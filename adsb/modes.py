"""Mode S / ADS-B 1090ES (DO-260B) frame codec: CRC-24, DF17/18, CPR.

Encoder and decoder share the same bit layout so the simulator and the
receiver round-trip each other. Only extended squitter (DF17/18) frames are
decoded; DF4/5/20/21 address-parity replies are out of scope for now.
"""
from __future__ import annotations

import math

CHARSET = '#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######'
GENERATOR = 0x1FFF409
NZ = 15
CPR_SCALE = 1 << 17


def crc24(data: bytes) -> int:
    """Mode S parity remainder over the whole frame (0 for a valid DF17)."""
    crc = 0
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= GENERATOR
    return crc & 0xFFFFFF


def with_parity(head: bytes) -> bytes:
    """Append the 24-bit parity to the first 88 (or 32) bits of a frame."""
    return head + crc24(head).to_bytes(3, 'big')


def _mod(x, y):
    return x - y * math.floor(x / y)


def nl(lat: float) -> int:
    """Number of CPR longitude zones (DO-260B A.1.7.2)."""
    if lat == 0:
        return 59
    if abs(lat) >= 87:
        return 1
    term = 1 - (1 - math.cos(math.pi / (2 * NZ))) / math.cos(math.radians(lat)) ** 2
    return int(math.floor(2 * math.pi / math.acos(max(-1.0, min(1.0, term)))))


# cpr_encode was first drafted by the local LLM (Ollama gpt-oss:20b) and then
# reviewed: its nl() returned 60 at the equator, fixed above by the lat == 0 case.
def cpr_encode(lat: float, lon: float, odd: bool) -> tuple[int, int]:
    """17-bit airborne CPR (yz, xz) for one format (even=0 / odd=1)."""
    i = int(odd)
    dlat = 360.0 / (4 * NZ - i)
    yz = math.floor(CPR_SCALE * _mod(lat, dlat) / dlat + 0.5)
    rlat = dlat * (yz / CPR_SCALE + math.floor(lat / dlat))
    dlon = 360.0 / max(nl(rlat) - i, 1)
    xz = math.floor(CPR_SCALE * _mod(lon, dlon) / dlon + 0.5)
    return yz & 0x1FFFF, xz & 0x1FFFF


def cpr_global(even: tuple[int, int], odd: tuple[int, int], odd_newest: bool):
    """Unambiguous position from an even/odd pair, or None if zones differ."""
    lat_e, lon_e = even[0] / CPR_SCALE, even[1] / CPR_SCALE
    lat_o, lon_o = odd[0] / CPR_SCALE, odd[1] / CPR_SCALE
    j = math.floor(59 * lat_e - 60 * lat_o + 0.5)
    rlat_e = 360 / 60 * (_mod(j, 60) + lat_e)
    rlat_o = 360 / 59 * (_mod(j, 59) + lat_o)
    rlat_e -= 360 if rlat_e >= 270 else 0
    rlat_o -= 360 if rlat_o >= 270 else 0
    if nl(rlat_e) != nl(rlat_o):
        return None
    lat = rlat_o if odd_newest else rlat_e
    zones = nl(lat)
    ni = max(zones - int(odd_newest), 1)
    m = math.floor(lon_e * (zones - 1) - lon_o * zones + 0.5)
    lon = 360 / ni * (_mod(m, ni) + (lon_o if odd_newest else lon_e))
    lon -= 360 if lon >= 180 else 0
    return round(lat, 6), round(lon, 6)


def cpr_local(yz: int, xz: int, odd: bool, ref_lat: float, ref_lon: float):
    """Position from one message near a reference (< 180 NM away)."""
    i = int(odd)
    dlat = 360 / (60 - i)
    y = yz / CPR_SCALE
    j = math.floor(ref_lat / dlat) + math.floor(_mod(ref_lat, dlat) / dlat - y + 0.5)
    lat = dlat * (j + y)
    dlon = 360 / max(nl(lat) - i, 1)
    x = xz / CPR_SCALE
    m = math.floor(ref_lon / dlon) + math.floor(_mod(ref_lon, dlon) / dlon - x + 0.5)
    return round(lat, 6), round(dlon * (m + x), 6)


def _bits(value: int, start: int, width: int, total: int = 56) -> int:
    return (value >> (total - start - width)) & ((1 << width) - 1)


def decode_altitude(code: int):
    """12-bit AC field with Q bit; Gillham (Q=0) is not decoded."""
    if not code or not code & 0x10:
        return None
    n = ((code & 0xFE0) >> 1) | (code & 0xF)
    return n * 25 - 1000


def encode_altitude(feet: float) -> int:
    n = max(0, min(0x7FF, round((feet + 1000) / 25)))
    return ((n & 0x7F0) << 1) | 0x10 | (n & 0xF)


def decode(frame: str | bytes) -> dict:
    """Decode one 112-bit extended squitter. Raises ValueError when invalid."""
    data = bytes.fromhex(frame) if isinstance(frame, str) else bytes(frame)
    if len(data) != 14:
        raise ValueError('112-bit frame required')
    if crc24(data):
        raise ValueError('CRC error')
    df = data[0] >> 3
    if df not in (17, 18):
        raise ValueError(f'Unsupported DF{df}')
    me = int.from_bytes(data[4:11], 'big')
    tc = _bits(me, 0, 5)
    msg = {'df': df, 'icao': data[1:4].hex().upper(), 'tc': tc}
    if 1 <= tc <= 4:
        msg['kind'] = 'identification'
        msg['category'] = _bits(me, 5, 3)
        msg['callsign'] = ''.join(CHARSET[_bits(me, 8 + 6 * k, 6)] for k in range(8)).replace('#', '').strip()
    elif 9 <= tc <= 18 or 20 <= tc <= 22:
        msg['kind'] = 'airborne_position'
        msg['altitude_source'] = 'baro' if tc <= 18 else 'gnss'
        alt = _bits(me, 8, 12)
        msg['altitude_ft'] = decode_altitude(alt) if tc <= 18 else (round(alt * 3.28084) if alt else None)
        msg['odd'] = bool(_bits(me, 21, 1))
        msg['cpr'] = (_bits(me, 22, 17), _bits(me, 39, 17))
    elif tc == 19:
        msg['kind'] = 'velocity'
        sub = _bits(me, 5, 3)
        msg['subtype'] = sub
        if sub in (1, 2):
            vew, vns = _bits(me, 14, 10), _bits(me, 25, 10)
            if vew and vns:
                scale = 4 if sub == 2 else 1
                vx = (vew - 1) * scale * (-1 if _bits(me, 13, 1) else 1)
                vy = (vns - 1) * scale * (-1 if _bits(me, 24, 1) else 1)
                msg['ground_speed_kt'] = round(math.hypot(vx, vy), 1)
                msg['track_deg'] = round(math.degrees(math.atan2(vx, vy)) % 360, 2)
        vr = _bits(me, 37, 9)
        if vr:
            msg['vertical_rate_fpm'] = (vr - 1) * 64 * (-1 if _bits(me, 36, 1) else 1)
    else:
        msg['kind'] = 'other'
    return msg


def _frame(icao: int, me: int, df: int = 17, ca: int = 5) -> str:
    head = bytes([(df << 3) | ca]) + icao.to_bytes(3, 'big') + me.to_bytes(7, 'big')
    return with_parity(head).hex().upper()


def encode_identification(icao: int, callsign: str, category: int = 3, tc: int = 4) -> str:
    me = (tc << 51) | (category << 48)
    for k, ch in enumerate(callsign.upper().ljust(8)[:8]):
        idx = CHARSET.find(ch) if ch != '#' else -1
        me |= (idx if idx > 0 else 32) << (42 - 6 * k)
    return _frame(icao, me)


def encode_position(icao: int, lat: float, lon: float, altitude_ft: float, odd: bool, tc: int = 11) -> str:
    yz, xz = cpr_encode(lat, lon, odd)
    me = (tc << 51) | (encode_altitude(altitude_ft) << 36) | (int(odd) << 34) | (yz << 17) | xz
    return _frame(icao, me)


def encode_velocity(icao: int, speed_kt: float, track_deg: float, vertical_rate_fpm: float = 0) -> str:
    vx = speed_kt * math.sin(math.radians(track_deg))
    vy = speed_kt * math.cos(math.radians(track_deg))
    vew, vns = min(round(abs(vx)) + 1, 1023), min(round(abs(vy)) + 1, 1023)
    vr = min(round(abs(vertical_rate_fpm) / 64) + 1, 511)
    me = ((19 << 51) | (1 << 48) | (int(vx < 0) << 42) | (vew << 32) | (int(vy < 0) << 31)
          | (vns << 21) | (int(vertical_rate_fpm < 0) << 19) | (vr << 10))
    return _frame(icao, me)
