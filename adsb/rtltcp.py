"""Minimal rtl_tcp protocol: 12-byte "RTL0" header, 5-byte commands, CU8 stream.

Used by both ends of the software loop-back (adsb.sdrtx serves, the receiver
connects). The client also works against a real ``rtl_tcp -s 2000000``.
"""
from __future__ import annotations

import socket
import struct
import sys
import time

FREQUENCY = 1_090_000_000
SET_FREQUENCY, SET_SAMPLE_RATE, SET_GAIN_MODE = 1, 2, 3
HEADER = struct.Struct('>4sII')      # magic, tuner type, gain count
COMMAND = struct.Struct('>BI')


def command(cmd: int, value: int) -> bytes:
    return COMMAND.pack(cmd, value)


def stream(address, rate: int, chunk: int = 65536, retry: float = 5.0):
    """Yield raw CU8 blocks from an rtl_tcp server, reconnecting on loss."""
    while True:
        try:
            with socket.create_connection(address, timeout=30) as sock:
                header = b''
                while len(header) < HEADER.size:
                    part = sock.recv(HEADER.size - len(header))
                    if not part:
                        raise ConnectionError('closed before rtl_tcp header')
                    header += part
                if HEADER.unpack(header)[0] != b'RTL0':
                    raise ConnectionError('not an rtl_tcp server')
                sock.sendall(command(SET_SAMPLE_RATE, rate) + command(SET_FREQUENCY, FREQUENCY)
                             + command(SET_GAIN_MODE, 0))
                print(f'rtl_tcp connected {address[0]}:{address[1]} ({rate} S/s)', file=sys.stderr)
                while True:
                    data = sock.recv(chunk)
                    if not data:
                        raise ConnectionError('rtl_tcp closed')
                    yield data
        except OSError as exc:
            print(f'rtl_tcp {address[0]}:{address[1]}: {exc}', file=sys.stderr)
        time.sleep(retry)


def usb(device: str = '0', rate: int = 2_000_000, gain: str = '0', chunk: int = 65536, retry: float = 5.0):
    """Yield CU8 blocks from a USB RTL-SDR via the ``rtl_sdr`` tool (gain 0 = auto)."""
    import subprocess
    cmd = ['rtl_sdr', '-d', str(device), '-f', str(FREQUENCY), '-s', str(rate), '-g', str(gain), '-']
    while True:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            raise SystemExit('rtl_sdr not found: install rtl-sdr (apt install rtl-sdr / brew install librtlsdr)')
        print(f'rtl_sdr device {device} started ({rate} S/s)', file=sys.stderr)
        try:
            while True:
                data = proc.stdout.read(chunk)
                if not data:
                    break
                yield data
        finally:
            proc.kill()
            proc.wait()
        print(f'rtl_sdr device {device} stopped; retrying', file=sys.stderr)
        time.sleep(retry)
