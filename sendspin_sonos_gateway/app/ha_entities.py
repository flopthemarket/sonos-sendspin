"""
Home Assistant Entities

Publishes the entities called for in the design doc using MQTT discovery:
    number.sendspin_delay
    sensor.sendspin_buffer_fill
    sensor.sendspin_latency
    sensor.sendspin_stream_state
    button.sendspin_reconnect
    switch.sendspin_bridge_enabled

MQTT discovery (rather than the plain REST "set a state" API) is used
because it's the standard, correct way for a *Supervisor add-on* (as
opposed to a custom `homeassistant/components/...` integration) to create
real, interactive entities - a REST state push can't give you an editable
number slider or a button that calls back into this process.

Broker connection details are pulled from the Supervisor's `mqtt` service
(available because config.yaml declares `services: ["mqtt:want"]`), with
env-var fallbacks for standalone/dev use.
"""
from __future__ import annotations

import json
import logging
import os
import threading

import requests
import paho.mqtt.client as mqtt

log = logging.getLogger("ssg.ha_entities")

DISCOVERY_PREFIX = "homeassistant"
NODE_ID = "sendspin_sonos_gateway"
DEVICE_INFO = {
    "identifiers": [NODE_ID],
    "name": "Sendspin Sonos Gateway",
    "manufacturer": "sendspin-sonos-gateway",
    "model": "Gateway",
}


def _get_mqtt_service_info(supervisor_token: str) -> dict:
    """Ask the Supervisor for the auto-discovered MQTT broker's connection
    details. Falls back to MQTT_* env vars (useful outside HA)."""
    if supervisor_token:
        try:
            resp = requests.get(
                "http://supervisor/services/mqtt",
                headers={"Authorization": f"Bearer {supervisor_token}"},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
            if data:
                return {
                    "host": data.get("host", "core-mosquitto"),
                    "port": int(data.get("port", 1883)),
                    "username": data.get("username", ""),
                    "password": data.get("password", ""),
                }
        except requests.RequestException as exc:
            log.warning("Could not fetch MQTT service info from Supervisor: %s", exc)

    return {
        "host": os.environ.get("MQTT_HOST", "localhost"),
        "port": int(os.environ.get("MQTT_PORT", "1883")),
        "username": os.environ.get("MQTT_USERNAME", ""),
        "password": os.environ.get("MQTT_PASSWORD", ""),
    }


class HAEntities:
    def __init__(self, supervisor_token: str, on_delay_command=None,
                 on_reconnect_command=None, on_bridge_enabled_command=None):
        self._on_delay_command = on_delay_command
        self._on_reconnect_command = on_reconnect_command
        self._on_bridge_enabled_command = on_bridge_enabled_command

        broker = _get_mqtt_service_info(supervisor_token)
        self._client = mqtt.Client(client_id=NODE_ID)
        if broker["username"]:
            self._client.username_pw_set(broker["username"], broker["password"])
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

        self._broker_host = broker["host"]
        self._broker_port = broker["port"]
        self._connect_lock = threading.Lock()

    # ------------------------------------------------------------ setup --
    def start(self) -> None:
        try:
            self._client.connect_async(self._broker_host, self._broker_port, keepalive=60)
            self._client.loop_start()
            log.info("Connecting to MQTT broker %s:%s", self._broker_host, self._broker_port)
        except OSError as exc:
            log.warning("MQTT connection failed (%s); HA entities will not be available", exc)

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc != 0:
            log.warning("MQTT connect failed with code %s", rc)
            return
        log.info("MQTT connected; publishing discovery configs")
        self._publish_discovery()
        client.subscribe(f"{NODE_ID}/number/delay/set")
        client.subscribe(f"{NODE_ID}/button/reconnect/press")
        client.subscribe(f"{NODE_ID}/switch/bridge_enabled/set")

    def _on_message(self, client, userdata, msg) -> None:
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="ignore")
        try:
            if topic.endswith("number/delay/set") and self._on_delay_command:
                self._on_delay_command(int(float(payload)))
            elif topic.endswith("button/reconnect/press") and self._on_reconnect_command:
                self._on_reconnect_command()
            elif topic.endswith("switch/bridge_enabled/set") and self._on_bridge_enabled_command:
                self._on_bridge_enabled_command(payload.upper() == "ON")
        except (ValueError, TypeError):
            log.warning("Bad payload on %s: %r", topic, payload)

    # ------------------------------------------------------- discovery --
    def _publish_discovery(self) -> None:
        base = f"{NODE_ID}"

        entities = [
            ("number", "delay", {
                "name": "Sendspin Delay",
                "unique_id": f"{NODE_ID}_delay",
                "command_topic": f"{base}/number/delay/set",
                "state_topic": f"{base}/number/delay/state",
                "min": 0, "max": 5000, "step": 10,
                "unit_of_measurement": "ms",
                "mode": "slider",
                "device": DEVICE_INFO,
            }),
            ("sensor", "buffer_fill", {
                "name": "Sendspin Buffer Fill",
                "unique_id": f"{NODE_ID}_buffer_fill",
                "state_topic": f"{base}/sensor/buffer_fill/state",
                "unit_of_measurement": "s",
                "device": DEVICE_INFO,
            }),
            ("sensor", "latency", {
                "name": "Sendspin Latency",
                "unique_id": f"{NODE_ID}_latency",
                "state_topic": f"{base}/sensor/latency/state",
                "json_attributes_topic": f"{base}/sensor/latency/attributes",
                "unit_of_measurement": "ms",
                "device": DEVICE_INFO,
            }),
            ("sensor", "stream_state", {
                "name": "Sendspin Stream State",
                "unique_id": f"{NODE_ID}_stream_state",
                "state_topic": f"{base}/sensor/stream_state/state",
                "device": DEVICE_INFO,
            }),
            ("button", "reconnect", {
                "name": "Sendspin Reconnect",
                "unique_id": f"{NODE_ID}_reconnect",
                "command_topic": f"{base}/button/reconnect/press",
                "device": DEVICE_INFO,
            }),
            ("switch", "bridge_enabled", {
                "name": "Sendspin Bridge Enabled",
                "unique_id": f"{NODE_ID}_bridge_enabled",
                "command_topic": f"{base}/switch/bridge_enabled/set",
                "state_topic": f"{base}/switch/bridge_enabled/state",
                "device": DEVICE_INFO,
            }),
        ]

        for component, object_id, config in entities:
            topic = f"{DISCOVERY_PREFIX}/{component}/{NODE_ID}/{object_id}/config"
            self._client.publish(topic, json.dumps(config), retain=True)

    # -------------------------------------------------------- updating --
    def publish_delay(self, delay_ms: int) -> None:
        self._client.publish(f"{NODE_ID}/number/delay/state", str(delay_ms), retain=True)

    def publish_latency_snapshot(self, snapshot: dict) -> None:
        self._client.publish(
            f"{NODE_ID}/sensor/latency/state",
            str(snapshot.get("gateway_delay_ms", 0)),
            retain=True,
        )
        self._client.publish(
            f"{NODE_ID}/sensor/latency/attributes", json.dumps(snapshot), retain=True,
        )
        self._client.publish(
            f"{NODE_ID}/sensor/buffer_fill/state",
            str(snapshot.get("buffer_fill_seconds", 0)),
            retain=True,
        )

    def publish_stream_state(self, state: str) -> None:
        self._client.publish(f"{NODE_ID}/sensor/stream_state/state", state, retain=True)

    def publish_bridge_enabled(self, enabled: bool) -> None:
        self._client.publish(
            f"{NODE_ID}/switch/bridge_enabled/state", "ON" if enabled else "OFF", retain=True,
        )
