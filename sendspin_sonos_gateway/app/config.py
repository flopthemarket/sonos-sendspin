"""
Loads add-on configuration from /data/options.json (the standard location
Home Assistant Supervisor writes user-configured options to), falling back
to environment variables and then hard defaults so the gateway can also be
run standalone (e.g. `python3 main.py`) for local development.
"""
from __future__ import annotations

import json
import os
import logging
from dataclasses import dataclass, field

log = logging.getLogger("ssg.config")

DEFAULTS = {
    "sonos_ip": "",
    "sendspin_host": "",
    "sendspin_port": 4400,
    "player_name": "Living Room Sonos",
    "delay_ms": 1800,
    "buffer_ms": 5000,
    "stream_format": "flac",
    "log_level": "info",
}


@dataclass
class Config:
    sonos_ip: str = ""
    sendspin_host: str = ""
    sendspin_port: int = 4400
    player_name: str = "Living Room Sonos"
    delay_ms: int = 1800
    buffer_ms: int = 5000
    stream_format: str = "flac"
    log_level: str = "info"

    # Derived / environment-provided, not part of the user-facing schema
    stream_port: int = 8099
    supervisor_token: str = field(default_factory=lambda: os.environ.get("SUPERVISOR_TOKEN", ""))
    ha_api_base: str = "http://supervisor/core/api"

    @classmethod
    def load(cls) -> "Config":
        data = dict(DEFAULTS)

        options_path = os.environ.get("SSG_OPTIONS_FILE", "/data/options.json")
        if os.path.isfile(options_path):
            try:
                with open(options_path, "r", encoding="utf-8") as f:
                    file_data = json.load(f)
                data.update({k: v for k, v in file_data.items() if v is not None})
                log.info("Loaded options from %s", options_path)
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Could not read %s (%s); using defaults/env", options_path, exc)
        else:
            # Standalone/dev fallback: SSG_<OPTION_NAME> env vars
            for key in DEFAULTS:
                env_key = f"SSG_{key.upper()}"
                if env_key in os.environ:
                    raw = os.environ[env_key]
                    data[key] = type(DEFAULTS[key])(raw) if not isinstance(DEFAULTS[key], str) else raw

        cfg = cls(
            sonos_ip=data["sonos_ip"],
            sendspin_host=data["sendspin_host"],
            sendspin_port=int(data["sendspin_port"]),
            player_name=data["player_name"],
            delay_ms=int(data["delay_ms"]),
            buffer_ms=int(data["buffer_ms"]),
            stream_format=data["stream_format"],
            log_level=data["log_level"],
        )
        return cfg
