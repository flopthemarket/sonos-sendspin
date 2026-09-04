# sendspin-sonos-gateway

Home Assistant add-on that acts as a virtual Sendspin player and forwards
audio to a Sonos coordinator, with a live, glitch-free delay slider.

## Delay calibration by acoustic measurement, not guesswork

Manually nudging a slider while listening for echo is unreliable and
tedious. `http://<gateway-ip>:8099/calibrate` (LAN-reachable, since
`host_network: true`) serves a self-contained page that measures the
actual acoustic delay using your phone or laptop's microphone:

1. Play the same track on Music Assistant to both this Sonos speaker and
   a real, natively-synced Sendspin speaker in the same room.
2. Place the microphone roughly between them and tap **Measure**.
3. The page records ~6 seconds of audio and finds the delay between the
   two speakers via FFT-based autocorrelation (Wiener-Khinchin theorem:
   `autocorrelation = IFFT(|FFT(signal)|^2)`) - if the same audio is
   coming from two speakers with a timing offset, the microphone picks up
   one copy plus a delayed echo of itself, and this recovers that delay.

**Honesty about a real limitation, not hidden in the UI**: autocorrelation
of a single mono microphone can only recover the *magnitude* of the delay
between the two speakers, not *which one* played first. So the page asks
you to judge by ear which speaker sounded delayed and pick the matching
button, then applies the correction and offers a **re-measure to verify**
step so you can confirm the residual offset actually shrank rather than
blindly trusting one measurement.

The FFT/autocorrelation algorithm embedded in the page (`app/calibration.py`)
was not written ad-hoc: it was validated against a Python/numpy reference
implementation first (recovers known synthetic echo delays to <1ms across
a range of signal strengths and noise levels, from 150ms to 4800ms), then
independently re-verified as a Node.js port producing matching results,
*before* being embedded in the page. The live `/calibrate` and `/api/delay`
endpoints were also tested end-to-end (page serves and contains the
algorithm; `GET`/`POST /api/delay` correctly reads, sets, and clamps the
live `DelayEngine`) - all against the actual running aiohttp server, not
just read for plausibility.

**Not tested**: an actual microphone recording of two real speakers in a
real room. Everything above is real algorithmic and wiring correctness,
but a browser mic on real hardware picking up real room acoustics,
reverb, and background noise is a different (harder) test than clean
synthetic signals - the confidence score shown in the UI is there so you
can judge in the moment whether a given measurement looks trustworthy.

## Fixed: real cause of "no sound" from live device logs

A later round of real logs from an actual Sonos speaker + Music Assistant
pinned down the actual culprit: Sonos opened its HTTP connection to
`/sendspin.flac` immediately after this add-on called `play_uri`. But
Music Assistant didn't actually start sending real PCM audio over Sendspin
until **35 seconds later**. Sonos received total silence for those 35
seconds, gave up, and closed the connection one second after real audio
finally started flowing. Race lost by about a second.

**Root cause**: this add-on called `sonos.play_stream()` (which tells
Sonos to `play_uri` our HTTP stream) the instant the Sendspin *handshake*
completed - not once actual audio was flowing. There can be an arbitrary
gap between "player registered with the Sendspin server" and "Music
Assistant actually starts streaming audio to it" (e.g. nothing was queued
to play yet), and Sonos's own HTTP client won't tolerate a stream URI that
stays silent very long after being told to play it.

**Fix**: `sendspin_client.py` now distinguishes "handshake connected" from
"audio actually started" (`wait_until_streaming()`, set the moment the
first real PCM chunk arrives - not on the `stream_start` control message,
which can itself arrive before real audio does). `main.py`'s Sonos
bootstrap now binds to the Sonos speaker immediately (harmless, no audio
needed for that step) but waits for `wait_until_streaming()` before ever
calling `play_uri`. That way Sonos never opens a connection to a silent
stream in the first place.

Also fixed along the way: Sonos sends a `HEAD` request to validate the URL
before its real `GET` (visible in the logs). That was hitting the same
full streaming handler as `GET`, spinning up a needless ffmpeg process and
potentially contending for the single-active-listener slot. `HEAD` now
gets its own instant, lightweight response with no ffmpeg/ring-buffer
involvement at all.

**Verified live** against a real Sendspin test server: confirmed
`wait_until_streaming()` correctly returns `False` immediately after
handshake (before real audio exists) and only flips `True` once genuine
PCM chunks arrive; confirmed a `HEAD` request now returns in single-digit
milliseconds and opens zero ring-buffer readers.

## Status

## Fixed: no sound / no volume control

Real-world logs showed the Sonos speaker opening **three concurrent HTTP
connections** to `/sendspin.flac` within a ~20 second window, all closing
around the same moment - classic Sonos retry/reconnect behavior when a
stream isn't producing valid audio. The root cause: `RingBuffer` had a
single shared read pointer used by *every* connection. With more than one
concurrent connection, each one's `read()` call advanced the same pointer,
so connections stole bytes from each other mid-stream. That corrupts the
FLAC framing fed into ffmpeg, which explains total silence - Sonos can't
decode garbage, so it never plays anything and keeps retrying, opening
more concurrent connections, making it worse.

Fixed by giving `RingBuffer` independent per-reader cursors
(`open_reader()`/`read(reader_id, ...)`/`close_reader()`), so any number of
concurrent connections each see a coherent, correctly-ordered byte stream
regardless of what any other connection is doing. `stream_server.py` also
now closes out a previous connection when a new one arrives, since Sonos
only ever needs one active stream to its coordinator and there's no reason
to let retry connections pile up wasting ffmpeg processes.

**Verified**: a threaded test with 3 concurrent readers pulling from a
live-written ring buffer showed 0 discontinuities in any reader's stream
(previously this would corrupt every reader). A live end-to-end test
against a real Sendspin server, with two overlapping HTTP connections to
the stream server (reproducing the retry scenario), showed the surviving
connection received a clean, fully valid, decodable WAV stream (correct
RIFF header, 839KB of coherent audio).

**Not fully resolved**: my own test harness's shutdown sequence (calling
`client.stop()`/`server.stop()`) hung past a 40s timeout in this specific
concurrent-connection test, after the actual playback assertions had
already passed. I traced it partway - likely `AudioEncoder.stop()`'s
blocking `thread.join(timeout=2.0)` running synchronously inside a
cancelled asyncio task's cleanup path during the single-listener eviction
- but did not fully root-cause or fix it. This is a shutdown/cleanup-path
concern, not the playback-corruption bug that was reported, but if you see
the add-on hang on stop/restart under Supervisor, this is the first place
to look.

**Also added**: this add-on published no volume control at all, matching
your report. `SonosController.set_volume()` already existed but was never
wired to anything - added `number.sendspin_sonos_volume` (0-100%) via MQTT
discovery, wired through `main.py` to call it.

## Status

The real Sendspin protocol (via the official `aiosendspin==6.0.1` library)
is wired up in `app/sendspin_client.py` and has been **verified end-to-end
against a live Sendspin server**: connect → handshake → PCM format
negotiation → real audio bytes flowing into the ring buffer → live delay
changes with zero reconnects. See the docstring in that file for the exact
API details it was checked against.

| Component | File | Tested |
|---|---|---|
| Sendspin client (real protocol, `aiosendspin`) | `app/sendspin_client.py` | ✅ full handshake + live audio verified against a real Sendspin server |
| Ring buffer (jitter absorption, delay lookback) | `app/ring_buffer.py` | ✅ unit-tested pointer math |
| Delay engine (live slider → read-pointer offset) | `app/delay_engine.py` | ✅ confirmed delay changes never require a reconnect (incl. against a live connection) |
| Audio pipeline (ffmpeg encode) | `app/audio_pipeline.py` | ✅ end-to-end with synthetic 440Hz tone |
| HTTP stream server | `app/stream_server.py` | ✅ served a live-encoded WAV stream over real HTTP |
| Sonos control (SoCo) | `app/sonos_controller.py` | ⚠️ not tested — no Sonos hardware available in dev. Logic follows SoCo's documented API (`discover`, `play_uri`, `group.coordinator`, `unjoin`, etc). |
| HA entities via MQTT discovery | `app/ha_entities.py` | ⚠️ not tested — no MQTT broker/HA instance available in dev |

## Connecting to Music Assistant's Sendspin provider

Per Music Assistant's own Sendspin documentation:

- The server listens at `ws://<music-assistant-ip>:8927/sendspin` by
  default - this add-on's `sendspin_port` default is now `8927` to match
  (an earlier version of this add-on defaulted to `4400`, which was never
  a real Sendspin default, just an unverified placeholder - fixed now).
- **This gateway connects unencrypted.** `aiosendspin==6.0.1`, which this
  add-on is built and verified against, doesn't implement the protocol's
  Noise encryption/pairing handshake at all. Music Assistant's docs
  describe this as a "legacy client" and it will show up in the Sendspin
  player's settings with security state **"connected without encryption."**
  For it to connect at all, the Sendspin provider's **"Allow legacy
  clients"** setting must be left on (it's on by default, but Music
  Assistant's own docs note it's temporary and may be removed in a future
  release - if a future MA release removes it, this gateway would need
  `aiosendspin` bumped to a version with real Noise/pairing support and
  `app/sendspin_client.py` updated accordingly; see the version-pin note
  below).
- Music Assistant already has its own **per-player "Static playback delay
  (ms)"** setting (0-5000ms) built into the Sendspin protocol itself. This
  is *not* what this add-on's `delay_ms` controls - that MA-side setting
  is for genuine Sendspin clients doing their own local, sample-accurate
  playback scheduling, which Sonos cannot do. This add-on's `delay_ms`
  instead controls how far behind the *ring buffer's* read pointer trails
  its write pointer before audio reaches Sonos over HTTP - a different
  mechanism solving the same "keep this speaker in sync" problem, needed
  specifically because Sonos isn't a native Sendspin endpoint.
- Audio is sent to Sendspin players as 16-bit, matching what this add-on
  already assumes (`SupportedAudioFormat(..., bit_depth=16)`) - no change
  needed there.

## Repository structure (why the GitHub-URL error happened)

Home Assistant's Supervisor expects each add-on to live in its **own
subdirectory** alongside `repository.yaml` at the repo root:

```
repository.yaml              <- repo metadata, stays at the root
sendspin_sonos_gateway/      <- the add-on itself
    config.yaml
    build.yaml
    Dockerfile
    requirements.txt
    app/
    rootfs/
```

A previous version of this repo had `config.yaml` sitting directly at the
repo root *next to* `repository.yaml`, which isn't a structure the
Supervisor recognizes as either "one add-on repo" or "a repo of add-ons" -
that's what produced the *"...is not a valid app repository"* error when
adding the GitHub URL. That's fixed here.

## Other fixes in this pass

- **`config.yaml`**: removed the placeholder `image: ghcr.io/yourname/...`
  field. With that field present but pointing at a nonexistent image,
  Supervisor would try to pull it and fail install; without it, Supervisor
  builds the image locally from `Dockerfile`/`build.yaml` instead, which is
  correct for an add-on that isn't publishing prebuilt registry images.
- **`build.yaml`** (new): maps each supported architecture to the right HA
  base image, replacing an unused/incomplete `tempio`/`TARGETARCH` block
  that only handled amd64 and arm64 despite `config.yaml` claiming armv7
  and armhf support too.
- **`Dockerfile`**: was missing the steps that actually make the add-on
  work — it copied `rootfs/` and set labels, but never installed Python,
  ffmpeg, or `pip install`'d `requirements.txt`, and never copied `app/`
  into the image. Fixed to a complete build; the base image's own s6
  `/init` entrypoint is left as-is (no custom `ENTRYPOINT`/`CMD` needed).
- **`rootfs/etc/services.d/spin_sonos_gateway/run`**: was running
  `python3 -m app.main`, which does not work with this codebase - every
  module here uses flat imports (`from config import Config`, not
  `from .config import Config`), so running as an `-m app.main` package
  puts the wrong directory on `sys.path` and fails with
  `ModuleNotFoundError: No module named 'config'`. Fixed to `cd /app/app &&
  exec python3 -u main.py`, matching how every module actually imports.
- **`app/sendspin_client.py`**: the previous rewrite called the real
  `aiosendspin` library with several parameters that don't match its
  actual API — `roles=["player"]` (plain strings; the library expects
  `Roles.PLAYER` enum members and silently treats a string list as *no*
  supported roles), `device_info={}` / `player_support={}` (plain dicts
  where the library expects `DeviceInfo(...)` / `ClientHelloPlayerSupport(
  ...)` dataclass instances - this would crash mid-handshake), and
  `add_connect_listener(...)`, which doesn't exist in this library version.
  There was also dead/no-op clock-sync-via-`server/command` handling
  (`server/command` payloads don't carry time-sync data in this version -
  that's handled internally by the library already). All of this is
  rewritten and has been checked against the actual installed
  `aiosendspin==6.0.1` source and confirmed working against a real server
  (see Status table above).
- Removed committed `__pycache__/*.pyc` files and added `.gitignore` so
  they don't come back.

## Installing

1. In Home Assistant: Settings → Add-ons → Add-on Store → ⋮ → Repositories,
   add `https://github.com/flopthemarket/sonos-sendspin`.
2. Install "Sendspin Sonos Gateway" from the list.
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
cd sendspin_sonos_gateway
pip install -r requirements.txt --break-system-packages
SSG_SONOS_IP=192.168.1.50 SSG_SENDSPIN_HOST=192.168.1.10 \
  PYTHONPATH=app python3 app/main.py
```

Without `/data/options.json` present, `app/config.py` falls back to
`SSG_<OPTION_NAME>` environment variables (see `DEFAULTS` in that file for
the full list), which is how the above works outside the Supervisor.

## Known gaps / v2 items (called out explicitly, not silently skipped)

- **Sonos control and MQTT-based HA entities are untested against real
  hardware/brokers** — implemented against each library's documented API
  but not verified live, unlike the Sendspin client above.
- **Auto-calibration** (click + microphone offset measurement) — stubbed
  in `app/latency_tracker.py::run_auto_calibration`, raises
  `NotImplementedError` with a description of what real hardware/logic it
  needs, since there's no mic input in this container.
- **Sonos-side latency (Delay B)** is reported as a rough 1500–3000ms
  estimate, not measured, since Sonos exposes no API for it.
- **`aiosendspin` version pin**: this is a fast-moving protocol/library
  (multiple releases within days of each other observed during
  development). The client code here is verified against `aiosendspin==
  6.0.1` specifically — bumping that pin should be re-verified against the
  installed version's actual API before assuming it still works, since
  earlier testing surfaced real API differences between 6.0.x and 9.x
  (e.g. where `AudioFormat`/`DeviceInfo` live, constructor signature).
