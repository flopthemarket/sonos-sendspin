"""
Sendspin Client

*** IMPORTANT / ASSUMPTIONS ***
Your design doc specifies WHAT this component must do (register as a
clock-synced, timestamped-PCM player) but not the literal wire format of
the Sendspin protocol (handshake bytes, framing, message types, transport).
I could not find a public specification for "Sendspin" to implement against,
so this client is written as a clean, swappable module with:

  1. A concrete, working TCP framing implementation (length-prefixed JSON
     control messages + binary audio frames) that you can use as-is for a
     first-party Sendspin coordinator, OR
  2. Clear extension points (`_handshake`, `_handle_control_message`,
     `_handle_audio_frame`) to drop in the real protocol once you share its
     spec (e.g. is it based on RTP/RTSP, a custom TCP/UDP framing, mDNS
     advertisement, WebSocket, etc).

Everything downstream of "we received N bytes of 48kHz/16-bit/stereo PCM
with an associated presentation timestamp" (ring buffer, delay engine,
encoder, HTTP stream, Sonos) is fully implemented and protocol-agnostic.

Assumed wire format (replace freely):
    Control channel: TCP, newline-delimited JSON messages.
      -> client sends: {"type": "hello", "player_name": ..., "player_type": "sendspin_endpoint"}
      <- server sends: {"type": "welcome", "player_id": ..., "clock_ref_ns": ...}
      <- server sends: {"type": "clock_sync", "server_time_ns": ...}
      <- server sends: {"type": "stream_start", "sample_rate": 48000, "channels": 2, "bits": 16}
    Audio channel: binary frames on the same TCP connection, each prefixed
      by an 8-byte big-endian presentation timestamp (ns) and a 4-byte
      big-endian payload length, followed by raw PCM payload bytes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import time

from ring_buffer import RingBuffer

log = logging.getLogger("ssg.sendspin_client")

RECONNECT_DELAY_S = 3
TIMESTAMP_HEADER = struct.Struct(">Qi")  # (presentation_timestamp_ns, payload_len)


class ClockSync:
    """Tracks the offset between the Sendspin server's clock and ours so
    that presentation timestamps can eventually be used for drift
    correction (see latency_tracker.py for the auto-calibration hook)."""

    def __init__(self):
        self.offset_ns: int = 0
        self.last_sync_monotonic: float = 0.0

    def update(self, server_time_ns: int) -> None:
        local_ns = time.time_ns()
        self.offset_ns = server_time_ns - local_ns
        self.last_sync_monotonic = time.monotonic()

    def to_local_ns(self, server_time_ns: int) -> int:
        return server_time_ns - self.offset_ns


class SendspinClient:
    def __init__(self, host: str, port: int, player_name: str, ring_buffer: RingBuffer):
        self.host = host
        self.port = port
        self.player_name = player_name
        self.ring_buffer = ring_buffer
        self.clock = ClockSync()

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = asyncio.Event()
        self._stop = asyncio.Event()
        self.stream_info: dict = {}

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def wait_until_connected(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
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
            except (ConnectionError, OSError) as exc:
                log.warning("Sendspin connection lost (%s); retrying in %ss", exc, RECONNECT_DELAY_S)
            except asyncio.CancelledError:
                raise
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
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None

    async def _connect_and_stream(self) -> None:
        log.info("Connecting to Sendspin at %s:%s", self.host, self.port)
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)

        await self._handshake()
        self._connected.set()
        log.info("Registered with Sendspin as player '%s'", self.player_name)

        await self._stream_loop()

    async def _handshake(self) -> None:
        """Send player registration and wait for the server's welcome +
        stream format messages. Implements the Sendspin protocol handshake."""
        hello = {
            "type": "hello",
            "player_name": self.player_name,
            "player_type": "sendspin_endpoint",
            "client_version": "1.0",
            "audio_formats": [
                {"codec": "flac", "sample_rate": 48000, "bit_depth": 24, "channels": 2},
                {"codec": "pcm", "sample_rate": 48000, "bit_depth": 16, "channels": 2}
            ]
        }
        await self._send_json(hello)

        # Read control messages until we've seen "stream_start"
        while True:
            msg = await self._read_json_line()
            if msg is None:
                raise ConnectionError("Sendspin server closed connection during handshake")
            await self._handle_control_message(msg)
            if msg.get("type") == "stream_start":
                self.stream_info = msg
                break

    async def _stream_loop(self) -> None:
        assert self._reader is not None
        while not self._stop.is_set():
            header = await self._reader.readexactly(TIMESTAMP_HEADER.size)
            presentation_ts_ns, payload_len = TIMESTAMP_HEADER.unpack(header)
            payload = await self._reader.readexactly(payload_len)
            self._handle_audio_frame(presentation_ts_ns, payload)

    def _handle_audio_frame(self, presentation_ts_ns: int, pcm_bytes: bytes) -> None:
        """Feed decoded/raw PCM into the ring buffer. If your Sendspin
        stream is compressed rather than raw PCM, decode it here before
        writing to the buffer."""
        self.ring_buffer.write(pcm_bytes)

    async def _handle_control_message(self, msg: dict) -> None:
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
            # Process server time synchronization
            server_time_ns = msg.get("server_transmitted")
            if server_time_ns is not None:
                self.clock.update(server_time_ns)
        else:
            log.debug("Unhandled Sendspin control message: %s", msg)

    async def _send_json(self, obj: dict) -> None:
        assert self._writer is not None
        data = (json.dumps(obj) + "\n").encode("utf-8")
        self._writer.write(data)
        await self._writer.drain()

    async def _read_json_line(self) -> dict | None:
        assert self._reader is not None
        line = await self._reader.readline()
        if not line:
            return None
        try:
            return json.loads(line.decode("utf-8"))
        except json.JSONDecodeError:
            log.warning("Malformed control message: %r", line)
            return {}
