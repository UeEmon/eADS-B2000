"""ADS-B receiver: AVR text or CU8 I/Q in, aircraft rows / $PADSB out (never converted to AIS).

    # from dump1090/readsb raw output (or adsb.simulator --tcp 30002)
    python -m adsb.receiver --avr-tcp localhost:30002 --forward localhost:10111
    # from an I/Q recording made by adsb.simulator --iq or rtl_sdr -s 2000000 -f 1090e6
    python -m adsb.receiver --iq traffic.cu8
    # live I/Q from adsb.sdrtx (software) or a real `rtl_tcp -s 2000000`
    python -m adsb.receiver --rtltcp localhost:1236 --forward localhost:10111
    # USB RTL-SDR on this machine (needs the rtl_sdr tool)
    python -m adsb.receiver --rtlsdr 0 --forward localhost:10111
    # several destinations at once: eNMEA2000 (TCP) and an iPad (UDP), both $PADSB
    python -m adsb.receiver --rtltcp localhost:1236 \
        --output tcp://localhost:10111,udp://192.168.1.30:10110
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from . import mapping, modes, ppm, rtltcp
from .tracker import Tracker


def frames_from_avr(lines):
    for line in lines:
        line = line.strip()
        if line.startswith(('*', '@')) and line.endswith(';'):
            body = line[1:-1]
            yield body[12:] if line[0] == '@' else body   # @ = 48-bit timestamp prefix


class Output:
    """One $PADSB destination: TCP (reconnects after a failure) or UDP datagrams."""

    def __init__(self, protocol, address, retry=5.0):
        if protocol not in ('tcp', 'udp'):
            raise ValueError(f'unsupported output protocol {protocol}')
        self.protocol, self.address, self.retry = protocol, address, retry
        self.sock, self.next_try = None, 0.0
        self.stats = {'sent': 0, 'failures': 0}

    def __repr__(self):
        return f'{self.protocol}://{self.address[0]}:{self.address[1]}'

    def send(self, sentence):
        now = time.monotonic()
        if self.sock is None and now < self.next_try:
            return
        data = (sentence + '\r\n').encode()
        try:
            if self.protocol == 'udp':
                if self.sock is None:
                    self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.sock.sendto(data, self.address)
            else:
                if self.sock is None:
                    self.sock = socket.create_connection(self.address, timeout=5)
                self.sock.sendall(data)
            self.stats['sent'] += 1
        except OSError as exc:
            print(f'output {self} failed: {exc}', file=sys.stderr)
            self.stats['failures'] += 1
            self.close()
            self.next_try = now + self.retry

    def close(self):
        if self.sock:
            self.sock.close()
        self.sock = None


def parse_outputs(value):
    """'tcp://host:port,udp://host:port'; a bare host:port is TCP."""
    outputs = []
    for item in filter(None, (v.strip() for v in value.split(','))):
        protocol, sep, rest = item.partition('://')
        if not sep:
            protocol, rest = 'tcp', item
        outputs.append(Output(protocol.lower(), _hostport(rest)))
    return outputs


class Receiver:
    def __init__(self, source='adsb', forward=None, ref=None, bridge_interval=1.0, outputs=()):
        self.tracker, self.source = Tracker(ref=ref), source
        self.outputs = list(outputs) + ([Output('tcp', forward)] if forward else [])
        self.sent = {}
        self.bridge_interval = bridge_interval
        self.stats = {'frames': 0, 'decoded': 0, 'errors': 0}

    def handle(self, frame: str):
        self.stats['frames'] += 1
        if len(frame) != 28:
            return None
        try:
            msg = modes.decode(frame)
        except ValueError:
            self.stats['errors'] += 1
            return None
        self.stats['decoded'] += 1
        ac = self.tracker.update(msg)
        row = mapping.event_row(frame, msg, ac, self.source)
        self._bridge(ac)
        return row

    def _bridge(self, ac):
        if not self.outputs or ac.get('lat') is None:
            return
        now = time.monotonic()
        if now - self.sent.get(ac['icao'], 0) < self.bridge_interval:
            return
        self.sent[ac['icao']] = now
        sentence = mapping.padsb(ac)
        for out in self.outputs:
            out.send(sentence)


def avr_tcp(address, retry=5.0):
    """Yield frames from a raw AVR feed, reconnecting when it drops."""
    while True:
        try:
            with socket.create_connection(address, timeout=30) as sock:
                print(f'connected to {address[0]}:{address[1]}', file=sys.stderr)
                yield from frames_from_avr(sock.makefile('r'))
        except OSError as exc:
            print(f'AVR source {address[0]}:{address[1]}: {exc}', file=sys.stderr)
        time.sleep(retry)


def demodulated(blocks):
    demod = ppm.StreamDemodulator()
    for block in blocks:
        yield from demod.feed(block)


def _hostport(value):
    host, port = value.rsplit(':', 1)
    if not host or not 0 < int(port) < 65536:
        raise ValueError(f'bad host:port {value!r}')
    return host, int(port)


def main(argv=None):
    p = argparse.ArgumentParser(description='ADS-B 1090ES receiver')
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument('--avr-tcp', type=_hostport, help='host:port of AVR raw feed (30002)')
    src.add_argument('--avr-file', help='file with *...; lines ("-" for stdin)')
    src.add_argument('--iq', help='CU8 I/Q file at 2 Msps')
    src.add_argument('--rtltcp', type=_hostport, help='host:port of rtl_tcp I/Q (adsb.sdrtx 1236 or rtl_tcp)')
    src.add_argument('--rtlsdr', metavar='DEVICE', help='USB RTL-SDR index or serial (rtl_sdr tool)')
    p.add_argument('--forward', type=_hostport, help='send aircraft to eNMEA2000 TCP (10111)')
    p.add_argument('--output', action='append', default=[], metavar='LIST',
                   help='comma-separated tcp://host:port or udp://host:port; repeatable '
                        '(e.g. eNMEA2000 and iPads at once, all $PADSB)')
    p.add_argument('--ref', type=lambda v: tuple(map(float, v.split(','))), help='receiver lat,lon')
    p.add_argument('--quiet', action='store_true', help='no JSON rows on stdout')
    args = p.parse_args(argv)
    try:
        outputs = [o for value in args.output for o in parse_outputs(value)]
    except ValueError as exc:
        p.error(f'--output: {exc}')
    rx = Receiver(forward=args.forward, ref=args.ref, outputs=outputs)
    for out in rx.outputs:
        print(f'output {out}', file=sys.stderr)
    if args.iq:
        with open(args.iq, 'rb') as f:
            frames = ppm.demodulate(f.read())
    elif args.rtltcp:
        frames = demodulated(rtltcp.stream(args.rtltcp, ppm.RATE))
    elif args.rtlsdr is not None:
        frames = demodulated(rtltcp.usb(args.rtlsdr, ppm.RATE))
    elif args.avr_file:
        frames = frames_from_avr(sys.stdin if args.avr_file == '-' else open(args.avr_file))
    else:
        frames = avr_tcp(args.avr_tcp)
    for frame in frames:
        row = rx.handle(frame)
        if row and not args.quiet:
            print(json.dumps(row, ensure_ascii=False), flush=True)
    print(json.dumps(rx.stats), file=sys.stderr)


if __name__ == '__main__':
    main()
