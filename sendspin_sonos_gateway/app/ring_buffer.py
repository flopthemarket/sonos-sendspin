"""
PCM ring buffer.

The gateway keeps a rolling window of raw PCM in memory. The *write*
pointer always advances as fresh Sendspin audio arrives. The *read*
pointer that feeds the encoder/stream sits BEHIND the write pointer by
however many milliseconds of delay are currently configured.

Because "changing the delay" is just moving the read pointer backwards
or forwards inside a buffer that already holds the audio, adjusting the
Home Assistant slider never requires restarting the Sonos stream.
"""
from __future__ import annotations

import threading
import logging

log = logging.getLogger("ssg.ring_buffer")

SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_SAMPLE = 2  # 16-bit PCM
FRAME_SIZE = CHANNELS * BYTES_PER_SAMPLE  # bytes per audio frame (one sample per channel)


class RingBuffer:
    def __init__(self, capacity_seconds: float = 20.0):
        self.sample_rate = SAMPLE_RATE
        self.channels = CHANNELS
        self.capacity_seconds = capacity_seconds
        self.capacity_bytes = int(capacity_seconds * self.sample_rate * FRAME_SIZE)

        self._buf = bytearray(self.capacity_bytes)
        self._lock = threading.Condition()

        # Pointers are monotonically increasing byte offsets (not wrapped);
        # wrapping into self._buf happens via modulo at access time. This
        # keeps "how far behind is read from write" a trivial subtraction.
        self._write_pos = 0
        self._read_pos = 0
        self._closed = False

    # ---------------------------------------------------------- writing --
    def write(self, pcm_bytes: bytes) -> None:
        """Append newly-arrived PCM audio. Called from the Sendspin client."""
        if not pcm_bytes:
            return
        with self._lock:
            n = len(pcm_bytes)
            if n > self.capacity_bytes:
                # Pathological case: a single chunk bigger than the whole
                # buffer. Keep only the tail of it.
                pcm_bytes = pcm_bytes[-self.capacity_bytes:]
                n = len(pcm_bytes)

            start = self._write_pos % self.capacity_bytes
            end = start + n
            if end <= self.capacity_bytes:
                self._buf[start:end] = pcm_bytes
            else:
                first_len = self.capacity_bytes - start
                self._buf[start:] = pcm_bytes[:first_len]
                self._buf[: n - first_len] = pcm_bytes[first_len:]

            self._write_pos += n

            # If the read pointer has fallen further behind than the
            # buffer can hold (e.g. delay was set larger than capacity,
            # or we've been starved), clamp it so it never reads stale
            # data that has already been overwritten.
            min_read_pos = self._write_pos - self.capacity_bytes
            if self._read_pos < min_read_pos:
                self._read_pos = min_read_pos

            self._lock.notify_all()

    # ---------------------------------------------------------- reading --
    def read(self, num_bytes: int, timeout: float | None = 1.0) -> bytes:
        """
        Blocking read of exactly `num_bytes` (or less, if closed/timed out)
        from the current read pointer, honoring whatever delay offset is
        currently applied to that pointer (see DelayEngine).
        """
        with self._lock:
            out = bytearray()
            while len(out) < num_bytes and not self._closed:
                available = self._write_pos - self._read_pos
                if available <= 0:
                    if not self._lock.wait(timeout=timeout):
                        break
                    continue

                take = min(available, num_bytes - len(out))
                start = self._read_pos % self.capacity_bytes
                end = start + take
                if end <= self.capacity_bytes:
                    out += self._buf[start:end]
                else:
                    first_len = self.capacity_bytes - start
                    out += self._buf[start:]
                    out += self._buf[: take - first_len]

                self._read_pos += take
            return bytes(out)

    def set_read_offset_bytes(self, offset_bytes: int) -> None:
        """Move the read pointer to (write_pos - offset_bytes), clamped to
        what the buffer actually still holds."""
        with self._lock:
            target = self._write_pos - offset_bytes
            min_read_pos = self._write_pos - self.capacity_bytes
            max_read_pos = self._write_pos
            self._read_pos = max(min_read_pos, min(target, max_read_pos))
            self._lock.notify_all()

    def fill_seconds(self) -> float:
        with self._lock:
            available = self._write_pos - self._read_pos
            return available / (self.sample_rate * FRAME_SIZE)

    def fill_ratio(self) -> float:
        with self._lock:
            available = self._write_pos - self._read_pos
            return max(0.0, min(1.0, available / self.capacity_bytes))

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._lock.notify_all()
