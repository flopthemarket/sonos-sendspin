"""
Sendspin Client using the official aiosendspin library.

This implementation replaces the TCP-based placeholder with a proper
Sendspin protocol client that uses WebSockets with Noise encryption,
as defined in the Sendspin specification.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from aiosendspin.client import SendspinClient as AiospinClient
from aiosendspin.models.core import StreamStartMessage, StreamEndMessage, ServerTimePayload
from aiosendspin.client import AudioFormat
from ring_buffer import RingBuffer

log = logging.getLogger("ssg.sendspin_client")

RECONNECT_DELAY_S = 3


class ClockSync:
    """Tracks the offset between the Sendspin server's clock and ours so
    that presentation timestamps can eventually be used for drift
    correction (see latency_tracker.py for the auto-calibration hook)."""

    def __init__(self):
        self.offset_ns: int = 0
        self.last_sync_monotonic: float = 0.0

    def update(self, server_time_ns: int) -> None:
        local_ns = asyncio.get_event_loop().time() * 1_000_000_000  # Convert to nanoseconds
        self.offset_ns = server_time_ns - local_ns
        self.last_sync_monotonic = asyncio.get_event_loop().time()

    def to_local_ns(self, server_time_ns: int) -> int:
        return server_time_ns - self.offset_ns


class SendspinClient:
    """Sendspin client that uses the official aiosendspin library for
    WebSocket connections with Noise encryption."""

    def __init__(
        self,
        host: str,
        port: int,
        player_name: str,
        ring_buffer: RingBuffer,
    ):
        self.host = host
        self.port = port
        self.player_name = player_name
        self.ring_buffer = ring_buffer
        self.clock = ClockSync()

        self._client: Optional[AiospinClient] = None
        self._connected = asyncio.Event()
        self._stop = asyncio.Event()
        self._stream_info: dict = {}
        self._audio_format: Optional[AudioFormat] = None

    @property
    def connected(self) -> bool:
        # aiosendspin client has a connected property we can use
        if self._client is not None:
            return self._client.connected
        return False

    async def wait_until_connected(self, timeout: float | None = None) -> bool:
        # Wait until we're connected by polling the client's connected property
        try:
            # If we don't have a client yet, we can't be connected
            if self._client is None:
                return False

            # Wait for the client to report connected
            await asyncio.wait_for(
                self._wait_for_client_connected(),
                timeout=timeout
            )
            return True
        except (asyncio.TimeoutError, AttributeError):
            return False

    async def _wait_for_client_connected(self) -> None:
        """Wait for the underlying client to report connected."""
        while self._client is not None and not self._client.connected:
            await asyncio.sleep(0.1)

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
            except (ConnectionError, OSError) as exc:
                log.warning("Sendspin connection lost (%s); retrying in %ss", exc, RECONNECT_DELAY_S)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Unexpected error in Sendspin client loop")
            finally:
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

        # Create the aiosendspin client
        self._client = AiospinClient(
            client_id="",  # Will be generated
            client_name=self.player_name,
            roles=["player"],  # We're a player role
            device_info={},  # Could be enhanced with actual device info
            player_support={},  # Could be enhanced
            initial_volume=100,
            initial_muted=False,
        )

        # Set up event handlers
        self._client.add_audio_chunk_listener(self._on_audio_chunk)
        self._client.add_stream_start_listener(self._on_stream_start)
        self._client.add_stream_end_listener(self._on_stream_end)
        self._client.add_server_command_listener(self._on_server_command)  # Handle all server commands
        self._client.add_disconnect_listener(self._on_disconnected)
        self._client.add_connect_listener(self._on_connected)

        # Connect to the server
        url = f"ws://{self.host}:{self.port}/sendspin"
        await self._client.connect(url)

        # Wait for connection to be established (we'll check in wait_until_connected)
        log.info("Registered with Sendspin as player '%s'", self.player_name)

        # Keep the connection alive until stopped
        await self._stop.wait()

    def _on_server_command(self, message) -> None:
        """Handle server command messages from the server."""
        # Handle server time synchronization
        if hasattr(message, 'payload') and isinstance(message.payload, ServerTimePayload):
            server_time_ns = message.payload.server_transmitted
            log.debug("Received server time: %d ns", server_time_ns)
            self.clock.update(server_time_ns)
        # Other server commands can be handled here if needed

    def _on_stream_start(self, message: StreamStartMessage) -> None:
        """Handle stream start messages from the server."""
        log.info("Stream started: %s", message)
        if hasattr(message, 'payload'):
            payload = message.payload
            # The payload should be a StreamStartPayload
            if hasattr(payload, 'player') and payload.player:
                self._audio_format = payload.player
                log.info(
                    "Audio format: %s %dHz %d-bit %dch",
                    payload.player.codec.value if payload.player.codec else "unknown",
                    payload.player.sample_rate,
                    payload.player.bit_depth,
                    payload.player.channels,
                )
            self._stream_info = {
                "sample_rate": payload.player.sample_rate if payload.player else 48000,
                "channels": payload.player.channels if payload.player else 2,
                "bits": payload.player.bit_depth if payload.player else 16,
            }

    def _on_stream_end(self, message: StreamEndMessage) -> None:
        """Handle stream end messages from the server."""
        log.info("Stream ended")
        self._stream_info = {}
        self._audio_format = None

    def _on_audio_chunk(self, timestamp_ns: int, audio_data: bytes, fmt: AudioFormat) -> None:
        """Handle incoming audio chunks from the server."""
        # Convert timestamp from nanoseconds to local time using our clock sync
        local_timestamp_ns = self.clock.to_local_ns(timestamp_ns)

        # Write the PCM data to the ring buffer
        self.ring_buffer.write(audio_data)

        log.debug(
            "Received audio chunk: %d bytes at timestamp %d ns (local: %d ns)",
            len(audio_data),
            timestamp_ns,
            local_timestamp_ns,
        )

    def _on_connected(self) -> None:
        """Called when the client successfully connects."""
        log.info("Connected to Sendspin server")
        self._connected.set()

    def _on_disconnected(self) -> None:
        """Called when the client disconnects."""
        log.info("Disconnected from Sendspin server")
        self._connected.clear()

    # Properties to maintain compatibility with the original interface
    @property
    def stream_info(self) -> dict:
        return self._stream_info

    def _handle_audio_frame(self, presentation_ts_ns: int, pcm_bytes: bytes) -> None:
        """Compatibility method for the original interface.

        Note: With aiosendspin, we receive audio chunks directly via the
        audio_chunk callback, so this method is not used in the new implementation.
        However, we keep it for compatibility with any existing code that might
        call it directly.
        """
        self.ring_buffer.write(pcm_bytes)

    async def _handle_control_message(self, msg: dict) -> None:
        """Compatibility method for the original interface.

        Note: With aiosendspin, control messages are handled via the event system,
        so this method is not used in the new implementation.
        """
        msg_type = msg.get("type")
        if msg_type == "welcome":
            log.info("Sendspin welcome: player_id=%s", msg.get("player_id"))
        elif msg_type == "clock_sync":
            server_time_ns = msg.get("server_time_ns")
            if server_time_ns is not None:
                self.clock.update(server_time_ns)
        elif msg_type == "stream_start":
            log.info(
                "Stream format: %sHz %s-bit %s channel(s)",
                msg.get("sample_rate"), msg.get("bits"), msg.get("channels"),
            )
        elif msg_type == "stream_end":
            log.info("Stream ended")
        elif msg_type == "server_time":
            server_time_ns = msg.get("server_transmitted")
            if server_time_ns is not None:
                self.clock.update(server_time_ns)
        else:
            log.debug("Unhandled Sendspin control message: %s", msg)