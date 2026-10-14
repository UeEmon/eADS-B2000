"""Map ADS-B aircraft state onto the eNMEA2000 data model.

Two outputs are provided. ADS-B stays separate from AIS: aircraft are never
turned into AIS messages (eNMEA2000 and its iPad app both read ``$PADSB``).

* ``padsb`` - the proprietary ``$PADSB`` sentence that eNMEA2000 parses
  natively (ICAO column, aircraft symbol, track details). This is the default.

* ``event_row`` - the same row shape that ``app.parser.Decoder.parse``
  produces, so ``app.store.Store.add`` can persist ADS-B next to NMEA/AIS.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

def event_row(raw: str, msg: dict, ac: dict, source: str, received_at: str | None = None) -> dict:
    decoded = {k: v for k, v in msg.items() if k != 'cpr'}
    decoded['aircraft'] = {k: v for k, v in ac.items() if k not in ('even', 'odd', 'seen')}
    return dict(received_at=received_at or datetime.now(timezone.utc).isoformat(), source=source,
                raw=raw, sentence_type='ADSB', status='ok', checksum_valid=True, decoded=decoded,
                latitude=ac.get('lat'), longitude=ac.get('lon'), mmsi=None, event_time=None,
                icao=ac['icao'])


def _nmea(body: str) -> str:
    cs = 0
    for c in body:
        cs ^= ord(c)
    return f'{body}*{cs:02X}'


def _num(value, fmt: str) -> str:
    return '' if value is None else format(value, fmt)


def padsb(ac: dict) -> str:
    """$PADSB,icao,callsign,lat,lon,altitude_ft,ground_speed_kt,track_deg,vertical_rate_fpm,category"""
    callsign = ''.join(c for c in (ac.get('callsign') or '').upper() if c.isalnum() or c == ' ')[:8]
    fields = [ac['icao'], callsign, _num(ac.get('lat'), '.6f'), _num(ac.get('lon'), '.6f'),
              _num(ac.get('altitude_ft'), 'd'), _num(ac.get('ground_speed_kt'), '.1f'),
              _num(ac.get('track_deg'), '.1f'), _num(ac.get('vertical_rate_fpm'), 'd'),
              _num(ac.get('category'), 'd')]
    return '$' + _nmea('PADSB,' + ','.join(fields))


def summary(ac: dict) -> str:
    return json.dumps({k: v for k, v in ac.items() if k not in ('even', 'odd', 'seen')}, ensure_ascii=False)
