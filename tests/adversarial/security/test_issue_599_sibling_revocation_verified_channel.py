"""A forged plaintext 404 must not erase imported SMTP or Tailnet secrets."""

import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.device import smtp_manager, tailnet_service
from app.transfer import peer_connectivity as pc


SOURCE = "source-599-sibling"


class _NotFoundServer:
    def __init__(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


class SiblingRevocationVerifiedChannelTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        env = {
            "USERDATA_ROOT": str(self.root),
            "ROMS_ROOT": str(self.root / "roms"),
            "BIOS_ROOT": str(self.root / "bios"),
            "SAVES_ROOT": str(self.root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(self.root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "victim-599-sibling",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            self.settings = Settings.from_env()
        self.server = _NotFoundServer()
        self.addCleanup(self.server.close)
        for patcher in (
            mock.patch.object(pc, "_identity_swarm_networking_allowed", return_value=True),
            mock.patch.object(pc, "_candidate_dialable", return_value=True),
            mock.patch("app.transfer.local_network.get_paired_peer", side_effect=lambda *_args: self.peer),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.peer = {
            "drone_id": SOURCE,
            "scheme": "http",
            "paired_over_tls": False,
            "local_ip": "127.0.0.1",
            "api_port": self.server.port,
        }

    def test_plaintext_404_preserves_imported_smtp_password(self):
        smtp_manager.import_from_peer(self.settings, {
            "host": "smtp.example.test",
            "port": 587,
            "use_starttls": True,
            "use_ssl": False,
            "username": "relay-user",
            "password": "smtp-secret",
            "from_address": "drone@example.test",
            "recipient_email": "owner@example.test",
        }, source_peer_id=SOURCE)

        self.assertFalse(smtp_manager.check_sharing_revocation(self.settings))

        state = smtp_manager._load_state(self.settings)
        self.assertEqual(state["password"], "smtp-secret")
        self.assertEqual(state["revoked_reason"], "")

    def test_plaintext_404_preserves_imported_tailnet_auth_key(self):
        tailnet_service._save_sharing_state(
            self.settings, auth_key="tskey-secret", source_peer_id=SOURCE,
        )

        self.assertFalse(tailnet_service.check_tailnet_sharing_revocation(self.settings))

        state = tailnet_service._load_sharing_state(self.settings)
        self.assertEqual(state["auth_key"], "tskey-secret")
        self.assertEqual(state["revoked_reason"], "")


if __name__ == "__main__":
    unittest.main()
