"""Per-aircraft state built from decoded DF17/18 messages."""
import time
from . import modes

PAIR_SECONDS = 10      # even/odd frames older than this are not paired
STALE_SECONDS = 60     # aircraft not heard from are dropped


class Tracker:
    def __init__(self, ref=None, clock=time.monotonic):
        self.aircraft = {}
        self.ref = ref          # optional receiver (lat, lon) for local CPR
        self.clock = clock

    def update(self, msg: dict) -> dict:
        now = self.clock()
        ac = self.aircraft.setdefault(msg['icao'], {'icao': msg['icao'], 'messages': 0})
        ac['messages'] += 1
        ac['seen'] = now
        kind = msg['kind']
        if kind == 'identification':
            ac['callsign'], ac['category'] = msg['callsign'], msg['category']
        elif kind == 'velocity':
            for key in ('ground_speed_kt', 'track_deg', 'vertical_rate_fpm'):
                if key in msg:
                    ac[key] = msg[key]
        elif kind == 'airborne_position':
            ac['altitude_ft'] = msg['altitude_ft']
            ac['odd' if msg['odd'] else 'even'] = (msg['cpr'], now)
            self._position(ac, msg['odd'], msg['cpr'])
        return ac

    def _position(self, ac, odd, cpr):
        even_, odd_ = ac.get('even'), ac.get('odd')
        pos = None
        if even_ and odd_ and abs(even_[1] - odd_[1]) <= PAIR_SECONDS:
            pos = modes.cpr_global(even_[0], odd_[0], odd)
        if pos is None and 'lat' in ac:
            pos = modes.cpr_local(*cpr, odd, ac['lat'], ac['lon'])
        if pos is None and self.ref:
            pos = modes.cpr_local(*cpr, odd, *self.ref)
        if pos:
            ac['lat'], ac['lon'] = pos

    def expire(self):
        cutoff = self.clock() - STALE_SECONDS
        for icao in [k for k, v in self.aircraft.items() if v['seen'] < cutoff]:
            del self.aircraft[icao]
