"""Network UAT for Issue #302: an unconfigured Drone has no shared login.

The security invariant is derived from the issue, rather than the implementation:

* a fresh credential state must not accept the documented ``batocera/linux`` pair;
* a malformed persisted credential record must fail closed rather than revive that
  pair; and
* server startup may provision a local recovery secret, but its plaintext must
  not be placed in the state database or returned by the login endpoint.

The live-server case deliberately uses a real TCP listener and the public login
route.  HTTP-only is used solely to keep this stdlib UAT independent of local
certificate tooling; the same handler and credential store serve HTTPS in normal
operation.
"""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import app.drone_api as drone_api
from app.common.auth import DroneCredentialStore, SessionAuth, SessionStore
from app.common.settings import Settings
from app.storage.state_store import load_payload, save_payload


_QUIET_FLAGS = (
    "_ROM_METADATA_POLLER_STARTED",
    "_ROM_METADATA_WATCHER_STARTED",
    "_GAME_PROCESS_MONITOR_STARTED",
    "_AUTOMATION_POLLER_STARTED",
    "_VPN_AUTO_CONNECT_ATTEMPTED",
    "_VPN_SHARING_POLLER_STARTED",
    "_VPN_SELF_HEAL_POLLER_STARTED",
    "_SMTP_BOOTSTRAP_ATTEMPTED",
    "_SMTP_SHARING_POLLER_STARTED",
    "_AUDIT_EMAIL_POLLER_STARTED",
    "_NETWORK_SHARE_BOOT_REPLAY_ATTEMPTED",
    "_NETWORK_SHARE_WATCHDOG_STARTED",
    "_NFS_EXPORT_BOOT_REPLAY_ATTEMPTED",
    "_TAILNET_BOOTSTRAP_ATTEMPTED",
    "_TAILNET_SHARING_POLLER_STARTED",
    "_STREAMDECK_SUPERVISOR_STARTED",
)


def _login(port: int, username: str, password: str) -> tuple[int, dict]:
    body = json.dumps({"username": username, "password": password}).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("POST", "/v1/api/auth/login", body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()


class NoDefaultCredentialStoreTests(unittest.TestCase):
    def test_fresh_store_rejects_documented_pair_until_local_secret_exists(self) -> None:
        with tempfile.TemporaryDirectory(prefix="issue-302-fresh-") as tmp:
            root = Path(tmp)
            path = root / "credentials.json"
            database = root / "state.sqlite3"
            store = DroneCredentialStore(path, state_database_file=database)
            auth = SessionAuth(store, SessionStore(database))

            self.assertTrue(store.setup_required())
            self.assertFalse(store.check("batocera", "linux"))
            self.assertIsNone(auth.login("batocera", "linux"))

            with mock.patch("app.common.auth.secrets.token_urlsafe", return_value="local-only-one-time-secret"):
                initial_file = store.ensure_initial_password()
            self.assertEqual(initial_file, root / "initial-password.txt")
            self.assertEqual(initial_file.stat().st_mode & 0o777, 0o600)
            self.assertFalse(store.check("batocera", "linux"))
            self.assertTrue(store.check("batocera", "local-only-one-time-secret"))
            self.assertNotIn("local-only-one-time-secret", str(load_payload(database, "credentials", {})))


class ServerBootstrapCredentialBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="issue-302-server-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.userdata = self.root / "userdata"
        self.database = self.root / "state.sqlite3"
        self.server = None
        self.thread = None

    def tearDown(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def test_malformed_credentials_are_replaced_by_local_secret_not_default_over_tcp(self) -> None:
        # A partial record is a realistic interrupted/malformed legacy state: it
        # names the conventional user but contains no verifiable secret.
        save_payload(self.database, "credentials", {"username": "batocera"})
        env = {
            "USERDATA_ROOT": str(self.userdata),
            "ROMS_ROOT": str(self.userdata / "roms"),
            "BIOS_ROOT": str(self.userdata / "bios"),
            "SAVES_ROOT": str(self.userdata / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(self.database),
            "DRONE_DEVICE_ID": "issue-302-test-drone",
            "HTTPS_PORT": "0",
            "DRONE_COMPAT_HTTPS_PORTS": "",
            "DRONE_PEER_MTLS_PORT": "0",
            "HTTP_ONLY": "1",
            "DRONE_CAST_ENABLED": "0",
            "ROM_METADATA_POLL_SECONDS": "0",
            "USE_FAKE_DATA": "1",
        }
        flags = {name: True for name in _QUIET_FLAGS}
        flags.update({"_DOWNLOAD_MANAGER": object(), "_TORRENT_MANAGER": object()})
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.multiple(drone_api, **flags), \
                mock.patch.object(drone_api._identity, "swarm_networking_allowed", return_value=False), \
                mock.patch.object(drone_api, "DroneCertificateManager") as certificate_manager, \
                mock.patch("app.common.auth.secrets.token_urlsafe", return_value="tcp-local-one-time-secret"):
            certificate_manager.return_value.ensure_certificate.return_value = {}
            self.server = drone_api.create_server(Settings.from_env())
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()

            port = self.server.server_address[1]
            status, rejected = _login(port, "batocera", "linux")
            self.assertEqual(status, 401)
            self.assertEqual(rejected, {"error": "invalid username or password"})

            status, accepted = _login(port, "batocera", "tcp-local-one-time-secret")
            self.assertEqual(status, 200)
            self.assertEqual(accepted, {"status": "ok", "username": "batocera"})
            self.assertNotIn("tcp-local-one-time-secret", json.dumps(load_payload(self.database, "credentials", {})))


if __name__ == "__main__":
    unittest.main()
