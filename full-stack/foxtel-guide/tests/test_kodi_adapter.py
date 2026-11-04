"""Offline security and behaviour tests for the optional Kodi Foxtel companion."""
import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kodi_adapter import (
    KodiBridgeError, KodiFoxtelPort, KodiRemote, parse_kodi_playlist,
    plugin_url_allowed, read_kodi_playlist,
)

SAMPLE_M3U = """#EXTM3U
#EXTINF:-1 tvg-id="F1S" tvg-name="Example Foxtel" group-title="Foxtel",Example Foxtel
plugin://plugin.video.foxtel.go/?_=play&id=3889324&media_type=1&_is_live=1
#EXTINF:-1 tvg-id="F2S" tvg-name="Example Now",Example Now
plugin://plugin.video.foxtel.now/?_=play&id=3889325
#EXTINF:-1 tvg-id="BAD" tvg-name="External",External
https://example.org/video.m3u8
#EXTINF:-1 tvg-id="CMD" tvg-name="Wrong Kodi plugin",Wrong Kodi plugin
plugin://plugin.video.youtube/?action=play
"""


class KodiCompanionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "kodi.m3u"
        self.path.write_text(SAMPLE_M3U, encoding="utf-8")

    def test_only_foxtel_plugin_entries_imported(self):
        entries = read_kodi_playlist(self.path)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["tvg_id"], "F1S")
        self.assertEqual(entries[1]["addon"], "plugin.video.foxtel.now")

    def test_deduplicates_plugin_urls(self):
        source = SAMPLE_M3U + SAMPLE_M3U
        self.assertEqual(len(parse_kodi_playlist(source)), 2)

    def test_rejects_arbitrary_plugins_and_embedded_credentials(self):
        rejected = [
            "plugin://plugin.video.youtube/?action=play",
            "plugin://plugin.video.foxtel.go.evil/?_=play",
            "plugin://user:password@plugin.video.foxtel.go/",
            "https://example.org/stream.m3u8",
            "plugin://plugin.video.foxtel.go/\n",
            "plugin://plugin.video.foxtel.go/#fragment",
            "file:///etc/passwd",
        ]
        for url in rejected:
            with self.subTest(url=url):
                self.assertFalse(plugin_url_allowed(url))
        self.assertTrue(plugin_url_allowed("plugin://plugin.video.foxtel.go/?_=play&id=1"))

    def test_missing_file_yields_no_channels(self):
        self.path.unlink()
        self.assertEqual(read_kodi_playlist(self.path), [])

    def test_public_catalog_hides_plugin_urls(self):
        port = KodiFoxtelPort(str(self.path))
        entries = port.public_channels()
        self.assertEqual(len(entries), 2)
        self.assertNotIn("_play_url", entries[0])
        self.assertTrue(entries[0]["channel_id"])

    def test_remote_not_configured_fails_closed(self):
        port = KodiFoxtelPort(str(self.path))
        with self.assertRaisesRegex(KodiBridgeError, "not been configured"):
            port.play(port.public_channels()[0]["channel_id"])

    def test_invalid_channel_id_fails(self):
        remote = Mock()
        port = KodiFoxtelPort(str(self.path), remote)
        with self.assertRaisesRegex(KodiBridgeError, "Invalid channel"):
            port.play("../etc/passwd")
        remote.play.assert_not_called()

    def test_selected_channel_is_sent_only_to_kodi(self):
        remote = Mock()
        port = KodiFoxtelPort(str(self.path), remote)
        channel = port.public_channels()[0]
        result = port.play(channel["channel_id"])
        self.assertTrue(result["sent_to_kodi"])
        self.assertFalse(result["stream_verified"])
        remote.play.assert_called_once_with(
            "plugin://plugin.video.foxtel.go/?_=play&id=3889324&media_type=1&_is_live=1"
        )

    def test_missing_channel_does_not_call_rpc(self):
        remote = Mock()
        port = KodiFoxtelPort(str(self.path), remote)
        with self.assertRaisesRegex(KodiBridgeError, "not found"):
            port.play("0" * 24)
        remote.play.assert_not_called()

    def test_reject_bad_rpc_configuration(self):
        invalid_urls = [
            "file:///etc/shadow",
            "http://user:pass@127.0.0.1:8080/jsonrpc",
            "https://127.0.0.1:8080/forward?target=abc",
            "http://127.0.0.1:8080/jsonrpc#fragment",
            "http://127.0.0.1:bad/jsonrpc",
        ]
        for url in invalid_urls:
            with self.subTest(url=url), self.assertRaises(KodiBridgeError):
                KodiRemote(url)

    def test_kodi_rpc_ping_and_play_with_basic_auth(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                received.append((self.path, self.headers.get("Authorization"),
                                 json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                result = "pong" if received[-1][2]["method"] == "JSONRPC.Ping" else "OK"
                self.wfile.write(json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode())
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        remote = KodiRemote(f"http://127.0.0.1:{server.server_port}", "kodi", "secret")
        self.assertTrue(remote.ping())
        remote.play("plugin://plugin.video.foxtel.go/?_=play&id=42")
        self.assertEqual(received[1][2]["method"], "Player.Open")
        self.assertEqual(
            received[1][2]["params"]["item"]["file"],
            "plugin://plugin.video.foxtel.go/?_=play&id=42",
        )
        self.assertEqual(received[0][0], "/jsonrpc")
        self.assertEqual(
            received[0][1], "Basic " + base64.b64encode(b"kodi:secret").decode()
        )


if __name__ == "__main__":
    unittest.main()
