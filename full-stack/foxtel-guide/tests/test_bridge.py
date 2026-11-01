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
from urllib.request import urlopen
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
        self.assertIn('tvg-name="Demo  One "', output)
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


if __name__ == "__main__":
    unittest.main()
