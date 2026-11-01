"""Safe Kodi Foxtel add-on compatibility layer.

This sends plugin:// playback requests to a *separately running* Kodi player.
It does not decrypt, relay, capture or modify subscription video streams.
"""
import base64
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit, parse_qs
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

ALLOWED_ADDONS = frozenset(("plugin.video.foxtel.go", "plugin.video.foxtel.now"))
MAX_M3U_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_ENTRY_URL = 4096
ATTR = re.compile(r'([A-Za-z][\w-]*)="([^"]*)"')
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class KodiBridgeError(RuntimeError):
    """A safe configuration/transport failure to display to the operator."""


def plugin_url_allowed(url):
    """Only URLs for the two historical Foxtel Kodi add-ons are accepted."""
    if not isinstance(url, str) or len(url) > MAX_ENTRY_URL or CONTROL.search(url):
        return False
    parsed = urlsplit(url)
    if not (parsed.scheme == "plugin" and parsed.netloc in ALLOWED_ADDONS
            and not parsed.username and not parsed.password and not parsed.fragment):
        return False
    query = parse_qs(parsed.query, keep_blank_values=True)
    # Historical SlyGuy Foxtel exports use ?_=play&id=<numeric-id>.
    # Do not allow arbitrary Kodi plugin actions from imported M3U files.
    return (query.get("_") == ["play"]
            and len(query.get("id", [])) == 1
            and query["id"][0].isdigit()
            and len(query["id"][0]) <= 24)


def parse_kodi_playlist(data):
    """Read Kodi IPTV Merge M3U. NEVER make plugin:// links Jellyfin M3U entries."""
    if isinstance(data, bytes):
        data = data.decode("utf-8-sig", errors="replace")
    entries, seen, details = [], set(), None
    for raw in data.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF:"):
            attrs = dict(ATTR.findall(line))
            name = line.split(",", 1)[-1].strip() if "," in line else ""
            details = {
                "name": name or attrs.get("tvg-name", "Unnamed Kodi channel"),
                "tvg_id": attrs.get("tvg-id", ""),
                "group": attrs.get("group-title", "Foxtel via Kodi"),
                "logo": attrs.get("tvg-logo", ""),
            }
        elif line and not line.startswith("#"):
            if details is not None and plugin_url_allowed(line):
                channel_id = hashlib.sha256(line.encode("utf-8")).hexdigest()[:24]
                if channel_id not in seen:
                    seen.add(channel_id)
                    entries.append({
                        **details,
                        "channel_id": channel_id,
                        "addon": urlsplit(line).netloc,
                        "_play_url": line,
                    })
            details = None
    return entries


def read_kodi_playlist(path):
    """The playlist is supplied locally; no public downloader or arbitrary URL."""
    source = Path(path)
    if not source.is_file():
        return []
    if source.stat().st_size > MAX_M3U_BYTES:
        raise KodiBridgeError("Kodi playlist exceeds 2 MiB")
    return parse_kodi_playlist(source.read_bytes())



class KodiRemote:
    """Fixed endpoint JSON-RPC client. No runtime user-supplied target URLs."""

    def __init__(self, rpc_url, username="", password=""):
        self.rpc_url = rpc_url.strip()
        parsed = urlsplit(self.rpc_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query
                or parsed.fragment or parsed.path not in ("", "/", "/jsonrpc")):
            raise KodiBridgeError("KODI_RPC_URL must be a fixed HTTP(S) Kodi host or /jsonrpc endpoint")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise KodiBridgeError("KODI_RPC_URL has invalid port") from exc
        self.rpc_url = self.rpc_url.rstrip("/")
        if parsed.path in ("", "/"):
            self.rpc_url += "/jsonrpc"
        self.username = username
        self.password = password

    def call(self, method, params=None):
        body = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": method, "params": params or {},
        }).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.username:
            credentials = base64.b64encode(
                (self.username + ":" + self.password).encode("utf-8")
            ).decode("ascii")
            headers["Authorization"] = "Basic " + credentials

        class DenyRedirects(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise KodiBridgeError("Kodi RPC redirected unexpectedly")

        opener = build_opener(DenyRedirects)
        request = Request(self.rpc_url, data=body, headers=headers, method="POST")
        try:
            with opener.open(request, timeout=8) as response:
                if response.status != 200:
                    raise KodiBridgeError("Kodi JSON-RPC returned HTTP " + str(response.status))
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            if isinstance(exc, HTTPError) and exc.code == 401:
                raise KodiBridgeError("Kodi JSON-RPC rejected its configured username/password") from None
            raise KodiBridgeError("Unable to reach Kodi JSON-RPC: " + type(exc).__name__) from None
        if len(payload) > MAX_RESPONSE_BYTES:
            raise KodiBridgeError("Kodi JSON-RPC response exceeds size limit")
        try:
            answer = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise KodiBridgeError("Kodi JSON-RPC returned invalid JSON") from None
        if not isinstance(answer, dict) or answer.get("jsonrpc") != "2.0":
            raise KodiBridgeError("Kodi JSON-RPC returned an unexpected response")
        if "error" in answer:
            error = answer["error"]
            code = error.get("code", "unknown") if isinstance(error, dict) else "unknown"
            raise KodiBridgeError("Kodi JSON-RPC rejected the request (error " + str(code) + ")")
        return answer.get("result")

    def ping(self):
        return self.call("JSONRPC.Ping") == "pong"

    def play(self, url):
        if not plugin_url_allowed(url):
            raise KodiBridgeError("Only trusted Foxtel Kodi plugin:// URLs can be played")
        return self.call("Player.Open", {"item": {"file": url}})


class KodiFoxtelPort:
    """Kodi companion with a local read-only playlist and fixed RPC connection."""

    def __init__(self, playlist_path, remote=None):
        self.playlist_path = playlist_path
        self.remote = remote

    def channels(self):
        return read_kodi_playlist(self.playlist_path)

    def public_channels(self):
        return [{key: val for key, val in row.items() if not key.startswith("_")}
                for row in self.channels()]

    def status(self):
        channels = self.channels()
        return {
            "engine": "external-kodi",
            "channels": len(channels),
            "kodi_configured": self.remote is not None,
            "foxtel_login_via_kodi_only": True,
            "jellyfin_drm_playback": False,
            "kodi_reachable": self.remote.ping() if self.remote else False,
        }

    def play(self, channel_id):
        if not self.remote:
            raise KodiBridgeError("KODI_RPC_URL has not been configured")
        if not isinstance(channel_id, str) or not re.fullmatch(r"[a-f0-9]{24}", channel_id):
            raise KodiBridgeError("Invalid channel identifier")
        match = next((item for item in self.channels()
                      if item["channel_id"] == channel_id), None)
        if match is None:
            raise KodiBridgeError("Channel not found in local Kodi playlist")
        self.remote.play(match["_play_url"])
        return {"sent_to_kodi": True, "channel": match["name"],
                "playback": "Kodi (not Jellyfin)", "stream_verified": False}
