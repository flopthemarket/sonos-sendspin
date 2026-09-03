"""
PCM ring buffer.

The gateway keeps a rolling window of raw PCM in memory. The *write*
pointer always advances as fresh Sendspin audio arrives. Each *reader*
(one per active HTTP connection to the stream server - normally just one,
but Sonos can and does open more than one connection to a stream URI
around a retry/reconnect) has its OWN read pointer that sits BEHIND the
write pointer by however many milliseconds of delay are currently
configured.

IMPORTANT: earlier versions of this buffer had a single shared read
pointer used by all callers. That's only correct for exactly one
consumer. When Sonos opened more than one concurrent HTTP connection to
the same stream (observed in practice - see git history), every read()
call from every connection's ffmpeg feeder thread advanced the *same*
pointer, so concurrent connections stole bytes from each other mid-frame.
That corrupts the FLAC framing fed to the encoder, which explains total
silence: Sonos can't decode garbage, so it never plays anything and just
keeps retrying, which opens more concurrent connections, making it worse.
Each reader now gets its own independent cursor via open_reader()/read()/
close_reader(), so concurrent connections each see a coherent, correctly
ordered byte stream regardless of what any other reader is doing.

Because "changing the delay" is just moving a read pointer backwards or
forwards inside a buffer that already holds the audio, adjusting the Home
Assistant slider never requires restarting the Sonos stream.
"""
from __future__ import annotations

import itertools
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
        self._closed = False

        # reader_id -> read_pos. Each active HTTP connection gets its own
        # entry so concurrent connections never share (and corrupt) a
        # cursor. Default delay applied to newly-opened readers is tracked
        # separately so a reader opened after a delay change starts at the
        # right offset without needing DelayEngine to know reader internals.
        self._readers: dict[int, int] = {}
        self._reader_id_gen = itertools.count(1)
        self._current_delay_bytes = 0

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

            # If any reader has fallen further behind than the buffer can
            # hold (e.g. delay was set larger than capacity, or that
            # reader's consumer stalled), clamp it so it never reads stale
            # data that has already been overwritten.
            min_read_pos = self._write_pos - self.capacity_bytes
            for reader_id, pos in self._readers.items():
                if pos < min_read_pos:
                    self._readers[reader_id] = min_read_pos

            self._lock.notify_all()

    # --------------------------------------------------- reader lifecycle --
    def open_reader(self) -> int:
        """Register a new independent reader (one per HTTP connection),
        starting at the currently configured delay offset behind the live
        write position. Returns a reader_id to pass to read()/close_reader()."""
        with self._lock:
            reader_id = next(self._reader_id_gen)
            target = self._write_pos - self._current_delay_bytes
            min_read_pos = self._write_pos - self.capacity_bytes
            self._readers[reader_id] = max(min_read_pos, min(target, self._write_pos))
            log.debug("Opened ring buffer reader #%d", reader_id)
            return reader_id

    def close_reader(self, reader_id: int) -> None:
        with self._lock:
            self._readers.pop(reader_id, None)
            log.debug("Closed ring buffer reader #%d", reader_id)

    # ---------------------------------------------------------- reading --
    def read(self, reader_id: int, num_bytes: int, timeout: float | None = 1.0) -> bytes:
        """
        Blocking read of exactly `num_bytes` (or less, if closed/timed out/
        reader closed) from this reader's own cursor.
        """
        with self._lock:
            out = bytearray()
            while len(out) < num_bytes and not self._closed:
                if reader_id not in self._readers:
                    # Reader was closed out from under us (shouldn't
                    # normally happen mid-read, but fail safe).
                    break

                read_pos = self._readers[reader_id]
                available = self._write_pos - read_pos
                if available <= 0:
                    if not self._lock.wait(timeout=timeout):
                        break
                    continue

                take = min(available, num_bytes - len(out))
                start = read_pos % self.capacity_bytes
                end = start + take
                if end <= self.capacity_bytes:
                    out += self._buf[start:end]
                else:
                    first_len = self.capacity_bytes - start
                    out += self._buf[start:]
                    out += self._buf[: take - first_len]

                self._readers[reader_id] = read_pos + take
            return bytes(out)

    def set_read_offset_bytes(self, offset_bytes: int) -> None:
        """Move every active reader's pointer to (write_pos - offset_bytes),
        clamped to what the buffer actually still holds, and remember this
        offset so readers opened later start at the same place."""
        with self._lock:
            self._current_delay_bytes = offset_bytes
            min_read_pos = self._write_pos - self.capacity_bytes
            max_read_pos = self._write_pos
            target = self._write_pos - offset_bytes
            clamped = max(min_read_pos, min(target, max_read_pos))
            for reader_id in self._readers:
                self._readers[reader_id] = clamped
            self._lock.notify_all()

    def fill_seconds(self, reader_id: int | None = None) -> float:
        """Fill for a specific reader, or the most-behind active reader if
        none given (used for the general buffer_fill metric)."""
        with self._lock:
            if reader_id is not None:
                if reader_id not in self._readers:
                    return 0.0
                available = self._write_pos - self._readers[reader_id]
            elif self._readers:
                oldest = min(self._readers.values())
                available = self._write_pos - oldest
            else:
                available = 0
            return available / (self.sample_rate * FRAME_SIZE)

    def fill_ratio(self, reader_id: int | None = None) -> float:
        with self._lock:
            if reader_id is not None:
                if reader_id not in self._readers:
                    return 0.0
                available = self._write_pos - self._readers[reader_id]
            elif self._readers:
                oldest = min(self._readers.values())
                available = self._write_pos - oldest
            else:
                available = 0
            return max(0.0, min(1.0, available / self.capacity_bytes))

    @property
    def active_reader_count(self) -> int:
        with self._lock:
            return len(self._readers)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._lock.notify_all()
