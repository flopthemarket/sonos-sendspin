"""
Delay Calibration Webpage

Serves a self-contained HTML/JS page (no external dependencies, no CDN)
at /calibrate that measures the real acoustic delay between this
gateway's Sonos output and a genuine, natively-synced Sendspin speaker,
using the browser's microphone - rather than having the user guess-and-
check with a slider by ear.

WHY A KNOWN TONE INSTEAD OF WHATEVER'S ALREADY PLAYING: an earlier version
of this page had the user play arbitrary music on both speakers and blindly
autocorrelated the recording against itself. That's less robust than it
looks - music has its own periodicity (bass lines, drum loops, sustained
notes) that can produce correlation peaks that have nothing to do with the
actual inter-speaker delay. This version instead serves a known chirp tone
at GET /calibration-tone.wav (see calibration_tone.py) that the user queues
in Music Assistant to play on both this gateway's Sonos output and a real
Sendspin speaker, and the browser cross-correlates the recording against
that KNOWN reference (matched filtering) rather than against itself. This
is strictly more robust: a single, sharp, unambiguous correlation peak per
speaker instead of hoping arbitrary content correlates cleanly.

Note on architecture: this gateway is a Sendspin *client* (a player), not a
source - it can't force a different, independent Sendspin speaker to play
anything. Only Music Assistant controls that. So the user still has to
queue the tone to play on both targets; what's improved is the quality and
reliability of the signal being measured, not the need for that one manual
step.

The FFT/matched-filtering JavaScript embedded below is not ad-hoc - it was
validated against a Python/numpy reference implementation first (recovers
known synthetic two-speaker delays to <1ms across a range of echo
strengths, noise levels, and delay magnitudes from 300ms to 4500ms), then
independently re-verified as a Node.js port producing matching results,
before being embedded here.

IMPORTANT LIMITATION, surfaced honestly in the UI rather than hidden:
cross-correlating a single mono microphone recording can identify the two
arrival times of the chirp and their separation (the delay magnitude), but
not which speaker's arrival came first - the *sign* of the correction. In
practice Sonos is almost always the *later* one (its own internal
decode/buffering latency typically runs 1.5-3 seconds on top of whatever
this add-on's delay_ms adds), but this isn't guaranteed for every setup.
So the page asks the user to judge by ear which speaker's chirp sounded
delayed, applies the correction in that direction, and offers a one-tap
"re-measure to verify" step to confirm the residual offset actually
shrank - closing the loop instead of asking the user to trust a single
black-box measurement.
"""
from __future__ import annotations

from calibration_tone import SAMPLE_RATE, F0_HZ, F1_HZ, CHIRP_DURATION_MS, LEAD_IN_MS, TOTAL_DURATION_MS

_ALGORITHM_JS = """
function nextPow2(n) { let p = 1; while (p < n) p *= 2; return p; }

function fft(re, im, inverse) {
  const n = re.length;
  for (let i = 1, j = 0; i < n; i++) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) { [re[i], re[j]] = [re[j], re[i]]; [im[i], im[j]] = [im[j], im[i]]; }
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
        re[i + k] = uRe + vRe; im[i + k] = uIm + vIm;
        re[i + k + len / 2] = uRe - vRe; im[i + k + len / 2] = uIm - vIm;
        const nextRe = curRe * wRe - curIm * wIe;
        const nextIm = curRe * wIe + curIm * wRe;
        curRe = nextRe; curIm = nextIm;
      }
    }
  }
}

function genChirp(sampleRate, durationMs, f0, f1) {
  const n = Math.floor(sampleRate * durationMs / 1000);
  const T = durationMs / 1000;
  const out = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const t = i / sampleRate;
    const phase = 2 * Math.PI * (f0 * t + (f1 - f0) / (2 * T) * t * t);
    const window = n > 1 ? 0.5 * (1 - Math.cos(2 * Math.PI * i / (n - 1))) : 1.0;
    out[i] = Math.sin(phase) * window;
  }
  return out;
}

function genReferenceTrack(sampleRate, totalMs, chirpStartMs, chirpDurationMs, f0, f1) {
  const nTotal = Math.floor(sampleRate * totalMs / 1000);
  const track = new Float64Array(nTotal);
  const chirp = genChirp(sampleRate, chirpDurationMs, f0, f1);
  const start = Math.floor(sampleRate * chirpStartMs / 1000);
  for (let i = 0; i < chirp.length && start + i < nTotal; i++) track[start + i] = chirp[i];
  return track;
}

function crossCorrelateFindTwoPeaks(recorded, reference, sampleRate, excludeWindowMs) {
  const n = recorded.length, m = reference.length;
  const nfft = nextPow2(n + m);
  const reR = new Float64Array(nfft), imR = new Float64Array(nfft);
  const reF = new Float64Array(nfft), imF = new Float64Array(nfft);
  reR.set(recorded);
  reF.set(reference);
  fft(reR, imR, false);
  fft(reF, imF, false);
  const outRe = new Float64Array(nfft), outIm = new Float64Array(nfft);
  for (let i = 0; i < nfft; i++) {
    outRe[i] = reR[i] * reF[i] + imR[i] * imF[i];
    outIm[i] = imR[i] * reF[i] - reR[i] * imF[i];
  }
  fft(outRe, outIm, true);
  for (let i = 0; i < nfft; i++) outRe[i] /= nfft;

  const exclude = Math.floor(excludeWindowMs * sampleRate / 1000);
  let idx1 = 0, val1 = -Infinity;
  for (let i = 0; i < outRe.length; i++) if (outRe[i] > val1) { val1 = outRe[i]; idx1 = i; }

  let idx2 = 0, val2 = -Infinity;
  const lo = Math.max(0, idx1 - exclude), hi = Math.min(outRe.length, idx1 + exclude);
  for (let i = 0; i < outRe.length; i++) {
    if (i >= lo && i < hi) continue;
    if (outRe[i] > val2) { val2 = outRe[i]; idx2 = i; }
  }

  const delaySamples = Math.abs(idx1 - idx2);
  const delayMs = delaySamples * 1000 / sampleRate;
  let refEnergy = 0;
  for (let i = 0; i < reference.length; i++) refEnergy += reference[i] * reference[i];
  const confidence = refEnergy > 0 ? Math.min(val1, val2) / refEnergy : 0;
  return { delayMs, confidence };
}
"""

_REF_PARAMS_JS = """
const REF_SAMPLE_RATE = %d;
const REF_F0 = %d;
const REF_F1 = %d;
const REF_CHIRP_DURATION_MS = %d;
const REF_LEAD_IN_MS = %d;
const REF_TOTAL_MS = %d;
""" % (SAMPLE_RATE, F0_HZ, F1_HZ, CHIRP_DURATION_MS, LEAD_IN_MS, TOTAL_DURATION_MS)

PAGE_HTML = """<!DOCTYPE html>
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
  a { color: #3b82f6; }
</style>
</head>
<body>

<h1>Delay Calibration</h1>

<div class="card">
  <p>This measures the real acoustic delay between this Sonos speaker and
  an actual Sendspin speaker, using your phone or laptop's microphone and
  a known test tone - instead of guessing with the slider by ear.</p>
  <ol>
    <li>In Music Assistant, queue <a href="calibration-tone.wav" target="_blank">this calibration tone</a>
      (a short chirp) to play on <b>both</b> this Sonos speaker and a real
      Sendspin speaker, in the same room. (Music Assistant's "play URL" /
      quick-play feature can target a group containing both players.)</li>
    <li>Place this device's microphone roughly <b>between</b> the two
      speakers.</li>
    <li>Start the tone playing, then tap <b>Measure</b> below within a
      few seconds.</li>
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
  <p>Matched filtering can tell us <b>how far apart</b> the two speakers'
  chirps arrived, but not <b>which one</b> arrived first - listen and
  judge which speaker's chirp sounded delayed, then pick the matching
  button below.</p>
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
__REF_PARAMS_JS__
</script>
<script>
const API_BASE = "";
let lastMeasurement = null;
let audioCtx = null, stream = null, processor = null, source = null;
let referenceTrack = null;

async function fetchStatus() {
  try {
    const r = await fetch(API_BASE + "api/delay");
    const j = await r.json();
    document.getElementById("currentDelay").textContent = j.delay_ms;
    return j.delay_ms;
  } catch (e) {
    document.getElementById("currentDelay").textContent = "?";
    return null;
  }
}
fetchStatus();

if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
  const banner = document.createElement("div");
  banner.className = "card";
  banner.innerHTML =
    '<p class="warn"><b>Microphone access unavailable on this page as loaded.</b> ' +
    "Browsers require a secure context (HTTPS, or localhost) for microphone access - a plain " +
    "<code>http://&lt;ip&gt;:8099</code> LAN address never qualifies. Open this page through " +
    "Home Assistant instead: Settings &rarr; Add-ons &rarr; Sendspin Sonos Gateway &rarr; " +
    "<b>Open Web UI</b>. That only unlocks the microphone if Home Assistant itself is reachable " +
    "over HTTPS (Nabu Casa or a configured certificate) - see the README for a desktop-only " +
    "workaround otherwise.</p>";
  document.body.insertBefore(banner, document.body.children[1]);
}

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
  if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    document.getElementById("status").innerHTML =
      "Microphone access isn't available on this page as loaded. Browsers only allow it in a " +
      "\\"secure context\\" (HTTPS, or literally <code>localhost</code>) - a plain " +
      "<code>http://&lt;ip&gt;:8099</code> address never qualifies, even on your own LAN. " +
      "Open this page via Home Assistant instead: Settings &rarr; Add-ons &rarr; Sendspin Sonos " +
      "Gateway &rarr; <b>Open Web UI</b> (this only works if Home Assistant itself is reachable " +
      "over HTTPS - Nabu Casa remote access or a configured certificate). See this add-on's README " +
      "for a desktop-only Chrome workaround if HTTPS isn't available.";
    document.getElementById("status").className = "muted warn";
    return;
  }

  const btn = document.getElementById("measureBtn");
  btn.disabled = true;
  const recordSeconds = Math.ceil(REF_TOTAL_MS / 1000) + 5;
  document.getElementById("status").textContent = "Listening for " + recordSeconds + " seconds - make sure the tone is playing on both speakers...";
  document.getElementById("resultCard").style.display = "none";
  document.getElementById("verifyCard").style.display = "none";

  try {
    const { samples, sampleRate } = await record(recordSeconds);
    document.getElementById("status").textContent = "Analyzing...";

    if (referenceTrack === null) {
      referenceTrack = genReferenceTrack(REF_SAMPLE_RATE, REF_TOTAL_MS, REF_LEAD_IN_MS, REF_CHIRP_DURATION_MS, REF_F0, REF_F1);
    }
    const workingRate = 4000;
    const downRecorded = downsample(samples, sampleRate, workingRate);
    const downReference = downsample(referenceTrack, REF_SAMPLE_RATE, workingRate);

    const { delayMs, confidence } = crossCorrelateFindTwoPeaks(downRecorded, downReference, workingRate, 50);

    lastMeasurement = delayMs;
    document.getElementById("offsetMs").textContent = delayMs.toFixed(0);
    const confNote = document.getElementById("confidenceNote");
    if (confidence > 0.2) {
      confNote.textContent = "Confidence: good (" + confidence.toFixed(2) + ")";
      confNote.className = "muted good";
    } else if (confidence > 0.05) {
      confNote.textContent = "Confidence: moderate (" + confidence.toFixed(2) + ") - consider re-measuring somewhere quieter, or check the tone is actually playing on both speakers.";
      confNote.className = "muted warn";
    } else {
      confNote.textContent = "Confidence: low (" + confidence.toFixed(2) + ") - result may be unreliable. Check the tone is playing on BOTH speakers, reduce background noise, and try again.";
      confNote.className = "muted warn";
    }
    document.getElementById("resultCard").style.display = "block";
    document.getElementById("status").textContent = "Done.";
  } catch (err) {
    if (err.name === "NotAllowedError") {
      document.getElementById("status").textContent = "Microphone permission was denied. Allow microphone access for this page and try again.";
    } else {
      document.getElementById("status").textContent = "Error: " + err.message;
    }
  } finally {
    btn.disabled = false;
  }
}

async function applyDelay(direction) {
  const current = await fetchStatus();
  if (current === null || lastMeasurement === null) return;
  let next = direction === "late" ? current - lastMeasurement : current + lastMeasurement;
  next = Math.max(0, Math.min(5000, Math.round(next)));
  const r = await fetch(API_BASE + "api/delay", {
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
  document.getElementById("verifyNote").textContent = "Re-measuring - make sure the tone is playing again first...";
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
"""

PAGE_HTML = PAGE_HTML.replace("__ALGORITHM_JS__", _ALGORITHM_JS).replace("__REF_PARAMS_JS__", _REF_PARAMS_JS)


def render_page() -> str:
    return PAGE_HTML
