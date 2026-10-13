"""ADS-B traffic simulator.

Generates aircraft that fly around a centre point and emits DF17 frames as
AVR text (``*8D...;``, dump1090 raw port 30002 format) on TCP, stdout, or as
CU8 I/Q via the PPM modulator.

    python -m adsb.simulator --count 20 --tcp 30002
    python -m adsb.simulator --count 5 --seconds 30 --iq traffic.cu8
"""
from __future__ import annotations

import argparse
import math
import random
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from . import modes, ppm

EARTH_NM = 3440.065
AIRLINES = ('JAL', 'ANA', 'SKY', 'ADO', 'SFJ', 'APJ', 'JJP', 'KAL', 'CPA', 'UAL')


@dataclass
class Aircraft:
    icao: int
    callsign: str
    lat: float
    lon: float
    altitude_ft: float
    speed_kt: float
    track_deg: float
    vertical_rate_fpm: float = 0
    odd: bool = False
    since_ident: float = field(default=99)

    def step(self, dt: float, centre, radius_nm: float):
        dist = self.speed_kt * dt / 3600 / EARTH_NM
        lat1, lon1, brg = map(math.radians, (self.lat, self.lon, self.track_deg))
        lat2 = math.asin(math.sin(lat1) * math.cos(dist) + math.cos(lat1) * math.sin(dist) * math.cos(brg))
        lon2 = lon1 + math.atan2(math.sin(brg) * math.sin(dist) * math.cos(lat1),
                                 math.cos(dist) - math.sin(lat1) * math.sin(lat2))
        self.lat, self.lon = math.degrees(lat2), (math.degrees(lon2) + 540) % 360 - 180
        self.altitude_ft = max(0, min(45000, self.altitude_ft + self.vertical_rate_fpm * dt / 60))
        if self.altitude_ft in (0, 45000):
            self.vertical_rate_fpm = 0
        if distance_nm(centre, (self.lat, self.lon)) > radius_nm:
            # turn back towards the centre at 3 deg/s
            want = bearing((self.lat, self.lon), centre)
            diff = (want - self.track_deg + 540) % 360 - 180
            self.track_deg = (self.track_deg + max(-3 * dt, min(3 * dt, diff))) % 360
        self.since_ident += dt

    def frames(self) -> list[str]:
        out = [modes.encode_position(self.icao, self.lat, self.lon, self.altitude_ft, self.odd),
               modes.encode_velocity(self.icao, self.speed_kt, self.track_deg, self.vertical_rate_fpm)]
        self.odd = not self.odd
        if self.since_ident >= 5:
            out.append(modes.encode_identification(self.icao, self.callsign))
            self.since_ident = 0
        return out


def distance_nm(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * EARTH_NM * math.asin(math.sqrt(h))


def bearing(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return math.degrees(math.atan2(y, x)) % 360


def fleet(count: int, centre, radius_nm: float, seed=None) -> list[Aircraft]:
    rng = random.Random(seed)
    out = []
    for _ in range(count):
        r, theta = radius_nm * math.sqrt(rng.random()), rng.uniform(0, 2 * math.pi)
        lat = centre[0] + r / 60 * math.cos(theta)
        lon = centre[1] + r / 60 * math.sin(theta) / math.cos(math.radians(centre[0]))
        out.append(Aircraft(icao=rng.randrange(0x840000, 0x87FFFF),   # Japan block
                            callsign=f'{rng.choice(AIRLINES)}{rng.randrange(1, 9999)}',
                            lat=lat, lon=lon, altitude_ft=rng.choice(range(3000, 41000, 1000)),
                            speed_kt=rng.uniform(180, 480), track_deg=rng.uniform(0, 360),
                            vertical_rate_fpm=rng.choice((0, 0, 0, 1500, -1500))))
    return out


class TcpFeed:
    """dump1090-style raw output server: every client gets every line."""
    def __init__(self, port: int, host: str = '0.0.0.0'):
        self.clients, self.lock = [], threading.Lock()
        self.server = socket.create_server((host, port), reuse_port=False)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            conn, _ = self.server.accept()
            with self.lock:
                self.clients.append(conn)

    def send(self, line: str):
        data = (line + '\n').encode()
        with self.lock:
            for c in list(self.clients):
                try:
                    c.sendall(data)
                except OSError:
                    self.clients.remove(c)


def run(args):
    centre = (args.lat, args.lon)
    planes = fleet(args.count, centre, args.radius, args.seed)
    feed = TcpFeed(args.tcp) if args.tcp else None
    iq_frames, t, dt = [], 0.0, 0.5
    while args.seconds is None or t < args.seconds:
        for ac in planes:
            ac.step(dt, centre, args.radius)
            for frame in ac.frames():
                if args.iq:
                    iq_frames.append(frame)
                elif feed:
                    feed.send(f'*{frame};')
                else:
                    print(f'*{frame};', flush=True)
        t += dt
        if not args.iq:
            time.sleep(dt / args.speedup)
    if args.iq:
        with open(args.iq, 'wb') as f:
            f.write(ppm.cu8(ppm.waveform(iq_frames, offset_hz=args.offset), args.noise))
        print(f'{len(iq_frames)} frames -> {args.iq}', file=sys.stderr)


def main(argv=None):
    p = argparse.ArgumentParser(description='ADS-B 1090ES traffic simulator')
    p.add_argument('--count', type=int, default=10)
    p.add_argument('--lat', type=float, default=35.5494)       # RJTT Haneda
    p.add_argument('--lon', type=float, default=139.7798)
    p.add_argument('--radius', type=float, default=60, help='NM')
    p.add_argument('--seconds', type=float, help='stop after N simulated seconds')
    p.add_argument('--speedup', type=float, default=1.0)
    p.add_argument('--seed', type=int)
    p.add_argument('--tcp', type=int, help='serve AVR on this TCP port (e.g. 30002)')
    p.add_argument('--iq', help='write CU8 I/Q at 2 Msps instead of AVR text')
    p.add_argument('--noise', type=float, default=0.0)
    p.add_argument('--offset', type=float, default=0.0, help='carrier offset Hz')
    run(p.parse_args(argv))


if __name__ == '__main__':
    main()
