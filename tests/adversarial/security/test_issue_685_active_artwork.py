"""Regression: uploaded artwork must not execute script in the Drone origin.

Confidence: high. The ROM detail page opens artwork in a top-level tab.
Exercise the actual upload and public-image handlers with a harmless SVG
marker; no browser script, network request, or device operation is executed.
"""

import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from app import drone_api


class UploadedArtworkScriptBoundaryTests(unittest.TestCase):
    def test_uploaded_svg_is_not_served_as_an_active_same_origin_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {
                "USERDATA_ROOT": tmp,
                "ROMS_ROOT": str(root / "roms"),
                "BIOS_ROOT": str(root / "bios"),
                "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
                "DRONE_DEVICE_ID": "security-artwork-fixture",
            }, clear=True):
                settings = drone_api.Settings.from_env()
            system = settings.roms_root / "snes"
            system.mkdir(parents=True)
            (system / "Game.zip").write_bytes(b"local fixture")
            (system / "gamelist.xml").write_text(
                "<gameList><game><path>./Game.zip</path>"
                "<name>Game</name></game></gameList>", encoding="utf-8",
            )
            handler = drone_api.RomRequestHandler.__new__(drone_api.RomRequestHandler)
            handler.settings = settings
            handler.repository = drone_api.RomRepository(
                settings.roms_root, settings.bios_root, settings=settings,
            )
            svg = (
                b'<svg xmlns="http://www.w3.org/2000/svg"><script>'
                b'document.documentElement.setAttribute("data-security-probe", "executed")'
                b'</script></svg>'
            )
            boundary = "SecurityReviewLocalFixture"
            parts = []
            for key, value in (("field", "image"), ("system", "snes"), ("rom_path", "./Game.zip")):
                parts.append(
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"'
                    f'\r\n\r\n{value}\r\n'.encode()
                )
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="cover.svg"'
                '\r\nContent-Type: image/svg+xml\r\n\r\n'.encode() + svg + b"\r\n"
            )
            body = b"".join(parts) + f"--{boundary}--\r\n".encode()
            handler.headers = {
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            }
            handler.rfile = io.BytesIO(body)
            responses = []
            handler._send_json = lambda code, payload: responses.append((code, payload))
            try:
                handler._handle_admin_artwork_upload()
            except ValueError:
                return  # Rejecting active artwork before publishing it is safe.
            status, uploaded = responses[-1]
            if 400 <= status < 500:
                return
            self.assertEqual(status, 200)

            # Cache misses only: serving, MIME selection, bytes, and CSP remain real.
            handler.image_miss_cache = mock.Mock()
            handler.image_miss_cache.has.return_value = False
            handler.image_cache = mock.Mock()
            handler.image_cache.get.return_value = None
            headers = {}
            handler.send_response = lambda code: headers.update(status=code)
            handler.send_header = lambda key, value: headers.update({key.lower(): value})
            handler.end_headers = lambda: None
            handler.wfile = io.BytesIO()
            try:
                handler._handle_public_image("snes", Path(uploaded["path"]).name)
            except (ValueError, FileNotFoundError):
                return  # Refusing to serve active content is also safe.
            self.assertEqual(headers["status"], 200)
            csp = dict(
                (tokens[0], tokens[1:])
                for part in headers.get("content-security-policy", "").split(";")
                if (tokens := part.strip().split())
            )
            script_policy = csp.get("script-src-elem", csp.get("script-src", csp.get("default-src")))
            inline_allowed = script_policy is None or "'unsafe-inline'" in script_policy
            sandbox = csp.get("sandbox")
            isolated = sandbox is not None and (
                "allow-scripts" not in sandbox or "allow-same-origin" not in sandbox
            )
            active_document = headers.get("content-type", "").split(";", 1)[0] in {
                "image/svg+xml", "text/html", "application/xhtml+xml",
            }
            attachment = headers.get("content-disposition", "").lower().startswith("attachment")
            self.assertFalse(
                b"<script>" in handler.wfile.getvalue()
                and active_document and inline_allowed and not isolated and not attachment,
                "Uploaded scripted SVG is served unchanged in the Drone origin with "
                "inline scripts enabled; the ROM detail artwork link opens this document.",
            )


if __name__ == "__main__":
    unittest.main()
