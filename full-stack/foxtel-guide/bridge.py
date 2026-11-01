"""Jellyfin M3U/XMLTV adapter for independently authorised, unencrypted streams.

This does not authenticate to Foxtel Now, extract Foxtel streams, or handle DRM.
"""
import gzip
import hmac
import io
import json
import os
from pathlib import Path
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

PORT = int(os.environ.get("BRIDGE_PORT", "8765"))
MAX_COMPRESSED = 8 * 1024 * 1024
MAX_XML = 32 * 1024 * 1024
DEFAULT_GUIDE_URL = "https://i.mjh.nz/Foxtel/epg.xml.gz"
SAFE_TEXT = re.compile(r"[\x00-\x1f\x7f\"]")


def clean(value):
    return SAFE_TEXT.sub(" ", str(value)).strip()


def validate_stream_url(value):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ValueError("stream_url must be an HTTP(S) URL without control characters")
    url = urlsplit(value)
    if url.scheme not in ("https", "http") or not url.hostname or url.username or url.password:
        raise ValueError("stream_url must be HTTP(S) and must not contain embedded credentials")
    try:
        _ = url.port
    except ValueError as exc:
        raise ValueError("stream_url has an invalid port") from exc
    return value


def playlist(channels):
    rows = ["#EXTM3U"]
    for channel in channels:
        props = {
            "tvg-id": clean(channel["tvg_id"]),
            "tvg-name": clean(channel["name"]),
            "group-title": clean(channel.get("group", "Authorised TV")),
        }
        if channel.get("logo"):
            props["tvg-logo"] = clean(channel["logo"])
        attributes = " ".join(f'{key}="{value}"' for key, value in props.items())
        rows += [f'#EXTINF:-1 {attributes},{clean(channel["name"])}', channel["stream_url"]]
    return ("\n".join(rows) + "\n").encode("utf-8")


def xmltv_filter(root, channel_ids):
    output = ET.Element("tv", {"generator-info-name": "Jellyfin authorised-stream guide bridge"})
    for element in root:
        if element.tag == "channel" and element.get("id") in channel_ids:
            output.append(element)
        elif element.tag == "programme" and element.get("channel") in channel_ids:
            output.append(element)
    return ET.tostring(output, encoding="utf-8", xml_declaration=True)


def read_guide(source):
    parsed = urlsplit(source)
    if parsed.scheme in ("http", "https"):
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("Remote EPG source must be HTTPS")
        req = Request(source, headers={"User-Agent": "JellyfinGuideBridge/1.0", "Accept": "application/xml, application/gzip"})
        with urlopen(req, timeout=20) as response:
            if urlsplit(response.geturl()).scheme != "https":
                raise ValueError("Remote EPG redirects must remain HTTPS")
            data = response.read(MAX_COMPRESSED + 1)
            encoding = response.headers.get("Content-Encoding", "")
    elif parsed.scheme == "" and source.startswith("/"):
        data = Path(source).read_bytes()
        encoding = ""
    else:
        raise ValueError("EPG source must be an HTTPS URL or absolute local file path")
    if len(data) > MAX_COMPRESSED:
        raise ValueError("EPG download exceeds size limit")
    if data[:2] == b"\x1f\x8b" or "gzip" in encoding.lower():
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            data = stream.read(MAX_XML + 1)
    if len(data) > MAX_XML:
        raise ValueError("EPG decompressed XML exceeds size limit")
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("EPG XML declarations are not allowed")
    root = ET.fromstring(data)
    if root.tag != "tv":
        raise ValueError("EPG is not XMLTV (expected <tv>)")
    return root


class Bridge:
    def __init__(self, config_file, guide_url, refresh_seconds=21600):
        self.config_file = Path(config_file)
        self.guide_url = guide_url
        self.refresh_seconds = max(300, int(refresh_seconds))
        self._root = None
        self._next_refresh = 0
        self._last_success = 0
        self._last_error = None
        self._lock = threading.Lock()

    def channels(self):
        if not self.config_file.exists():
            return []
        data = json.loads(self.config_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("channels"), list):
            raise ValueError("channels.json must contain a 'channels' array")
        parsed, seen = [], set()
        for row in data["channels"]:
            if not isinstance(row, dict):
                raise ValueError("Each channel must be an object")
            tvg_id = row.get("tvg_id")
            if not isinstance(tvg_id, str) or not tvg_id.strip() or len(tvg_id) > 128 or clean(tvg_id) != tvg_id:
                raise ValueError("Invalid tvg_id in channel configuration")
            if tvg_id in seen:
                raise ValueError("Duplicate tvg_id: " + tvg_id)
            seen.add(tvg_id)
            name = row.get("name", tvg_id)
            if not isinstance(name, str) or not name.strip() or len(name) > 200:
                raise ValueError("Invalid channel name for " + tvg_id)
            stream = validate_stream_url(row.get("stream_url"))
            item = {"tvg_id": tvg_id, "name": name, "stream_url": stream}
            for key in ("group", "logo"):
                if key in row:
                    if not isinstance(row[key], str) or len(row[key]) > 500:
                        raise ValueError("Invalid " + key + " for " + tvg_id)
                    item[key] = row[key]
            parsed.append(item)
        return parsed

    def guide(self):
        now = time.monotonic()
        with self._lock:
            if now >= self._next_refresh:
                try:
                    self._root = read_guide(self.guide_url)
                    self._last_success = time.time()
                    self._last_error = None
                    self._next_refresh = time.monotonic() + self.refresh_seconds
                except Exception as exc:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    self._next_refresh = time.monotonic() + 300
            if self._root is None:
                raise RuntimeError("EPG unavailable: " + str(self._last_error))
            return self._root

    def status(self):
        try:
            count = len(self.channels())
            config_error = None
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            count, config_error = 0, str(exc)
        return {
            "service": "authorised-stream-guide-bridge",
            "foxtel_now_login_supported": False,
            "drm_playback_supported": False,
            "configured_streams": count,
            "guide_cached": self._root is not None,
            "guide_last_success_utc_epoch": self._last_success or None,
            "guide_last_error": self._last_error,
            "configuration_error": config_error,
        }


def make_handler(bridge, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "GuideBridge/1.0"

        def log_message(self, fmt, *args):
            # Never log query parameters, which may contain the Jellyfin feed token.
            print(f"{self.address_string()} {self.command} {urlsplit(self.path).path}", flush=True)

        def do_HEAD(self):
            self.handle_request(head_only=True)

        def do_GET(self):
            self.handle_request(head_only=False)

        def respond(self, code, payload, content_type, head_only):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if not head_only:
                self.wfile.write(payload)

        def send_json(self, code, data, head_only):
            self.respond(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", head_only)

        def handle_request(self, head_only):
            parts = urlsplit(self.path)
            path = parts.path
            if path == "/healthz":
                self.send_json(200, {"status": "running"}, head_only)
                return
            query_token = parse_qs(parts.query).get("token", [""])[0]
            header = self.headers.get("Authorization", "")
            supplied = header[7:] if header.startswith("Bearer ") else query_token
            if not hmac.compare_digest(supplied, token):
                self.send_json(401, {"error": "Invalid feed token"}, head_only)
                return
            try:
                if path == "/status":
                    self.send_json(200, bridge.status(), head_only)
                elif path == "/channels.m3u":
                    self.respond(200, playlist(bridge.channels()), "application/vnd.apple.mpegurl; charset=utf-8", head_only)
                elif path == "/guide.xml":
                    ids = {row["tvg_id"] for row in bridge.channels()}
                    data = xmltv_filter(bridge.guide(), ids)
                    self.respond(200, data, "application/xml; charset=utf-8", head_only)
                elif path == "/catalog":
                    root = bridge.guide()
                    entries = []
                    for channel in root.findall("channel"):
                        entries.append({"tvg_id": channel.get("id"), "name": channel.findtext("display-name", "")})
                    self.send_json(200, {"channels": entries}, head_only)
                else:
                    self.send_json(404, {"error": "Not found"}, head_only)
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)}, head_only)
            except RuntimeError as exc:
                self.send_json(503, {"error": str(exc)}, head_only)

    return Handler


def main():
    token = os.environ.get("FOXTEL_GUIDE_FEED_TOKEN", "")
    if len(token) < 24:
        raise SystemExit("FOXTEL_GUIDE_FEED_TOKEN is required (minimum 24 characters); refusing insecure startup")
    bridge = Bridge(
        os.environ.get("FOXTEL_GUIDE_CONFIG", "/data/channels.json"),
        os.environ.get("FOXTEL_GUIDE_SOURCE", DEFAULT_GUIDE_URL),
        int(os.environ.get("FOXTEL_GUIDE_REFRESH_SECONDS", "21600")),
    )
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), make_handler(bridge, token))
    print(f"Authorised-stream guide bridge listening on {PORT}; Foxtel Now account login and DRM are NOT supported", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
