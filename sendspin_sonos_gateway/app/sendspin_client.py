"""
Sendspin Client using the official aiosendspin library.

Verified against the actual aiosendspin==6.0.1 API installed in this
project's requirements.txt (not guessed) - see the notes inline for the
specific things that were wrong before and why.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Optional

from aiosendspin.client import SendspinClient as _AioSendspinClient
from aiosendspin.client import AudioFormat as AioAudioFormat
from aiosendspin.models.core import DeviceInfo, StreamStartMessage
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, Roles

from ring_buffer import RingBuffer, SAMPLE_RATE, CHANNELS

log = logging.getLogger("ssg.sendspin_client")

RECONNECT_DELAY_S = 3
DATA_DIR = Path("/data") if Path("/data").is_dir() else Path("/tmp/sendspin-sonos-gateway-data")
CLIENT_ID_FILE = DATA_DIR / "sendspin_client_id.txt"

# We only ever advertise raw PCM support. This means the server will never
# send us compressed audio, so every audio chunk handed to the callback is
# already exactly what RingBuffer expects - no decoding step needed.
PLAYER_AUDIO_FORMAT = SupportedAudioFormat(
    codec=AudioCodec.PCM, channels=CHANNELS, sample_rate=SAMPLE_RATE, bit_depth=16,
)


def _load_or_create_client_id() -> str:
    """A stable client_id (persisted across restarts) so the server sees
    this gateway as the same client each time rather than a new one."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if CLIENT_ID_FILE.is_file():
        existing = CLIENT_ID_FILE.read_text().strip()
        if existing:
            return existing
    new_id = str(uuid.uuid4())
    CLIENT_ID_FILE.write_text(new_id)
    return new_id


class SendspinClient:
    """Thin wrapper around aiosendspin.SendspinClient exposing just what the
    gateway needs: connect/reconnect lifecycle + a feed into RingBuffer."""

    def __init__(self, host: str, port: int, player_name: str, ring_buffer: RingBuffer):
        self.host = host
        self.port = port
        self.player_name = player_name
        self.ring_buffer = ring_buffer
        self.client_id = _load_or_create_client_id()

        self._client: Optional[_AioSendspinClient] = None
        self._connected = asyncio.Event()
        self._audio_started = asyncio.Event()
        self._stop = asyncio.Event()
        self._stream_info: dict = {}

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.connected

    async def wait_until_connected(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_until_streaming(self, timeout: float | None = None) -> bool:
        """Wait until real PCM audio has actually started arriving from the
        Sendspin server - NOT just until the handshake completed. There can
        be an arbitrary delay between "player registered" and "Music
        Assistant actually starts sending audio" (e.g. nothing is playing
        yet), and Sonos's own HTTP client gives up on a stream URI that
        stays silent too long after being told to play it. Callers should
        wait for this before pointing Sonos at the stream, not just for
        wait_until_connected()."""
        try:
            await asyncio.wait_for(self._audio_started.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def run(self) -> None:
        """Long-running task: connect, register, stream audio, reconnect on
        failure. Intended to be created with asyncio.create_task()."""
        while not self._stop.is_set():
            if not self.host:
                log.warning("No sendspin_host configured; client idle.")
                await asyncio.sleep(RECONNECT_DELAY_S)
                continue
            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                raise
            except (TimeoutError, ConnectionError, OSError) as exc:
                log.warning("Sendspin connection lost (%s); retrying in %ss", exc, RECONNECT_DELAY_S)
            except Exception:
                log.exception("Unexpected error in Sendspin client loop")
            finally:
                self._connected.clear()
                await self._close()
            if not self._stop.is_set():
                await asyncio.sleep(RECONNECT_DELAY_S)

    async def stop(self) -> None:
        self._stop.set()
        await self._close()

    async def _close(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        self._client = None

    async def _connect_and_stream(self) -> None:
        log.info("Connecting to Sendspin at %s:%s", self.host, self.port)

        self._client = _AioSendspinClient(
            client_id=self.client_id,
            client_name=self.player_name,
            roles=[Roles.PLAYER],
            device_info=DeviceInfo(
                product_name="Sendspin Sonos Gateway",
                manufacturer="sendspin-sonos-gateway",
            ),
            player_support=ClientHelloPlayerSupport(
                supported_formats=[PLAYER_AUDIO_FORMAT],
                buffer_capacity=self.ring_buffer.capacity_bytes,
                supported_commands=[],
            ),
            initial_volume=100,
            initial_muted=False,
        )

        self._client.add_audio_chunk_listener(self._on_audio_chunk)
        self._client.add_stream_start_listener(self._on_stream_start)
        self._client.add_stream_end_listener(self._on_stream_end)
        self._client.add_disconnect_listener(self._on_disconnected)

        url = f"ws://{self.host}:{self.port}/sendspin"
        # connect() performs the full client/hello <-> server/hello handshake
        # itself and only returns once it's complete (or raises TimeoutError
        # after 10s) - so if this doesn't raise, we're genuinely connected
        # and admitted, no separate "wait for connected" step needed.
        await self._client.connect(url)
        self._connected.set()
        log.info(
            "Registered with Sendspin as player '%s' (client_id=%s)",
            self.player_name, self.client_id,
        )

        # Idle here for the lifetime of the connection; aiosendspin drives
        # the reader and time-sync loops in the background and invokes our
        # listeners as audio/events arrive.
        while not self._stop.is_set() and self._client.connected:
            await asyncio.sleep(1.0)

    def _on_audio_chunk(self, timestamp_us: int, audio_data: bytes, fmt: AioAudioFormat) -> None:
        """Feed raw PCM straight into the ring buffer. `timestamp_us` is the
        server's presentation timestamp in microseconds; since Sonos (not
        us) does the actual playback timing, we don't need it for anything
        beyond optional drift logging - see LatencyTracker."""
        if fmt.codec is not AudioCodec.PCM:
            log.warning("Received unexpected codec %s from Sendspin server; dropping chunk", fmt.codec)
            return
        self.ring_buffer.write(audio_data)
        if not self._audio_started.is_set():
            self._audio_started.set()

    def _on_stream_start(self, message: StreamStartMessage) -> None:
        payload = getattr(message, "payload", None)
        player_fmt = getattr(payload, "player", None) if payload is not None else None
        if player_fmt is not None:
            self._stream_info = {
                "codec": player_fmt.codec.value if player_fmt.codec else "unknown",
                "sample_rate": player_fmt.sample_rate,
                "channels": player_fmt.channels,
                "bits": player_fmt.bit_depth,
            }
            log.info(
                "Stream started: %s %sHz %s-bit %sch",
                self._stream_info["codec"], self._stream_info["sample_rate"],
                self._stream_info["bits"], self._stream_info["channels"],
            )
        else:
            log.info("Stream started")

    def _on_stream_end(self, group_ids: list[str] | None) -> None:
        # NOTE: aiosendspin's StreamEndCallback passes the list of group ids
        # whose stream ended (or None), not a message object - see
        # `StreamEndCallback` in aiosendspin.client.client.
        log.info("Stream ended (groups=%s)", group_ids)
        self._stream_info = {}

    def _on_disconnected(self) -> None:
        log.info("Disconnected from Sendspin server")
        self._connected.clear()
        self._audio_started.clear()

    @property
    def stream_info(self) -> dict:
        return self._stream_info

    def is_time_synchronized(self) -> bool:
        return self._client is not None and self._client.is_time_synchronized()
