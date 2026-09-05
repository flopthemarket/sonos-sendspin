"""
Calibration Tone Generator

Produces a short WAV file containing a single linear-sweep chirp (a
"whoop" from f0 to f1 Hz, Hann-windowed to avoid clicks) preceded and
followed by silence. Served at GET /calibration-tone.wav so the same,
known, well-behaved test signal can be queued in Music Assistant to play
on both this gateway's Sonos output and a real Sendspin speaker.

A chirp is used instead of relying on whatever's already playing because:
  - It's broadband (covers f0..f1), so it survives typical speaker/mic
    frequency response better than a single tone or a very short click.
  - It has a single, sharp, unambiguous correlation peak against a KNOWN
    reference (matched filtering), unlike autocorrelating arbitrary music,
    which has its own periodicity (bass lines, drum loops) that can
    produce correlation peaks unrelated to the actual inter-speaker delay.

IMPORTANT: the parameters here (sample rate, timing, frequencies) MUST
exactly match the JS reference-generation function embedded in
calibration.py, since the browser needs to correlate the recorded
microphone audio against a reference chirp it generates independently
(not by decoding this WAV file) - see the shared parameter constants
below, used by both the WAV generator and referenced in the JS docstring.
"""
from __future__ import annotations

import io
import math
import struct

SAMPLE_RATE = 48000
F0_HZ = 500
F1_HZ = 2000
CHIRP_DURATION_MS = 100
LEAD_IN_MS = 500
TRAIL_MS = 2000
TOTAL_DURATION_MS = LEAD_IN_MS + CHIRP_DURATION_MS + TRAIL_MS


def _gen_chirp(sample_rate: int, duration_ms: float, f0: float, f1: float) -> list[float]:
    n = int(sample_rate * duration_ms / 1000)
    t_total = duration_ms / 1000
    out = []
    for i in range(n):
        t = i / sample_rate
        phase = 2 * math.pi * (f0 * t + (f1 - f0) / (2 * t_total) * t * t)
        window = 0.5 * (1 - math.cos(2 * math.pi * i / (n - 1))) if n > 1 else 1.0
        out.append(math.sin(phase) * window)
    return out


def generate_wav_bytes() -> bytes:
    """Generate the full calibration tone as 16-bit PCM mono WAV bytes."""
    n_total = int(SAMPLE_RATE * TOTAL_DURATION_MS / 1000)
    samples = [0.0] * n_total
    chirp = _gen_chirp(SAMPLE_RATE, CHIRP_DURATION_MS, F0_HZ, F1_HZ)
    start = int(SAMPLE_RATE * LEAD_IN_MS / 1000)
    for i, v in enumerate(chirp):
        if start + i < n_total:
            samples[start + i] = v

    pcm = bytearray()
    for v in samples:
        v = max(-1.0, min(1.0, v))
        pcm += struct.pack("<h", int(v * 32767))

    n_channels = 1
    bits_per_sample = 16
    byte_rate = SAMPLE_RATE * n_channels * bits_per_sample // 8
    block_align = n_channels * bits_per_sample // 8
    data_size = len(pcm)

    buf = io.BytesIO()
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + data_size))
    buf.write(b"WAVE")
    buf.write(b"fmt ")
    buf.write(struct.pack("<IHHIIHH", 16, 1, n_channels, SAMPLE_RATE, byte_rate, block_align, bits_per_sample))
    buf.write(b"data")
    buf.write(struct.pack("<I", data_size))
    buf.write(pcm)
    return buf.getvalue()


# Generated once at import time - it's tiny (a few hundred KB) and never
# changes, no need to regenerate per request.
_CACHED_WAV_BYTES: bytes | None = None


def get_wav_bytes() -> bytes:
    global _CACHED_WAV_BYTES
    if _CACHED_WAV_BYTES is None:
        _CACHED_WAV_BYTES = generate_wav_bytes()
    return _CACHED_WAV_BYTES
