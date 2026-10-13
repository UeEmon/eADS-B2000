"""1090 MHz PPM baseband at 2 Msps: modulator and a minimal demodulator.

Mirrors sdr-tx/modulator.py for AIS: produces CU8 I/Q (rtl_sdr / rtl_tcp
format) that dump1090/readsb can read, and a reference demodulator used by
tests and the software loop-back path.
"""
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


def demodulate(raw: bytes) -> list[str]:
    """Return CRC-valid 112-bit DF17/18 frames found in CU8 I/Q."""
    iq = np.frombuffer(raw, dtype=np.uint8).astype(float) - 127.5
    mag = np.hypot(iq[::2], iq[1::2])
    found, i, n = [], 0, len(mag) - 16 - 224
    high, low = (0, 2, 7, 9), (1, 3, 4, 5, 6, 8)
    while i < n:
        w = mag[i:i + 16]
        hi = min(w[k] for k in high)
        if hi > 10 and hi > 2 * max(w[k] for k in low):
            d = mag[i + 16:i + 16 + 224]
            bits = (d[::2] > d[1::2]).astype(np.uint8)
            frame = np.packbits(bits).tobytes()
            if frame[0] >> 3 in (17, 18) and not modes.crc24(frame):
                found.append(frame.hex().upper())
                i += 16 + 224
                continue
        i += 1
    return found
