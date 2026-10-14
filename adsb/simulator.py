"""ADS-B traffic simulator.

Generates aircraft that fly around a centre point and emits DF17 frames as
AVR text (``*8D...;``, dump1090 raw port 30002 format) on TCP, stdout, or as
CU8 I/Q via the PPM modulator.

    python -m adsb.simulator --count 20 --tcp 30002
    python -m adsb.simulator --count 20 --tcp 30002 --web 8091 --state fleet.json
    python -m adsb.simulator --count 5 --seconds 30 --iq traffic.cu8

``--web`` serves a page for setting many aircraft at once (adsb.simweb).
Every aircraft is dead-reckoned from its last position, speed, track and
vertical rate as time passes.
"""
from __future__ import annotations

import argparse
import json
import math
import os
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
    category: int = 3
    # True: turn back towards the centre at the radius (random traffic).
    # False: keep flying straight on the predicted track (aircraft set from the web page).
    bounded: bool = True
    odd: bool = False
    since_ident: float = field(default=99)

    def step(self, dt: float, centre, radius_nm: float):
        dist = self.speed_kt * dt / 3600 / EARTH_NM
        lat1, lon1, brg = map(math.radians, (self.lat, self.lon, self.track_deg))
        lat2 = math.asin(math.sin(lat1) * math.cos(dist) + math.cos(lat1) * math.sin(dist) * math.cos(brg))
        lon2 = lon1 + math.atan2(math.sin(brg) * math.sin(dist) * math.cos(lat1),
                                 math.cos(dist) - math.sin(lat1) * math.sin(lat2))
        self.lat, self.lon = math.degrees(lat2), (math.degrees(lon2) + 540) % 360 - 180
        self.altitude_ft = max(0, min(MAX_ALT_FT, self.altitude_ft + self.vertical_rate_fpm * dt / 60))
        if self.altitude_ft in (0, MAX_ALT_FT):
            self.vertical_rate_fpm = 0
        if self.bounded and distance_nm(centre, (self.lat, self.lon)) > radius_nm:
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
            out.append(modes.encode_identification(self.icao, self.callsign, self.category))
            self.since_ident = 0
        return out

    def to_dict(self) -> dict:
        return {'icao': f'{self.icao:06X}', 'callsign': self.callsign, 'lat': round(self.lat, 6),
                'lon': round(self.lon, 6), 'altitude_ft': round(self.altitude_ft),
                'speed_kt': round(self.speed_kt, 1), 'track_deg': round(self.track_deg, 1),
                'vertical_rate_fpm': round(self.vertical_rate_fpm), 'category': self.category,
                'bounded': self.bounded}


MAX_ALT_FT = 50000
SAVE_EVERY_S = 10
MAX_AIRCRAFT = 500
CALLSIGN_CHARS = set('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ')
# name: (low, high) - limits the DF17 encoders can represent
LIMITS = {'lat': (-90, 90), 'lon': (-180, 180), 'altitude_ft': (0, MAX_ALT_FT), 'speed_kt': (0, 1000),
          'track_deg': (0, 360), 'vertical_rate_fpm': (-32000, 32000), 'category': (0, 7)}


def aircraft_from_dict(d: dict, bounded: bool = False) -> Aircraft:
    """Validate one aircraft row from the web page / state file. Raises ValueError."""
    if not isinstance(d, dict):
        raise ValueError('each aircraft must be an object')
    icao = str(d.get('icao', '')).strip().upper()
    if len(icao) != 6 or any(c not in '0123456789ABCDEF' for c in icao) or icao == '000000':
        raise ValueError(f'icao must be 6 hex digits: {icao!r}')
    callsign = str(d.get('callsign', '') or '').strip().upper()
    if len(callsign) > 8 or not set(callsign) <= CALLSIGN_CHARS:
        raise ValueError(f'callsign must be up to 8 of A-Z, 0-9 and space: {callsign!r}')
    values = {}
    for name, (low, high) in LIMITS.items():
        raw = d.get(name, 3 if name == 'category' else 0)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f'{icao} {name} must be a number: {raw!r}') from None
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'{icao} {name} must be between {low} and {high}: {raw!r}')
        values[name] = value
    return Aircraft(icao=int(icao, 16), callsign=callsign, lat=values['lat'], lon=values['lon'],
                    altitude_ft=values['altitude_ft'], speed_kt=values['speed_kt'],
                    track_deg=values['track_deg'] % 360, vertical_rate_fpm=values['vertical_rate_fpm'],
                    category=int(values['category']), bounded=bool(d.get('bounded', bounded)))


def validate_fleet(rows) -> list[Aircraft]:
    if not isinstance(rows, list):
        raise ValueError('aircraft must be a list')
    if len(rows) > MAX_AIRCRAFT:
        raise ValueError(f'at most {MAX_AIRCRAFT} aircraft')
    planes = [aircraft_from_dict(r) for r in rows]
    seen = set()
    for ac in planes:
        if ac.icao in seen:
            raise ValueError(f'duplicate icao {ac.icao:06X}')
        seen.add(ac.icao)
    return planes


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


class Simulation:
    """The fleet, shared between the simulation loop and the web page."""
    def __init__(self, planes, centre, radius_nm: float, state_path: str | None = None):
        self.planes, self.centre, self.radius = list(planes), centre, radius_nm
        self.state_path, self.lock, self.sim_time = state_path, threading.Lock(), 0.0

    def tick(self, dt: float) -> list[str]:
        with self.lock:
            frames = []
            for ac in self.planes:
                ac.step(dt, self.centre, self.radius)
                frames += ac.frames()
            self.sim_time += dt
            return frames

    def snapshot(self) -> dict:
        with self.lock:
            return {'time': round(self.sim_time, 1), 'centre': {'lat': self.centre[0], 'lon': self.centre[1]},
                    'radius_nm': self.radius, 'aircraft': [ac.to_dict() for ac in self.planes]}

    def replace(self, rows) -> int:
        planes = validate_fleet(rows)
        with self.lock:
            self.planes = planes
        self.save()
        return len(planes)

    def randomize(self, count: int, centre, radius_nm: float, seed=None) -> int:
        if not 0 <= count <= MAX_AIRCRAFT:
            raise ValueError(f'count must be 0..{MAX_AIRCRAFT}')
        if not (-90 <= centre[0] <= 90 and -180 <= centre[1] <= 180 and 0 < radius_nm <= 250):
            raise ValueError('centre or radius out of range')
        planes = fleet(count, centre, radius_nm, seed)
        with self.lock:
            self.planes, self.centre, self.radius = planes, centre, radius_nm
        self.save()
        return count

    def save(self):
        if not self.state_path:
            return
        data = self.snapshot()
        tmp = self.state_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, self.state_path)

    def load(self) -> bool:
        if not self.state_path or not os.path.exists(self.state_path):
            return False
        with open(self.state_path, encoding='utf-8') as f:
            data = json.load(f)
        planes = validate_fleet(data.get('aircraft', []))
        with self.lock:
            self.planes = planes
            c = data.get('centre') or {}
            self.centre = (float(c.get('lat', self.centre[0])), float(c.get('lon', self.centre[1])))
            self.radius = float(data.get('radius_nm', self.radius))
        return True


def run(args):
    centre = (args.lat, args.lon)
    sim = Simulation(fleet(args.count, centre, args.radius, args.seed), centre, args.radius, args.state)
    if sim.load():
        print(f'loaded {len(sim.planes)} aircraft from {args.state}', file=sys.stderr)
    feed = TcpFeed(args.tcp) if args.tcp else None
    if args.web:
        from . import simweb
        simweb.serve(sim, args.web, args.web_host)
    iq_frames, t, dt, saved = [], 0.0, 0.5, 0.0
    while args.seconds is None or t < args.seconds:
        if t - saved >= SAVE_EVERY_S:
            sim.save()                   # restart from the predicted positions, not the set ones
            saved = t
        for frame in sim.tick(dt):
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
    p.add_argument('--web', type=int, help='serve the aircraft settings page on this port (e.g. 8091)')
    p.add_argument('--web-host', default='0.0.0.0')
    p.add_argument('--state', help='JSON file that keeps the aircraft set from the web page')
    run(p.parse_args(argv))


if __name__ == '__main__':
    main()
