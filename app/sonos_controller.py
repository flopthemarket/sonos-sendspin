"""
Sonos Controller

Talks to Sonos over UPnP/SOAP via the `soco` library (SoCo). Responsible
for discovery, pointing the coordinator at the gateway's HTTP stream, basic
transport control, and detecting when a speaker disappears/reboots so it
can be reconnected automatically.
"""
from __future__ import annotations

import asyncio
import logging

import soco
from soco.exceptions import SoCoException, SoCoUPnPException

log = logging.getLogger("ssg.sonos_controller")

POLL_INTERVAL_S = 5


class SonosController:
    def __init__(self, sonos_ip: str, stream_url_factory):
        """
        sonos_ip: IP address of the target Sonos speaker (becomes the
            coordinator if it's grouped with others).
        stream_url_factory: callable(host: str) -> str, used to build the
            stream URL once we know our own reachable address.
        """
        self.sonos_ip = sonos_ip
        self._stream_url_factory = stream_url_factory
        self._device: soco.SoCo | None = None
        self._connected = asyncio.Event()
        self._stop = asyncio.Event()
        self.last_error: str | None = None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def discover_and_bind(self) -> bool:
        """Blocking: locate the ZonePlayer at sonos_ip (or via SSDP if no
        IP configured) and bind it as our target coordinator."""
        try:
            if self.sonos_ip:
                device = soco.SoCo(self.sonos_ip)
                _ = device.player_name  # forces a round-trip; raises if unreachable
            else:
                devices = soco.discover()
                if not devices:
                    self.last_error = "No Sonos speakers found via SSDP"
                    return False
                device = next(iter(devices))

            # If this device is grouped, always control the group's
            # coordinator - that's the only member that should receive
            # the stream URI; Sonos fans it out to the rest of the group.
            coordinator = device.group.coordinator if device.group else device
            self._device = coordinator
            self._connected.set()
            self.last_error = None
            log.info(
                "Bound to Sonos coordinator '%s' (%s)",
                coordinator.player_name, coordinator.ip_address,
            )
            return True
        except (SoCoException, OSError) as exc:
            self.last_error = str(exc)
            self._connected.clear()
            log.warning("Sonos discovery/bind failed: %s", exc)
            return False

    def play_stream(self, gateway_host: str) -> bool:
        if self._device is None:
            return False
        stream_url = self._stream_url_factory(gateway_host)
        try:
            self._device.play_uri(stream_url, title="Sendspin")
            log.info("Sonos coordinator now playing %s", stream_url)
            return True
        except (SoCoException, SoCoUPnPException) as exc:
            self.last_error = str(exc)
            log.warning("play_uri failed: %s", exc)
            self._connected.clear()
            return False

    def pause(self) -> None:
        self._safe_call(lambda d: d.pause())

    def stop(self) -> None:
        self._safe_call(lambda d: d.stop())

    def set_volume(self, volume: int) -> None:
        self._safe_call(lambda d: setattr(d, "volume", volume))

    def unjoin(self) -> None:
        self._safe_call(lambda d: d.unjoin())

    def group_members(self) -> list[str]:
        if self._device is None:
            return []
        try:
            return [m.player_name for m in self._device.group.members]
        except SoCoException:
            return []

    def is_visible(self) -> bool:
        if self._device is None:
            return False
        try:
            _ = self._device.player_name
            return True
        except (SoCoException, OSError):
            return False

    def _safe_call(self, fn) -> None:
        if self._device is None:
            return
        try:
            fn(self._device)
        except (SoCoException, SoCoUPnPException) as exc:
            log.warning("Sonos call failed: %s", exc)

    async def monitor_loop(self, gateway_host_getter, on_reconnect=None) -> None:
        """Background task: periodically verify the bound speaker is still
        visible on the network; rediscover and resume the stream if it
        rebooted or dropped off, per the Failure Handling section of the
        design doc."""
        while not self._stop.is_set():
            await asyncio.sleep(POLL_INTERVAL_S)
            if self._device is not None and self.is_visible():
                continue

            log.info("Sonos coordinator not visible - attempting rediscovery")
            loop = asyncio.get_event_loop()
            ok = await loop.run_in_executor(None, self.discover_and_bind)
            if ok:
                host = gateway_host_getter()
                await loop.run_in_executor(None, self.play_stream, host)
                if on_reconnect:
                    on_reconnect()

    def request_stop(self) -> None:
        self._stop.set()
