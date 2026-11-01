"""Self-contained LAN Kodi remote control. No provider passwords or external assets."""
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Foxtel Kodi Companion</title>
<style>
:root{color-scheme:dark;font:16px system-ui,-apple-system,Arial,sans-serif;background:#101624;color:#f3f6ff}
*{box-sizing:border-box}body{max-width:880px;margin:0 auto;padding:2rem 1rem}
h1{font-size:1.65rem;margin-bottom:.4rem}p{color:#b6c3d8;line-height:1.55}
#state{padding:.9rem;border:1px solid #3c4c67;border-radius:8px;margin:1rem 0}
#channels{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:.7rem;margin-top:1rem}
.channel{background:#1b283b;border:1px solid #33455b;border-radius:10px;padding:1rem;display:flex;flex-direction:column;gap:.5rem}
.channel small{color:#afc2d7}
button{border:0;border-radius:7px;background:#467fe5;color:#fff;padding:.65rem 1rem;font-weight:600;cursor:pointer}
button[disabled]{opacity:.55;cursor:not-allowed}
.warning{color:#ffc480}.fine{font-size:.85rem}
</style>
</head>
<body>
<h1>Foxtel Kodi companion</h1>
<p>Launch available Kodi add-on channels on your separate Kodi device. This does not play DRM video inside Jellyfin. Foxtel login remains entirely within the Kodi add-on.</p>
<div id="state" role="status" aria-live="polite">Checking Kodi connection…</div>
<div id="channels" aria-label="Channels"></div>
<p class="fine warning">The historical Foxtel Kodi add-ons are deprecated. A successful launch request does not guarantee provider playback.</p>
<script>
"use strict";
const token = new URLSearchParams(location.search).get("token") || "";
const auth = {"Authorization": "Bearer " + token};
const state = document.getElementById("state");
const channels = document.getElementById("channels");
async function request(path, options={}) {
  const response = await fetch(path, {cache:"no-store", headers: {...auth, ...(options.headers||{})}, ...options});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || ("HTTP " + response.status));
  return data;
}
async function play(channel, button) {
  button.disabled = true;
  state.textContent = "Sending " + channel.name + " to Kodi…";
  try {
    const data = await request("/kodi/play", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({channel_id:channel.channel_id})});
    state.textContent = data.channel + ": playback request accepted by Kodi. Confirm playback on your Kodi device.";
  } catch(error) {
    state.textContent = "Kodi playback request failed: " + error.message;
  } finally {
    button.disabled = false;
  }
}
async function initialise() {
  if (!token) {state.textContent="Feed token missing from URL.";return;}
  try {
    const [status, catalogue] = await Promise.all([request("/kodi/status"), request("/kodi/channels")]);
    state.textContent = status.kodi_reachable
      ? "Kodi is reachable. " + catalogue.channels.length + " channel(s) imported."
      : "Kodi is not reachable or not configured. Check KODI_RPC_URL and Kodi HTTP control.";
    channels.replaceChildren();
    if (!catalogue.channels.length) {
      const empty = document.createElement("p");
      empty.textContent = "No Foxtel Kodi channels imported. Export your own Kodi IPTV Merge playlist into kodi-foxtel.m3u.";
      channels.appendChild(empty);
    }
    for (const channel of catalogue.channels) {
      const card = document.createElement("div");
      card.className = "channel";
      const title = document.createElement("strong");
      title.textContent = channel.name;
      const description = document.createElement("small");
      description.textContent = channel.addon + (channel.tvg_id ? " | " + channel.tvg_id : "");
      const button = document.createElement("button");
      button.textContent = "Play on Kodi";
      button.disabled = !status.kodi_reachable;
      button.onclick = () => play(channel, button);
      card.append(title, description, button);
      channels.appendChild(card);
    }
  } catch(error) {
    state.textContent = "Companion unavailable: " + error.message;
  }
}
initialise();
</script>
</body>
</html>
""".encode("utf-8")
