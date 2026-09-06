# Sendspin Sonos Gateway

## Delay calibration - how it works

The add-on generates a short test tone (a chirp) and serves it at
`/calibration-tone.wav`. To calibrate:

1. Open this add-on's **Web UI** (button on this add-on's Info tab - see
   below if you don't see one) to reach `/calibrate`.
2. In Music Assistant, queue the calibration tone link shown on that page
   to play on **both** this Sonos speaker and a real, natively-synced
   Sendspin speaker in the same room.
3. Place your phone or laptop's microphone roughly between the two
   speakers.
4. Start the tone playing, then tap **Measure** on the page.
5. The page records your microphone, then compares it against the known
   tone to find how far apart the two speakers played it (matched
   filtering, not a blind guess).
6. It can tell you *how far apart* the speakers are, but not *which one*
   was late - so listen and press **Sonos sounded LATE** or **Sonos
   sounded EARLY**, whichever matches what you heard.
7. Tap **Re-measure to verify** to confirm the offset actually shrank.

## No "Open Web UI" button?

That button needs Home Assistant to reload this add-on's configuration
after an update. Do one of:

- Settings → Add-ons → Add-on Store → ⋮ → **Check for updates**, then
  update/reinstall this add-on, or
- Uninstall and reinstall the add-on from your local repository.

Once reloaded, the button appears on the add-on's **Info** tab. You can
also always reach the page directly at `http://<gateway-ip>:8099/calibrate`
- note the microphone will only work through the Web UI button (Ingress),
not that direct address, since browsers require a secure context for
microphone access.
