"""Map ADS-B aircraft state onto the eNMEA2000 data model.

Two outputs are provided:

* ``event_row`` - the same row shape that ``app.parser.Decoder.parse``
  produces, so ``app.store.Store.add`` can persist ADS-B next to NMEA/AIS.
* ``ais_type9`` - an AIS Type 9 (SAR aircraft position report) ``!AIVDM``
  sentence, so the current receiver, GIS and iPad app show aircraft without
  any change. The MMSI is a pseudo id (100000000 + ICAO) and altitude is
  limited to 4094 m by the AIS format; it is a bridge for testing, not a
  replacement for a native ADS-B view.
"""
import json
from datetime import datetime, timezone

FT_TO_M = 0.3048


def pseudo_mmsi(icao: str) -> int:
    return 100000000 + int(icao, 16)


def event_row(raw: str, msg: dict, ac: dict, source: str, received_at: str | None = None) -> dict:
    decoded = {k: v for k, v in msg.items() if k != 'cpr'}
    decoded['aircraft'] = {k: v for k, v in ac.items() if k not in ('even', 'odd', 'seen')}
    return dict(received_at=received_at or datetime.now(timezone.utc).isoformat(), source=source,
                raw=raw, sentence_type='ADSB', status='ok', checksum_valid=True, decoded=decoded,
                latitude=ac.get('lat'), longitude=ac.get('lon'), mmsi=None, event_time=None,
                icao=ac['icao'])


def _armor(bits: str) -> tuple[str, int]:
    fill = (-len(bits)) % 6
    bits += '0' * fill
    out = []
    for i in range(0, len(bits), 6):
        v = int(bits[i:i + 6], 2)
        out.append(chr(v + 48 if v < 40 else v + 56))
    return ''.join(out), fill


def _field(value: int, width: int) -> str:
    return format(value & ((1 << width) - 1), f'0{width}b')


def ais_type9(ac: dict, second: int | None = None) -> str | None:
    """!AIVDM Type 9 for an aircraft with a known position, else None."""
    if ac.get('lat') is None or ac.get('lon') is None:
        return None
    alt = ac.get('altitude_ft')
    alt_m = 4095 if alt is None else max(0, min(4094, round(alt * FT_TO_M)))
    speed = ac.get('ground_speed_kt')
    speed = 1023 if speed is None else min(1022, round(speed))
    track = ac.get('track_deg')
    course = 3600 if track is None else round(track * 10) % 3600
    second = datetime.now(timezone.utc).second if second is None else second
    bits = (_field(9, 6) + _field(0, 2) + _field(pseudo_mmsi(ac['icao']), 30) + _field(alt_m, 12)
            + _field(speed, 10) + '0' + _field(round(ac['lon'] * 600000), 28)
            + _field(round(ac['lat'] * 600000), 27) + _field(course, 12) + _field(second, 6)
            + _field(0, 8) + '1' + _field(0, 3) + '0' + '0' + _field(0, 20))
    payload, fill = _armor(bits)
    body = f'AIVDM,1,1,,A,{payload},{fill}'
    cs = 0
    for c in body:
        cs ^= ord(c)
    return f'!{body}*{cs:02X}'


def summary(ac: dict) -> str:
    return json.dumps({k: v for k, v in ac.items() if k not in ('even', 'odd', 'seen')}, ensure_ascii=False)
