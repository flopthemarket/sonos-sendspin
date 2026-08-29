# sendspin-sonos-gateway

Home Assistant add-on that acts as a virtual Sendspin player and forwards
audio to a Sonos coordinator, with a live, glitch-free delay slider.

## Status

Everything in the design doc is implemented **except the literal Sendspin
wire protocol**, which wasn't specified in enough detail to implement against
(handshake bytes, framing, transport). See the big docstring at the top of
`app/sendspin_client.py` for the assumed placeholder protocol and the exact
three methods to replace once you share the real spec:
`_handshake`, `_handle_control_message`, `_handle_audio_frame`.

Everything downstream of "PCM bytes arrived" is fully implemented and has
been tested standalone in this environment:

| Component | File | Tested |
|---|---|---|
| Ring buffer (jitter absorption, delay lookback) | `app/ring_buffer.py` | ✅ unit-tested pointer math |
| Delay engine (live slider → read-pointer offset) | `app/delay_engine.py` | ✅ unit-tested, confirms delay changes never require a reconnect |
| Audio pipeline (ffmpeg encode) | `app/audio_pipeline.py` | ✅ end-to-end with synthetic 440Hz tone |
| HTTP stream server | `app/stream_server.py` | ✅ served a live-encoded WAV stream over real HTTP, verified with aiohttp client |
| Sonos control (SoCo) | `app/sonos_controller.py` | ⚠️ not tested — no Sonos hardware in this environment. Logic follows SoCo's documented API (`discover`, `play_uri`, `group.coordinator`, `unjoin`, etc). |
| HA entities via MQTT discovery | `app/ha_entities.py` | ⚠️ not tested — no MQTT broker/HA instance in this environment |
| Sendspin client | `app/sendspin_client.py` | ⚠️ placeholder protocol only, see above |

## Running it for real

1. **Give me (or fill in yourself) the actual Sendspin protocol spec** —
   transport (TCP/UDP/WebSocket?), handshake/registration format, clock
   sync mechanism, and audio framing (raw PCM? compressed? timestamped how?).
   Everything else is ready to receive real audio the moment that's wired up.
2. Build and install as a local add-on:
   ```
   cp -r sendspin-sonos-gateway /addons/sendspin-sonos-gateway
   ```
   Home Assistant → Settings → Add-ons → Add-on Store → ⋮ → Check for updates,
   then install "Sendspin Sonos Gateway" from the Local add-ons section.
3. Set `sonos_ip`, `sendspin_host`, `player_name`, `delay_ms` in the add-on's
   Configuration tab.
4. Start the add-on. It will:
   - connect to your Sendspin source and register as `player_name`,
   - discover/bind to the Sonos speaker at `sonos_ip` (or via SSDP if blank),
   - point that speaker's `play_uri` at its own HTTP stream,
   - publish `number.sendspin_delay`, `sensor.sendspin_buffer_fill`,
     `sensor.sendspin_latency`, `sensor.sendspin_stream_state`,
     `button.sendspin_reconnect`, and `switch.sendspin_bridge_enabled` via
     MQTT discovery (requires the Mosquitto broker add-on, or any MQTT
     broker HA is configured to use).

## Local dev (no Home Assistant)

```
cd sendspin-sonos-gateway
pip install -r requirements.txt --break-system-packages
SSG_SONOS_IP=192.168.1.50 SSG_SENDSPIN_HOST=192.168.1.10 \
  PYTHONPATH=app python3 app/main.py
```

Without `/data/options.json` present, `app/config.py` falls back to
`SSG_<OPTION_NAME>` environment variables (see `DEFAULTS` in that file for
the full list), which is how the above works outside the Supervisor.

## Known gaps / v2 items (called out explicitly, not silently skipped)

- **Sendspin wire protocol** — see above.
- **Auto-calibration** (click + microphone offset measurement) — stubbed
  in `app/latency_tracker.py::run_auto_calibration`, raises
  `NotImplementedError` with a description of what real hardware/logic it
  needs, since there's no mic input in this container.
- **Sonos-side latency (Delay B)** is reported as the design doc's rough
  1500–3000ms estimate, not measured, since Sonos exposes no API for it.
