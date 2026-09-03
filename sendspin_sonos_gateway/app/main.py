"""
sendspin-sonos-gateway entrypoint.

Wires together:
  Sendspin Client -> RingBuffer -> DelayEngine -> StreamServer (ffmpeg) -> Sonos
                                                        ^
                                            HAEntities (MQTT discovery)
                                                        |
                                              LatencyTracker (metrics)
"""
from __future__ import annotations

import asyncio
import logging
import socket
import sys

from config import Config
from ring_buffer import RingBuffer
from delay_engine import DelayEngine
from stream_server import StreamServer
from sendspin_client import SendspinClient
from sonos_controller import SonosController
from ha_entities import HAEntities
from latency_tracker import LatencyTracker

log = logging.getLogger("ssg.main")

METRICS_PUBLISH_INTERVAL_S = 5


def _configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


def _local_ip_guess() -> str:
    """Best-effort discovery of an IP address Sonos (on the same LAN) can
    reach us at. With host_network: true this is the host's own address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        s.close()


class Gateway:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bridge_enabled = True

        self.ring_buffer = RingBuffer(capacity_seconds=cfg.buffer_ms / 1000)
        self.delay_engine = DelayEngine(self.ring_buffer, initial_delay_ms=cfg.delay_ms)
        self.stream_server = StreamServer(
            self.ring_buffer, stream_format=cfg.stream_format, port=cfg.stream_port,
        )
        self.sendspin_client = SendspinClient(
            host=cfg.sendspin_host,
            port=cfg.sendspin_port,
            player_name=cfg.player_name,
            ring_buffer=self.ring_buffer,
        )
        self.sonos = SonosController(
            sonos_ip=cfg.sonos_ip,
            stream_url_factory=self.stream_server.url_for,
        )
        self.latency_tracker = LatencyTracker(self.ring_buffer, self.delay_engine)
        self.ha = HAEntities(
            supervisor_token=cfg.supervisor_token,
            on_delay_command=self._on_delay_command,
            on_reconnect_command=self._on_reconnect_command,
            on_bridge_enabled_command=self._on_bridge_enabled_command,
            on_volume_command=self._on_volume_command,
        )

        self._gateway_host = _local_ip_guess()
        self._loop: asyncio.AbstractEventLoop | None = None

    # -------------------------------------------------------- HA callbacks --
    def _on_delay_command(self, delay_ms: int) -> None:
        applied = self.delay_engine.set_delay(delay_ms)
        self.ha.publish_delay(applied)

    def _on_reconnect_command(self) -> None:
        log.info("Manual reconnect requested via HA")
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._reconnect_sonos(), self._loop)

    def _on_bridge_enabled_command(self, enabled: bool) -> None:
        self.bridge_enabled = enabled
        self.ha.publish_bridge_enabled(enabled)
        log.info("Bridge %s via HA switch", "enabled" if enabled else "disabled")

    def _on_volume_command(self, volume: int) -> None:
        volume = max(0, min(100, volume))
        log.info("Setting Sonos volume to %d via HA", volume)
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._set_sonos_volume(volume), self._loop)

    async def _set_sonos_volume(self, volume: int) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.sonos.set_volume, volume)
        self.ha.publish_volume(volume)

    async def _reconnect_sonos(self) -> None:
        loop = asyncio.get_event_loop()
        ok = await loop.run_in_executor(None, self.sonos.discover_and_bind)
        if ok:
            await loop.run_in_executor(None, self.sonos.play_stream, self._gateway_host)

    # -------------------------------------------------------------- run --
    async def run(self) -> None:
        self._loop = asyncio.get_event_loop()
        self.ha.start()
        self.ha.publish_delay(self.delay_engine.get_delay())
        self.ha.publish_volume(100)
        self.ha.publish_bridge_enabled(self.bridge_enabled)
        self.ha.publish_stream_state("starting")

        await self.stream_server.start()

        tasks = [
            asyncio.create_task(self.sendspin_client.run(), name="sendspin_client"),
            asyncio.create_task(self._sonos_bootstrap(), name="sonos_bootstrap"),
            asyncio.create_task(
                self.sonos.monitor_loop(lambda: self._gateway_host,
                                         on_reconnect=lambda: self.ha.publish_stream_state("reconnected")),
                name="sonos_monitor",
            ),
            asyncio.create_task(self._metrics_loop(), name="metrics_loop"),
        ]

        try:
            await asyncio.gather(*tasks)
        finally:
            await self.stream_server.stop()
            await self.sendspin_client.stop()
            self.sonos.request_stop()
            self.ha.stop()

    async def _sonos_bootstrap(self) -> None:
        loop = asyncio.get_event_loop()
        log.info("Waiting for Sendspin connection before binding Sonos...")
        await self.sendspin_client.wait_until_connected(timeout=None)

        ok = await loop.run_in_executor(None, self.sonos.discover_and_bind)
        if not ok:
            self.ha.publish_stream_state(f"error: {self.sonos.last_error}")
            return

        ok = await loop.run_in_executor(None, self.sonos.play_stream, self._gateway_host)
        self.ha.publish_stream_state("streaming" if ok else f"error: {self.sonos.last_error}")

    async def _metrics_loop(self) -> None:
        while True:
            await asyncio.sleep(METRICS_PUBLISH_INTERVAL_S)
            snapshot = self.latency_tracker.snapshot()
            self.ha.publish_latency_snapshot(snapshot)


def main() -> None:
    cfg = Config.load()
    _configure_logging(cfg.log_level)
    log.info(
        "Starting sendspin-sonos-gateway: sonos_ip=%s sendspin=%s:%s format=%s delay=%sms buffer=%sms",
        cfg.sonos_ip or "(auto-discover)", cfg.sendspin_host or "(unset)",
        cfg.sendspin_port, cfg.stream_format, cfg.delay_ms, cfg.buffer_ms,
    )

    gateway = Gateway(cfg)
    try:
        asyncio.run(gateway.run())
    except KeyboardInterrupt:
        log.info("Shutting down")


if __name__ == "__main__":
    main()
