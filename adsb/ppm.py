"""1090 MHz PPM baseband at 2 Msps: modulator and a minimal demodulator.

Mirrors sdr-tx/modulator.py for AIS: produces CU8 I/Q (rtl_sdr / rtl_tcp
format) that dump1090/readsb can read, and a reference demodulator used by
tests and the software loop-back path.
"""
from __future__ import annotations

import numpy as np
from . import modes

RATE = 2_000_000
PREAMBLE = (1, 0, 1, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0)   # 8 us at 0.5 us/sample


def samples(frame_hex: str) -> np.ndarray:
    chips = list(PREAMBLE)
    for byte in bytes.fromhex(frame_hex):
        for shift in range(7, -1, -1):
            chips += (1, 0) if (byte >> shift) & 1 else (0, 1)
    return np.array(chips, dtype=float)


def waveform(frames, amplitude=0.6, gap=200, offset_hz=0.0) -> np.ndarray:
    parts = [np.zeros(gap)]
    for frame in frames:
        parts += [samples(frame), np.zeros(gap)]
    env = amplitude * np.concatenate(parts)
    phase = 2 * np.pi * offset_hz * np.arange(len(env)) / RATE
    return env * np.exp(1j * phase)


def cu8(iq: np.ndarray, noise=0.0, rng=None) -> bytes:
    if noise:
        rng = rng or np.random.default_rng()
        iq = iq + noise * (rng.normal(size=len(iq)) + 1j * rng.normal(size=len(iq)))
    out = np.empty(len(iq) * 2)
    out[::2], out[1::2] = iq.real, iq.imag
    return np.clip(np.rint(out * 127 + 127.5), 0, 255).astype(np.uint8).tobytes()


FRAME = 16 + 224        # preamble + 112 bits, in samples
HIGH, LOW = (0, 2, 7, 9), (1, 3, 4, 5, 6, 8)


def scan(mag: np.ndarray, limit: int | None = None, threshold: float = 10.0):
    """Find CRC-valid DF17/18 frames whose preamble starts before ``limit``.

    Returns (frames, next_start): the caller keeps samples from next_start on
    for the following block, so streamed input is neither missed nor doubled.
    """
    n = min(len(mag) - FRAME, len(mag) if limit is None else limit)
    if n <= 0:
        return [], 0
    hi = np.minimum.reduce([mag[k:k + n] for k in HIGH])
    lo = np.maximum.reduce([mag[k:k + n] for k in LOW])
    found, skip = [], 0
    for i in np.flatnonzero((hi > threshold) & (hi > 2 * lo)):
        if i < skip:
            continue
        d = mag[i + 16:i + FRAME]
        frame = np.packbits(d[::2] > d[1::2]).tobytes()
        if frame[0] >> 3 in (17, 18) and not modes.crc24(frame):
            found.append(frame.hex().upper())
            skip = i + FRAME
    return found, max(n, skip)


def magnitude(raw: bytes) -> np.ndarray:
    iq = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5
    return np.hypot(iq[::2], iq[1::2])


def demodulate(raw: bytes) -> list[str]:
    """Return CRC-valid 112-bit DF17/18 frames found in CU8 I/Q."""
    return scan(magnitude(raw))[0]


class StreamDemodulator:
    """Incremental demodulator for a live CU8 stream (rtl_tcp)."""
    def __init__(self, threshold: float = 10.0):
        self.tail = np.zeros(0, dtype=np.float32)
        self.odd = b''           # a recv() can split an I/Q byte pair
        self.threshold = threshold

    def feed(self, raw: bytes) -> list[str]:
        raw = self.odd + raw
        even = len(raw) // 2 * 2
        self.odd = raw[even:]
        mag = np.concatenate((self.tail, magnitude(raw[:even])))
        frames, keep = scan(mag, len(mag) - FRAME, self.threshold)
        self.tail = mag[keep:]
        return frames
