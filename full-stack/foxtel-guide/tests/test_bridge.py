"""Offline tests: no Foxtel credentials, subscriptions or network required."""
import gzip
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import Bridge, make_handler, playlist, read_guide, validate_stream_url, xmltv_filter

SAMPLE = b"""<?xml version="1.0"?>
<tv><channel id="F1S"><display-name>Example One</display-name></channel>
<channel id="UKT"><display-name>Example Two</display-name></channel>
<programme channel="F1S" start="20261101120000 +1100" stop="20261101130000 +1100"><title>Test &amp; Trial</title></programme>
<programme channel="UKT" start="20261101120000 +1100" stop="20261101130000 +1100"><title>Other</title></programme></tv>"""


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.guide = self.dir / "epg.xml.gz"
        self.guide.write_bytes(gzip.compress(SAMPLE))
        self.config = self.dir / "channels.json"
        self.config.write_text(json.dumps({"channels": [{
            "tvg_id": "F1S", "name": 'Demo "One"', "stream_url": "https://example.org/live.m3u8",
            "group": "Licensed streams"
        }]}), encoding="utf-8")
        self.bridge = Bridge(self.config, str(self.guide), 300)

    def test_guide_gzip_and_xmltv_filter(self):
        root = self.bridge.guide()
        filtered = ET.fromstring(xmltv_filter(root, {"F1S"}))
        self.assertEqual(len(filtered.findall("channel")), 1)
        self.assertEqual(len(filtered.findall("programme")), 1)
        self.assertEqual(filtered.find("programme/title").text, "Test & Trial")

    def test_playlist_escaping_and_stream(self):
        output = playlist(self.bridge.channels()).decode()
        self.assertIn('tvg-id="F1S"', output)
        self.assertIn('tvg-name="Demo  One"', output)
        self.assertIn("https://example.org/live.m3u8", output)
        self.assertNotIn('tvg-name="Demo "One""', output)

    def test_invalid_stream_url(self):
        for url in ["file:///etc/passwd", "javascript:alert(1)", "https://a.test/\n#EXTINF", "https://user:password@a.test", "http://"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_stream_url(url)

    def test_duplicate_channels_rejected(self):
        rows = json.loads(self.config.read_text())["channels"]
        self.config.write_text(json.dumps({"channels": rows + rows}))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.bridge.channels()

    def test_doctype_rejected(self):
        path = self.dir / "bad.xml"
        path.write_bytes(b'<!DOCTYPE tv [<!ENTITY lol "expansion">]><tv/>')
        with self.assertRaisesRegex(ValueError, "declarations"):
            read_guide(str(path))

    def test_missing_channels_file_returns_empty(self):
        self.config.unlink()
        self.assertEqual(self.bridge.channels(), [])
        self.assertEqual(playlist([]), b"#EXTM3U\n")

    def test_token_auth_and_http_endpoints(self):
        token = "a" * 32
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.bridge, token))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:" + str(server.server_port)
        with self.assertRaises(HTTPError) as unauth:
            urlopen(base + "/channels.m3u", timeout=3)
        self.assertEqual(unauth.exception.code, 401)
        with urlopen(base + "/channels.m3u?token=" + token, timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"#EXTM3U", response.read())
        with urlopen(base + "/catalog?token=" + token, timeout=3) as response:
            ids = [v["tvg_id"] for v in json.load(response)["channels"]]
            self.assertEqual(ids, ["F1S", "UKT"])
        with urlopen(base + "/guide.xml?token=" + token, timeout=3) as response:
            self.assertEqual(len(ET.fromstring(response.read()).findall("programme")), 1)
        with urlopen(base + "/status?token=" + token, timeout=3) as response:
            status = json.load(response)
            self.assertFalse(status["foxtel_now_login_supported"])
            self.assertEqual(status["configured_streams"], 1)


    def test_kodi_http_authenticated_ui_and_remote_launch(self):
        token = "b" * 32
        kodi_playlist = self.dir / "kodi-foxtel.m3u"
        kodi_playlist.write_text(
            '#EXTM3U\n#EXTINF:-1 tvg-id="F1S",Foxtel Example\n'
            'plugin://plugin.video.foxtel.go/?_=play&id=3889324&media_type=1\n',
            encoding="utf-8",
        )
        self.bridge.kodi.playlist_path = str(kodi_playlist)
        self.bridge.kodi.remote = Mock()
        self.bridge.kodi.remote.ping.return_value = True
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.bridge, token))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with urlopen(base + "/kodi/ui?token=" + token, timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"Play on Kodi", response.read())
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        with urlopen(base + "/kodi/channels?token=" + token, timeout=3) as response:
            catalog = json.load(response)
        self.assertEqual(len(catalog["channels"]), 1)
        channel_id = catalog["channels"][0]["channel_id"]
        self.assertNotIn("_play_url", catalog["channels"][0])
        unauthorised = Request(
            base + "/kodi/play",
            data=json.dumps({"channel_id": channel_id}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as rejected:
            urlopen(unauthorised, timeout=3)
        self.assertEqual(rejected.exception.code, 401)
        self.bridge.kodi.remote.play.assert_not_called()
        authorised = Request(
            base + "/kodi/play",
            data=json.dumps({"channel_id": channel_id}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        with urlopen(authorised, timeout=3) as response:
            self.assertEqual(response.status, 202)
            self.assertTrue(json.load(response)["sent_to_kodi"])
        self.bridge.kodi.remote.play.assert_called_once_with(
            "plugin://plugin.video.foxtel.go/?_=play&id=3889324&media_type=1"
        )


if __name__ == "__main__":
    unittest.main()
