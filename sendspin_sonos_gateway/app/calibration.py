"""
Delay Calibration Webpage

Serves a self-contained HTML/JS page (no external dependencies, no CDN)
at /calibrate that measures the real acoustic delay between this
gateway's Sonos output and a genuine, natively-synced Sendspin speaker
playing the same audio, using the browser's microphone - rather than
having the user guess-and-check with a slider by ear.

How it works: if the same audio is playing on both Sonos (via this
gateway) and a real Sendspin speaker, a single microphone placed between
them picks up one signal plus a delayed copy of itself (like an echo).
The delay between those two copies is exactly the timing mismatch we want
to correct. That delay is recovered via FFT-based autocorrelation
(Wiener-Khinchin theorem: autocorrelation = IFFT(|FFT(x)|^2)), which is
efficient enough to run in a few milliseconds in-browser even over a
multi-second recording.

The FFT/autocorrelation JavaScript embedded below is not ad-hoc - it was
written and verified against a Python/numpy reference implementation
first (recovers known synthetic delays to <1ms across a range of
signal-to-noise and echo-strength conditions), then re-verified as a
standalone Node.js port matching that reference before being embedded
here. See the project's test notes for the validation cases used.

IMPORTANT LIMITATION, surfaced honestly in the UI rather than hidden:
autocorrelation of a single mono microphone signal can only recover the
*magnitude* of the delay between the two speakers, not its *sign* - it
cannot tell you which speaker played first. In practice Sonos is almost
always the *later* one (its own internal decode/buffering latency, per
Sonos's own behavior, typically runs 1.5-3 seconds on top of whatever
this add-on's delay_ms adds), but this isn't guaranteed for every setup.
So the page asks the user to judge by ear which speaker sounded delayed,
applies the correction in that direction, and offers a one-tap
"re-measure to verify" step to confirm the residual offset actually
shrank - closing the loop instead of asking the user to just trust a
single black-box measurement.
"""
from __future__ import annotations

# The embedded <script> block below is the exact algorithm from
# calibration_algo.js, ported for the browser (removes the Node.js
# `module.exports`, everything else is unchanged) - verified against a
# Python/numpy reference implementation and cross-checked in Node.js
# before being placed here.
_ALGORITHM_JS = r"""
function nextPow2(n) {
  let p = 1;
  while (p < n) p *= 2;
  return p;
}

function fft(re, im, inverse) {
  const n = re.length;
  for (let i = 1, j = 0; i < n; i++) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      [re[i], re[j]] = [re[j], re[i]];
      [im[i], im[j]] = [im[j], im[i]];
    }
  }
  for (let len = 2; len <= n; len <<= 1) {
    const ang = (2 * Math.PI / len) * (inverse ? -1 : 1);
    const wRe = Math.cos(ang), wIe = Math.sin(ang);
    for (let i = 0; i < n; i += len) {
      let curRe = 1, curIm = 0;
      for (let k = 0; k < len / 2; k++) {
        const uRe = re[i + k], uIm = im[i + k];
        const vRe = re[i + k + len / 2] * curRe - im[i + k + len / 2] * curIm;
        const vIm = re[i + k + len / 2] * curIm + im[i + k + len / 2] * curRe;
        re[i + k] = uRe + vRe;
        im[i + k] = uIm + vIm;
        re[i + k + len / 2] = uRe - vRe;
        im[i + k + len / 2] = uIm - vIm;
        const nextRe = curRe * wRe - curIm * wIe;
        const nextIm = curRe * wIe + curIm * wRe;
        curRe = nextRe;
        curIm = nextIm;
      }
    }
  }
}

function findEchoDelay(signal, sampleRate, minLagMs, maxLagMs) {
  const n = signal.length;
  const maxLag = Math.floor(maxLagMs * sampleRate / 1000);
  const minLag = Math.floor(minLagMs * sampleRate / 1000);
  const nfft = nextPow2(n + maxLag);

  const re = new Float64Array(nfft);
  const im = new Float64Array(nfft);
  re.set(signal);

  fft(re, im, false);
  for (let i = 0; i < nfft; i++) {
    re[i] = re[i] * re[i] + im[i] * im[i];
    im[i] = 0;
  }
  fft(re, im, true);
  for (let i = 0; i < nfft; i++) re[i] /= nfft;

  const zeroLag = re[0];
  let bestIdx = minLag;
  let bestVal = -Infinity;
  for (let k = minLag; k < maxLag; k++) {
    if (re[k] > bestVal) {
      bestVal = re[k];
      bestIdx = k;
    }
  }
  return {
    delayMs: bestIdx * 1000 / sampleRate,
    confidence: bestVal / zeroLag,
  };
}
"""

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sendspin Sonos Gateway - Delay Calibration</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; max-width: 640px; margin: 0 auto; padding: 20px; background: #111; color: #eee; }
  h1 { font-size: 1.3em; }
  .card { background: #1c1c1c; border-radius: 10px; padding: 16px; margin: 16px 0; }
  button { font-size: 1em; padding: 10px 16px; border-radius: 8px; border: none; margin: 4px 4px 4px 0; cursor: pointer; }
  button.primary { background: #3b82f6; color: white; }
  button.late { background: #f59e0b; color: black; }
  button.early { background: #10b981; color: black; }
  button:disabled { opacity: 0.5; cursor: default; }
  .level-bar { height: 10px; background: #333; border-radius: 5px; overflow: hidden; margin-top: 8px; }
  .level-fill { height: 100%; background: #3b82f6; width: 0%; transition: width 60ms linear; }
  .big { font-size: 1.6em; font-weight: 600; }
  .muted { color: #999; font-size: 0.9em; }
  .warn { color: #f59e0b; }
  .good { color: #10b981; }
  code { background: #2a2a2a; padding: 1px 5px; border-radius: 4px; }
</style>
</head>
<body>

<h1>Delay Calibration</h1>

<div class="card">
  <p>This measures the real acoustic delay between this Sonos speaker and
  an actual Sendspin speaker, using your phone or laptop's microphone -
  instead of guessing with the slider by ear.</p>
  <ol>
    <li>Start the <b>same track</b> playing in Music Assistant to <b>both</b>
      this Sonos speaker and a real Sendspin speaker, in the same room.</li>
    <li>Place this device's microphone roughly <b>between</b> the two
      speakers.</li>
    <li>Stay quiet, tap <b>Measure</b>, and wait a few seconds.</li>
  </ol>
  <p class="muted">Current gateway delay: <span id="currentDelay">-</span> ms</p>
</div>

<div class="card">
  <button class="primary" id="measureBtn">Measure</button>
  <div class="level-bar"><div class="level-fill" id="levelFill"></div></div>
  <p class="muted" id="status">Idle.</p>
</div>

<div class="card" id="resultCard" style="display:none">
  <p>Detected offset: <span class="big" id="offsetMs">-</span> ms</p>
  <p class="muted" id="confidenceNote"></p>
  <p>Autocorrelation can tell us <b>how far apart</b> the two speakers are,
  but not <b>which one</b> played first - listen and judge which speaker
  sounded delayed, then pick the matching button below.</p>
  <button class="late" id="applyLateBtn">Sonos sounded LATE (most common)</button>
  <button class="early" id="applyEarlyBtn">Sonos sounded EARLY</button>
  <p class="muted" id="applyNote"></p>
</div>

<div class="card" id="verifyCard" style="display:none">
  <button class="primary" id="verifyBtn">Re-measure to verify</button>
  <p class="muted" id="verifyNote"></p>
</div>

<script>
__ALGORITHM_JS__
</script>
<script>
const API_BASE = "";
let lastMeasurement = null;
let audioCtx = null, stream = null, processor = null, source = null;

async function fetchStatus() {
  try {
    const r = await fetch(API_BASE + "/api/delay");
    const j = await r.json();
    document.getElementById("currentDelay").textContent = j.delay_ms;
    return j.delay_ms;
  } catch (e) {
    document.getElementById("currentDelay").textContent = "?";
    return null;
  }
}
fetchStatus();

function downsample(buffer, fromRate, toRate) {
  const factor = Math.max(1, Math.round(fromRate / toRate));
  const outLen = Math.floor(buffer.length / factor);
  const out = new Float64Array(outLen);
  for (let i = 0; i < outLen; i++) {
    let sum = 0;
    for (let k = 0; k < factor; k++) sum += buffer[i * factor + k];
    out[i] = sum / factor;
  }
  return out;
}

async function record(durationS) {
  stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
    }
  });
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  source = audioCtx.createMediaStreamSource(stream);
  processor = audioCtx.createScriptProcessor(4096, 1, 1);
  const chunks = [];
  let peakLevel = 0;

  await new Promise((resolve) => {
    processor.onaudioprocess = (e) => {
      const data = e.inputBuffer.getChannelData(0);
      chunks.push(new Float32Array(data));
      let localPeak = 0;
      for (let i = 0; i < data.length; i++) localPeak = Math.max(localPeak, Math.abs(data[i]));
      peakLevel = Math.max(peakLevel * 0.9, localPeak);
      document.getElementById("levelFill").style.width = Math.min(100, peakLevel * 140) + "%";
    };
    source.connect(processor);
    processor.connect(audioCtx.destination);
    setTimeout(resolve, durationS * 1000);
  });

  source.disconnect();
  processor.disconnect();
  stream.getTracks().forEach((t) => t.stop());
  const sourceRate = audioCtx.sampleRate;
  await audioCtx.close();

  let total = 0;
  for (const c of chunks) total += c.length;
  const full = new Float64Array(total);
  let pos = 0;
  for (const c of chunks) { full.set(c, pos); pos += c.length; }
  return { samples: full, sampleRate: sourceRate };
}

async function measure() {
  const btn = document.getElementById("measureBtn");
  btn.disabled = true;
  document.getElementById("status").textContent = "Listening for 6 seconds - stay quiet...";
  document.getElementById("resultCard").style.display = "none";
  document.getElementById("verifyCard").style.display = "none";

  try {
    const { samples, sampleRate } = await record(6);
    document.getElementById("status").textContent = "Analyzing...";
    const targetRate = 4000;
    const down = downsample(samples, sampleRate, targetRate);
    const { delayMs, confidence } = findEchoDelay(down, targetRate, 80, 5000);

    lastMeasurement = delayMs;
    document.getElementById("offsetMs").textContent = delayMs.toFixed(0);
    const confNote = document.getElementById("confidenceNote");
    if (confidence > 0.15) {
      confNote.textContent = "Confidence: good (" + confidence.toFixed(2) + ")";
      confNote.className = "muted good";
    } else if (confidence > 0.05) {
      confNote.textContent = "Confidence: moderate (" + confidence.toFixed(2) + ") - consider re-measuring somewhere quieter.";
      confNote.className = "muted warn";
    } else {
      confNote.textContent = "Confidence: low (" + confidence.toFixed(2) + ") - result may be unreliable. Check both speakers are actually playing the same audio, reduce background noise, and try again.";
      confNote.className = "muted warn";
    }
    document.getElementById("resultCard").style.display = "block";
    document.getElementById("status").textContent = "Done.";
  } catch (err) {
    document.getElementById("status").textContent = "Error: " + err.message + " (microphone permission needed)";
  } finally {
    btn.disabled = false;
  }
}

async function applyDelay(direction) {
  const current = await fetchStatus();
  if (current === null || lastMeasurement === null) return;
  let next = direction === "late" ? current - lastMeasurement : current + lastMeasurement;
  next = Math.max(0, Math.min(5000, Math.round(next)));
  const r = await fetch(API_BASE + "/api/delay", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ delay_ms: next }),
  });
  const j = await r.json();
  document.getElementById("currentDelay").textContent = j.delay_ms;
  document.getElementById("applyNote").textContent = "Applied. New delay: " + j.delay_ms + " ms.";
  document.getElementById("verifyCard").style.display = "block";
  document.getElementById("verifyNote").textContent = "";
}

document.getElementById("measureBtn").addEventListener("click", measure);
document.getElementById("applyLateBtn").addEventListener("click", () => applyDelay("late"));
document.getElementById("applyEarlyBtn").addEventListener("click", () => applyDelay("early"));
document.getElementById("verifyBtn").addEventListener("click", async () => {
  document.getElementById("verifyNote").textContent = "Re-measuring...";
  await measure();
  if (lastMeasurement !== null) {
    const improved = lastMeasurement < 100;
    document.getElementById("verifyNote").textContent = improved
      ? "Residual offset now " + lastMeasurement.toFixed(0) + " ms - looking good."
      : "Residual offset still " + lastMeasurement.toFixed(0) + " ms - if this grew instead of shrank, try the other direction button above.";
    document.getElementById("verifyNote").className = improved ? "muted good" : "muted warn";
  }
});
</script>
</body>
</html>
""".replace("__ALGORITHM_JS__", _ALGORITHM_JS)


def render_page() -> str:
    return PAGE_HTML
