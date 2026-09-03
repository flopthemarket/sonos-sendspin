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

log = logging.getLogger("ssg.stream_server")


class StreamServer:
    def __init__(self, ring_buffer: RingBuffer, stream_format: str = "flac", port: int = 8099):
        self.ring_buffer = ring_buffer
        self.stream_format = stream_format
        self.port = port
        self._app = web.Application()
        self._app.router.add_get(f"/sendspin.{stream_format}", self._handle_stream)
        self._app.router.add_get("/healthz", self._handle_health)
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
