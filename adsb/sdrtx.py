"""ADS-B SDR transmit emulator: AVR frames -> paced 1090 MHz PPM I/Q over rtl_tcp.

Counterpart of eNMEA2000's sdr-tx (AIS GMSK). Reads DF17 frames from an AVR
feed (adsb.simulator --tcp 30002, or dump1090 raw output) and serves them as
CU8 at 2 Msps on an rtl_tcp port, so the receiver - or any rtl_tcp client -
can demodulate them. Software only: nothing is sent to RF hardware.

    python -m adsb.sdrtx --avr-tcp localhost:30002 --rtltcp 1236
"""
from __future__ import annotations

import argparse
import collections
import queue
import socket
import socketserver
import struct
import sys
import threading
import time
import numpy as np
from . import ppm, rtltcp
from .receiver import _hostport, avr_tcp

BLOCK_SECONDS = 0.01
MAX_AGE = 2.0      # frames older than this are stale positions; drop them


class Transmitter:
    def __init__(self, amplitude=0.6, noise=0.02, offset=0.0, max_queue=4096):
        self.amplitude, self.noise, self.offset = amplitude, noise, offset
        self.frames = queue.Queue(max_queue)
        self.client = None
        self.lock = threading.Lock()
        self.counts = collections.Counter()

    def ingest(self, frame: str):
        self.counts['frames'] += 1
        if self.client is None:
            self.counts['dropped'] += 1     # nobody listening: like a radio with no receiver
            return
        try:
            self.frames.put_nowait((time.monotonic(), frame))
        except queue.Full:
            self.counts['dropped'] += 1

    def block(self, size: int, rng) -> bytes:
        """One paced block: queued frames at random offsets over noise."""
        iq = np.zeros(size, dtype=complex)
        cursor = 0
        while cursor + ppm.FRAME + 8 < size:
            try:
                stamp, frame = self.frames.get_nowait()
            except queue.Empty:
                break
            if time.monotonic() - stamp > MAX_AGE:
                self.counts['dropped'] += 1
                continue
            cursor += 8 + int(rng.integers(0, 32))
            iq[cursor:cursor + ppm.FRAME] = ppm.waveform([frame], amplitude=self.amplitude, gap=0,
                                                          offset_hz=self.offset)
            cursor += ppm.FRAME
            self.counts['modulated'] += 1
        return ppm.cu8(iq, self.noise, rng)


def serve(tx: Transmitter, port: int, host: str = '0.0.0.0'):
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            with tx.lock:
                if tx.client is not None:
                    return                  # one I/Q client at a time, like rtl_tcp
                tx.client = f'{self.client_address[0]}:{self.client_address[1]}'
            sock, closed = self.request, threading.Event()
            print(f'I/Q client {tx.client}', file=sys.stderr)

            def commands():
                pending = b''
                try:
                    while not closed.is_set():
                        data = sock.recv(1024)
                        if not data:
                            break
                        pending += data
                        while len(pending) >= 5:
                            cmd, value = rtltcp.COMMAND.unpack(pending[:5])
                            pending = pending[5:]
                            if cmd == rtltcp.SET_SAMPLE_RATE and value != ppm.RATE:
                                print(f'unsupported sample rate {value}; serving {ppm.RATE}', file=sys.stderr)
                            if cmd == rtltcp.SET_FREQUENCY and value != rtltcp.FREQUENCY:
                                print(f'tuned to {value} Hz; ADS-B is only on 1090 MHz', file=sys.stderr)
                except OSError:
                    pass
                finally:
                    closed.set()

            try:
                sock.sendall(rtltcp.HEADER.pack(b'RTL0', 0, 0))
                threading.Thread(target=commands, daemon=True).start()
                rng = np.random.default_rng()
                size = int(ppm.RATE * BLOCK_SECONDS)
                deadline = time.monotonic()
                while not closed.is_set():
                    sock.sendall(tx.block(size, rng))
                    deadline += BLOCK_SECONDS
                    delay = deadline - time.monotonic()
                    if delay < -0.5:
                        deadline = time.monotonic()      # client too slow: resync, don't burst
                    elif delay > 0:
                        closed.wait(delay)
            except OSError:
                pass
            finally:
                closed.set()
                with tx.lock:
                    tx.client = None
                while not tx.frames.empty():
                    tx.frames.get_nowait()
                print('I/Q client disconnected', file=sys.stderr)

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address, daemon_threads = True, True

    server = Server((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main(argv=None):
    p = argparse.ArgumentParser(description='ADS-B SDR transmit emulator (rtl_tcp I/Q, software only)')
    p.add_argument('--avr-tcp', type=_hostport, required=True, help='AVR frame source host:port (30002)')
    p.add_argument('--rtltcp', type=int, default=1236, help='rtl_tcp port to serve I/Q on')
    p.add_argument('--amplitude', type=float, default=0.6)
    p.add_argument('--noise', type=float, default=0.02)
    p.add_argument('--offset', type=float, default=0.0, help='carrier offset Hz')
    args = p.parse_args(argv)
    tx = Transmitter(args.amplitude, args.noise, args.offset)
    serve(tx, args.rtltcp)
    last = time.monotonic()
    for frame in avr_tcp(args.avr_tcp):
        tx.ingest(frame)
        if time.monotonic() - last > 30:
            print(dict(tx.counts, client=tx.client), file=sys.stderr, flush=True)
            last = time.monotonic()


if __name__ == '__main__':
    main()
