"""
Delay Engine

Owns the user-facing "delay_ms" value (driven by the HA `number.sendspin_delay`
entity) and translates it into a byte offset applied to the RingBuffer's read
pointer. This is the whole trick that lets the slider move live without ever
touching the Sonos connection: Sonos just keeps pulling bytes from the same
HTTP stream, and where those bytes come from inside the ring buffer is all
that changes.
"""
from __future__ import annotations

import logging
import threading

from ring_buffer import RingBuffer, SAMPLE_RATE, FRAME_SIZE

log = logging.getLogger("ssg.delay_engine")

MIN_DELAY_MS = 0
MAX_DELAY_MS = 5000


class DelayEngine:
    def __init__(self, ring_buffer: RingBuffer, initial_delay_ms: int = 1800):
        self._ring_buffer = ring_buffer
        self._lock = threading.Lock()
        self._delay_ms = 0
        self.set_delay(initial_delay_ms)

    def set_delay(self, delay_ms: int) -> int:
        """Set delay in milliseconds, clamped to [MIN_DELAY_MS, MAX_DELAY_MS].
        Returns the clamped value actually applied."""
        clamped = max(MIN_DELAY_MS, min(MAX_DELAY_MS, int(delay_ms)))
        with self._lock:
            self._delay_ms = clamped
            offset_bytes = self._ms_to_bytes(clamped)
            self._ring_buffer.set_read_offset_bytes(offset_bytes)
        log.info("Delay set to %d ms (%d bytes lookback)", clamped, offset_bytes)
        return clamped

    def get_delay(self) -> int:
        with self._lock:
            return self._delay_ms

    @staticmethod
    def _ms_to_bytes(delay_ms: int) -> int:
        delay_samples = int(delay_ms * SAMPLE_RATE / 1000)
        return delay_samples * FRAME_SIZE
