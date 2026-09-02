"""
Audio Pipeline

Wraps an ffmpeg subprocess that converts the raw PCM coming out of the
RingBuffer (already delay-adjusted by DelayEngine) into a container/codec
Sonos can consume over HTTP (FLAC, MP3, or WAV).

One AudioEncoder is spun up per active HTTP connection (see stream_server.py)
since ffmpeg's stdin is fed by a background thread reading from the shared
RingBuffer - multiple listeners simply get independent ffmpeg processes all
reading from the same buffer at (roughly) the same read pointer.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import threading

from ring_buffer import RingBuffer, SAMPLE_RATE, CHANNELS, FRAME_SIZE

log = logging.getLogger("ssg.audio_pipeline")

READ_CHUNK_FRAMES = 4096  # frames per read/write cycle
READ_CHUNK_BYTES = READ_CHUNK_FRAMES * FRAME_SIZE

FORMAT_ARGS = {
    "flac": ["-f", "flac", "-compression_level", "0"],
    "mp3": ["-f", "mp3", "-b:a", "256k"],
    "wav": ["-f", "wav"],
}

CONTENT_TYPES = {
    "flac": "audio/flac",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
}


class AudioEncoder:
    """Runs `ffmpeg` to transcode raw PCM (read live from the ring buffer)
    into a streamable format. Use as an async context manager."""

    def __init__(self, ring_buffer: RingBuffer, fmt: str = "flac"):
        if fmt not in FORMAT_ARGS:
            raise ValueError(f"Unsupported stream_format: {fmt}")
        self.ring_buffer = ring_buffer
        self.fmt = fmt
        self.content_type = CONTENT_TYPES[fmt]
        self._proc: subprocess.Popen | None = None
        self._feeder_thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        cmd = [
            "ffmpeg",
            "-hide_banner", "-loglevel", "warning",
            "-f", "s16le",
            "-ar", str(SAMPLE_RATE),
            "-ac", str(CHANNELS),
            "-i", "pipe:0",
            *FORMAT_ARGS[self.fmt],
            "pipe:1",
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self._feeder_thread = threading.Thread(target=self._feed_loop, daemon=True)
        self._feeder_thread.start()
        log.debug("Started ffmpeg encoder (%s)", self.fmt)

    def _feed_loop(self) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        try:
            while not self._stop.is_set():
                chunk = self.ring_buffer.read(READ_CHUNK_BYTES, timeout=1.0)
                if self._stop.is_set():
                    break
                if not chunk:
                    continue
                self._proc.stdin.write(chunk)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                self._proc.stdin.close()
            except Exception:
                pass

    async def read_chunk(self, size: int = 8192) -> bytes:
        """Async-friendly read of encoded output bytes (runs the blocking
        ffmpeg stdout read in a thread)."""
        assert self._proc is not None and self._proc.stdout is not None
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._proc.stdout.read, size)

    def stop(self) -> None:
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
        if self._feeder_thread is not None:
            self._feeder_thread.join(timeout=2.0)

    async def __aenter__(self) -> "AudioEncoder":
        self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.stop()
