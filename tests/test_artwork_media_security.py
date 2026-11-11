"""Untrusted artwork stays inert through every ROM detail serving path."""

import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from app import drone_api
from app.common.http_cache import ExpiringKeyCache, ExpiringLRUCache


class ArtworkMediaSecurityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        with mock.patch.dict(os.environ, {
            "USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"), "DRONE_DEVICE_ID": "artwork-test",
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        }, clear=True):
            settings = drone_api.Settings.from_env()
        self.system = settings.roms_root / "snes"
        (self.system / "images").mkdir(parents=True)
        (self.system / "Game.zip").write_bytes(b"ROM")
        self.handler = drone_api.RomRequestHandler.__new__(drone_api.RomRequestHandler)
        self.handler.settings = settings
        self.handler.repository = drone_api.RomRepository(settings.roms_root, settings.bios_root, settings=settings)
        self.handler.image_cache = ExpiringLRUCache(3600, 20, 1024 * 1024)
        self.handler.image_miss_cache = ExpiringKeyCache(60)
        self.handler.send_response = lambda status: self.assertEqual(status, 200)
        self.handler.end_headers = lambda: None

    def response(self, callback):
        headers = {}
        self.handler.send_header = lambda key, value: headers.setdefault(key, []).append(value)
        self.handler.wfile = io.BytesIO()
        callback()
        return headers, self.handler.wfile.getvalue()

    def assert_inert(self, headers):
        self.assertEqual(len(headers["Content-Security-Policy"]), 1)
        directives = dict(
            (parts[0], parts[1:])
            for directive in headers["Content-Security-Policy"][0].split(";")
            if (parts := directive.strip().split())
        )
        self.assertEqual(directives["sandbox"], [])
        self.assertEqual(directives["script-src"], ["'none'"])
        self.assertEqual(directives["default-src"], ["'none'"])
        self.assertEqual(directives["form-action"], ["'none'"])
        self.assertEqual(headers["X-Content-Type-Options"], ["nosniff"])

    def test_active_images_are_sandboxed_on_cache_miss_hit_and_case_fallback(self):
        for extension, mime, data in (
            ("svg", "image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>alert(2)</script></svg>'),
            ("html", "text/html; charset=utf-8", b'<script src="/evil.js"></script><form action="/v1/api/admin/roms/delete"></form>'),
        ):
            path = self.system / "images" / f"Game-image.{extension}"
            path.write_bytes(data)
            for filename in (path.name, path.name, path.name.upper()):
                with self.subTest(filename=filename):
                    headers, body = self.response(lambda: self.handler._handle_public_image("snes", filename))
                    self.assert_inert(headers)
                    self.assertEqual(headers["Content-Type"], [mime])
                    self.assertEqual(headers["Cache-Control"], ["public, max-age=3600"])
                    self.assertEqual(body, data)
            self.assertIsNotNone(self.handler.image_cache.get(str(path.resolve())))

    def test_raster_artwork_and_stem_fallback_preserve_bytes_and_mime(self):
        data = b"\x89PNG\r\n\x1a\nfixture"
        path = self.system / "images" / "nested" / "Game-image.png"
        path.parent.mkdir()
        path.write_bytes(data)
        for _ in range(2):
            headers, body = self.response(lambda: self.handler._handle_public_image("snes", "Game.jpg"))
            self.assert_inert(headers)
            self.assertEqual(headers["Content-Type"], ["image/png"])
            self.assertEqual(body, data)

    def test_video_reference_cannot_bypass_artwork_sandbox(self):
        for extension, mime, data in (
            ("svg", "image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'),
            ("mp4", "video/mp4", b"video fixture"),
            ("webm", "video/webm", b"webm fixture"),
        ):
            with self.subTest(extension=extension):
                path = self.system / "images" / f"Game-video.{extension}"
                path.write_bytes(data)
                (self.system / "gamelist.xml").write_text(
                    "<gameList><game><path>./Game.zip</path>"
                    f"<video>./images/{path.name}</video></game></gameList>", encoding="utf-8",
                )
                headers, body = self.response(lambda: self.handler._handle_public_video("snes", "Game.zip"))
                self.assert_inert(headers)
                self.assertEqual(headers["Content-Type"], [mime])
                self.assertEqual(body, data)
                self.assertNotIn("Content-Disposition", headers)

    def test_application_documents_keep_their_script_policy(self):
        headers, _ = self.response(self.handler._send_security_headers)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"][0])
        self.assertNotIn("sandbox", headers["Content-Security-Policy"][0])


if __name__ == "__main__":
    unittest.main()
