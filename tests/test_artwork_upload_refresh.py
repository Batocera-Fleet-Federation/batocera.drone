"""Issue #685: an uploaded artwork file must be stored intact and show up on the refreshed detail page."""
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import drone_api
from app.common.http_cache import ExpiringLRUCache


class ArtworkUploadRefreshTests(unittest.TestCase):
    def _handler(self, tmp):
        root = Path(tmp)
        with mock.patch.dict(os.environ, {
            "USERDATA_ROOT": tmp, "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"), "DRONE_DEVICE_ID": "upload-refresh",
        }, clear=True):
            settings = drone_api.Settings.from_env()
        system = settings.roms_root / "snes"
        system.mkdir(parents=True)
        (system / "Game.zip").write_bytes(b"rom")
        (system / "gamelist.xml").write_text(
            "<gameList><game><path>./Game.zip</path><name>Game</name></game></gameList>", encoding="utf-8")
        handler = drone_api.RomRequestHandler.__new__(drone_api.RomRequestHandler)
        handler.settings = settings
        handler.repository = drone_api.RomRepository(settings.roms_root, settings.bios_root, settings=settings)
        handler.json_cache = ExpiringLRUCache(ttl_seconds=3600, max_items=10, max_bytes=1 << 20)
        return handler

    def _upload(self, handler, file_bytes, filename="cover.png"):
        boundary = "----WebKitFormBoundaryRefresh685"
        parts = []
        for key, value in (("field", "image"), ("system", "snes"), ("rom_id", ""), ("rom_path", "Game.zip")):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"'
            "\r\nContent-Type: application/octet-stream\r\n\r\n".encode() + file_bytes + b"\r\n")
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        handler.headers = {"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        responses = []
        handler._send_json = lambda code, payload: responses.append((code, payload))
        handler._handle_admin_artwork_upload()
        return responses[-1]

    def test_file_bytes_ending_like_multipart_framing_are_not_truncated(self):
        for tail in (b"--", b"-\r\n", b"\r", b"\n\n", b"-\r-\n--"):
            with self.subTest(tail=tail), tempfile.TemporaryDirectory() as tmp:
                handler = self._handler(tmp)
                data = b"\x89PNG\r\n\x1a\nbinary" + tail
                status, payload = self._upload(handler, data)
                self.assertEqual(status, 200)
                self.assertEqual(Path(payload["path"]).read_bytes(), data)
                self.assertEqual(payload["existing"]["image"], "./images/Game-image.png")

    def test_upload_drops_cached_rom_lists_so_the_refresh_sees_new_artwork(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = self._handler(tmp)
            handler.json_cache.put("json:/systems/snes?fingerprint=0", b'{"roms": []}')
            self.assertEqual(self._upload(handler, b"image-bytes")[0], 200)
            self.assertIsNone(handler.json_cache.get("json:/systems/snes?fingerprint=0"))


if __name__ == "__main__":
    unittest.main()
