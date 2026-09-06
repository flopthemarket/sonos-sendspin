"""
HTTP Streaming Server

Exposes the live encoded audio at:
    http://<gateway>:8099/sendspin.<ext>

Sonos is pointed at this URL via AVTransport's play_uri and simply treats
it like an internet radio station - it has no idea a ring buffer and delay
engine are sitting behind it, which is exactly the point.
"""
from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from audio_pipeline import AudioEncoder
from ring_buffer import RingBuffer
from calibration import render_page
from calibration_tone import get_wav_bytes as get_calibration_tone_bytes

log = logging.getLogger("ssg.stream_server")


class StreamServer:
    def __init__(self, ring_buffer: RingBuffer, stream_format: str = "flac", port: int = 8099,
                 get_delay=None, set_delay=None):
        self.ring_buffer = ring_buffer
        self.stream_format = stream_format
        self.port = port
        # Callbacks into DelayEngine, wired from main.py, so the web-based
        # calibration page (/calibrate, /api/delay) can read/adjust the
        # live delay the same way the HA number entity does.
        self._get_delay = get_delay
        self._set_delay = set_delay
        self._app = web.Application()
        # allow_head=False: aiohttp's add_get() auto-registers a HEAD route
        # pointing at the SAME handler by default, which would spin up a
        # full ffmpeg pipeline (and contend for the single-listener slot
        # below) just to answer Sonos's routine "is this URL valid" HEAD
        # probe. Route HEAD to its own lightweight handler instead.
        self._app.router.add_get(f"/sendspin.{stream_format}", self._handle_stream, allow_head=False)
        self._app.router.add_head(f"/sendspin.{stream_format}", self._handle_head)
        self._app.router.add_get("/healthz", self._handle_health)
        self._app.router.add_get("/calibrate", self._handle_calibrate_page)
        self._app.router.add_get("/calibration-tone.wav", self._handle_calibration_tone)
        self._app.router.add_get("/api/delay", self._handle_get_delay)
        self._app.router.add_post("/api/delay", self._handle_set_delay)
        self._runner: web.AppRunner | None = None
        self.active_listeners = 0
        self._current_task: asyncio.Task | None = None

    @property
    def stream_path(self) -> str:
        return f"/sendspin.{self.stream_format}"

    def url_for(self, host: str) -> str:
        return f"http://{host}:{self.port}{self.stream_path}"

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()
        log.info("Stream server listening on 0.0.0.0:%s%s", self.port, self.stream_path)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "buffer_fill_seconds": round(self.ring_buffer.fill_seconds(), 2),
            "active_listeners": self.active_listeners,
        })

    async def _handle_head(self, request: web.Request) -> web.Response:
        """Answer HEAD probes (Sonos sends one before GET-ing the actual
        stream) instantly with just headers - no ffmpeg, no ring buffer
        reader, no participation in single-listener eviction."""
        log.debug("HEAD probe from %s", request.remote)
        return web.Response(
            status=200,
            headers={
                "Content-Type": CONTENT_TYPE_FOR.get(self.stream_format, "application/octet-stream"),
                "Cache-Control": "no-cache, no-store",
            },
        )

    async def _handle_calibrate_page(self, request: web.Request) -> web.Response:
        return web.Response(text=render_page(), content_type="text/html")

    async def _handle_calibration_tone(self, request: web.Request) -> web.Response:
        return web.Response(body=get_calibration_tone_bytes(), content_type="audio/wav")

    async def _handle_get_delay(self, request: web.Request) -> web.Response:
        delay_ms = self._get_delay() if self._get_delay else 0
        return web.json_response({"delay_ms": delay_ms})

    async def _handle_set_delay(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            delay_ms = int(body["delay_ms"])
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "expected JSON body {\"delay_ms\": <int>}"}, status=400)

        if self._set_delay is None:
            return web.json_response({"error": "delay control not wired up"}, status=503)

        applied = self._set_delay(delay_ms)
        return web.json_response({"delay_ms": applied})

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        # RingBuffer now gives every connection its own independent reader
        # cursor (see ring_buffer.py), so concurrent connections are safe
        # and no longer corrupt each other's audio. But Sonos only ever
        # needs ONE active connection to its coordinator's stream URI - a
        # second one showing up almost always means Sonos is retrying
        # because the first one appeared stuck or silent, not that it
        # genuinely wants two simultaneous streams. So we still close out
        # any previous connection when a new one arrives: it avoids piling
        # up ffmpeg processes forever, and gives Sonos a single, immediately
        # fresh connection to work with instead of leaving it split across
        # two half-alive ones.
        previous_task = self._current_task
        current_task = asyncio.current_task()
        self._current_task = current_task
        if previous_task is not None and not previous_task.done():
            log.info(
                "New stream connection from %s while a previous one was still "
                "open; closing the previous one (likely a Sonos retry/reconnect)",
                request.remote,
            )
            previous_task.cancel()

        log.info("Stream connection opened from %s", request.remote)
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": CONTENT_TYPE_FOR.get(self.stream_format, "application/octet-stream"),
                "Cache-Control": "no-cache, no-store",
                "Connection": "keep-alive",
            },
        )
        # Explicit, not just implied by omitting Content-Length: never send
        # a Content-Length for this live feed, and use chunked encoding, so
        # Sonos treats this as a live stream rather than a fixed-length
        # file it might try to pre-buffer/seek within.
        response.enable_chunked_encoding()
        await response.prepare(request)

        self.active_listeners += 1
        try:
            async with AudioEncoder(self.ring_buffer, self.stream_format) as encoder:
                while True:
                    chunk = await encoder.read_chunk(8192)
                    if not chunk:
                        break
                    try:
                        await response.write(chunk)
                    except (ConnectionResetError, asyncio.CancelledError):
                        break
        except asyncio.CancelledError:
            # We were the previous connection and just got superseded by a
            # newer one above - that's expected, not an error.
            pass
        finally:
            self.active_listeners -= 1
            if self._current_task is current_task:
                self._current_task = None
            log.info("Stream connection closed from %s", request.remote)

        return response


CONTENT_TYPE_FOR = {
    "flac": "audio/flac",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
}
