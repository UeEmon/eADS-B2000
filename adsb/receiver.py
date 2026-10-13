"""ADS-B receiver: AVR text or CU8 I/Q in, aircraft rows / AIS bridge out.

    # from dump1090/readsb raw output (or adsb.simulator --tcp 30002)
    python -m adsb.receiver --avr-tcp localhost:30002 --forward host.docker.internal:10111
    # from an I/Q recording made by adsb.simulator --iq or rtl_sdr -s 2000000 -f 1090e6
    python -m adsb.receiver --iq traffic.cu8
"""
import argparse
import json
import socket
import sys
import time
from . import mapping, modes, ppm
from .tracker import Tracker


def frames_from_avr(lines):
    for line in lines:
        line = line.strip()
        if line.startswith(('*', '@')) and line.endswith(';'):
            body = line[1:-1]
            yield body[12:] if line[0] == '@' else body   # @ = 48-bit timestamp prefix


class Receiver:
    def __init__(self, source='adsb', forward=None, ref=None, bridge_interval=1.0):
        self.tracker, self.source = Tracker(ref=ref), source
        self.forward, self.sock, self.sent = forward, None, {}
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
        if not self.forward:
            return
        now = time.monotonic()
        if now - self.sent.get(ac['icao'], 0) < self.bridge_interval:
            return
        sentence = mapping.ais_type9(ac)
        if not sentence:
            return
        try:
            if self.sock is None:
                self.sock = socket.create_connection(self.forward, timeout=5)
            self.sock.sendall((sentence + '\r\n').encode())
            self.sent[ac['icao']] = now
        except OSError as exc:
            print(f'forward failed: {exc}', file=sys.stderr)
            self.sock = None


def _hostport(value):
    host, port = value.rsplit(':', 1)
    return host, int(port)


def main(argv=None):
    p = argparse.ArgumentParser(description='ADS-B 1090ES receiver')
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument('--avr-tcp', type=_hostport, help='host:port of AVR raw feed (30002)')
    src.add_argument('--avr-file', help='file with *...; lines ("-" for stdin)')
    src.add_argument('--iq', help='CU8 I/Q file at 2 Msps')
    p.add_argument('--forward', type=_hostport, help='send AIS Type 9 bridge to eNMEA2000 TCP (10111)')
    p.add_argument('--ref', type=lambda v: tuple(map(float, v.split(','))), help='receiver lat,lon')
    p.add_argument('--quiet', action='store_true', help='no JSON rows on stdout')
    args = p.parse_args(argv)
    rx = Receiver(forward=args.forward, ref=args.ref)
    if args.iq:
        with open(args.iq, 'rb') as f:
            frames = ppm.demodulate(f.read())
    elif args.avr_file:
        frames = frames_from_avr(sys.stdin if args.avr_file == '-' else open(args.avr_file))
    else:
        frames = frames_from_avr(socket.create_connection(args.avr_tcp).makefile('r'))
    for frame in frames:
        row = rx.handle(frame)
        if row and not args.quiet:
            print(json.dumps(row, ensure_ascii=False), flush=True)
    print(json.dumps(rx.stats), file=sys.stderr)


if __name__ == '__main__':
    main()
