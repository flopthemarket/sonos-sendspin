"""
Latency Tracker

Distinguishes the two delays in the system:
  Delay A - the gateway delay, directly controlled by the HA slider
            (see delay_engine.py). Known exactly at all times.
  Delay B - Sonos's own internal buffering/decoding delay. NOT exposed
            by Sonos's API, so it can only be estimated or measured.

For v1 this module reports Delay A directly and exposes Delay B as an
unmeasured placeholder plus buffer-health metrics, since Sonos exposes no
API for its internal latency. The design doc's v2 auto-calibration
(click-and-microphone offset measurement) is stubbed out as a documented
extension point rather than implemented here, since it requires physical
audio hardware (mic input) that doesn't exist in the container.
"""
from __future__ import annotations

import logging
import time

from delay_engine import DelayEngine
from ring_buffer import RingBuffer

log = logging.getLogger("ssg.latency_tracker")

# Rough published range from the design doc; treat as a placeholder until
# real measurement (v2) is implemented.
ESTIMATED_SONOS_LATENCY_MS = (1500, 3000)


class LatencyTracker:
    def __init__(self, ring_buffer: RingBuffer, delay_engine: DelayEngine):
        self.ring_buffer = ring_buffer
        self.delay_engine = delay_engine
        self._last_buffer_fill = 0.0
        self._last_update = time.monotonic()

    def snapshot(self) -> dict:
        now = time.monotonic()
        fill_s = self.ring_buffer.fill_seconds()
        fill_ratio = self.ring_buffer.fill_ratio()

        # Drift heuristic: if buffer fill is steadily shrinking or growing
        # over time, that indicates our write rate and Sonos's consumption
        # rate (via the encoder) have drifted apart.
        dt = max(now - self._last_update, 1e-6)
        drift_rate = (fill_s - self._last_buffer_fill) / dt
        self._last_buffer_fill = fill_s
        self._last_update = now

        return {
            "gateway_delay_ms": self.delay_engine.get_delay(),
            "estimated_sonos_latency_ms_low": ESTIMATED_SONOS_LATENCY_MS[0],
            "estimated_sonos_latency_ms_high": ESTIMATED_SONOS_LATENCY_MS[1],
            "buffer_fill_seconds": round(fill_s, 3),
            "buffer_fill_ratio": round(fill_ratio, 3),
            "drift_rate_s_per_s": round(drift_rate, 4),
        }

    def run_auto_calibration(self) -> None:
        """v2 placeholder. Real implementation would:
          1. Play a short click through the gateway's audio pipeline.
          2. Timestamp when the click was written to the ring buffer.
          3. Listen via an ESP32/USB mic or the HA Assist mic for the click
             actually coming out of the Sonos speaker.
          4. Compute the offset and feed it back into DelayEngine.
        Not implemented - no microphone input is available in this
        container by default."""
        raise NotImplementedError(
            "Auto-calibration requires a microphone input source; see "
            "module docstring for the intended v2 design."
        )
