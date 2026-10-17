"""create_server must release every listener it bound when startup fails part-way (#325)."""

import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.drone_api as drone_api
from app.common import identity_provisioning as identity
from app.common.settings import Settings
from app.transfer import peer_workers


def _free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return port


class CreateServerListenerCleanupTests(unittest.TestCase):
    def test_failed_worker_start_releases_peer_mtls_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "USERDATA_ROOT": str(root / "userdata"),
                "ROMS_ROOT": str(root / "roms"),
                "BIOS_ROOT": str(root / "bios"),
                "TLS_SELF_SIGNED_DIR": str(root / "certs"),
                "SAVES_ROOT": str(root / "saves"),
                "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
                "DRONE_DEVICE_ID": "listener-cleanup-test",
                "HTTPS_PORT": str(_free_port()),
                "DRONE_PEER_MTLS_PORT": str(_free_port()),
                "HTTP_REDIRECT_PORT": "0",
                "CAST_ENABLED": "0",
                "ROM_METADATA_POLL_SECONDS": "0",
                "ROM_METADATA_WATCH_ENABLED": "0",
            }
            with mock.patch.dict("os.environ", env, clear=True):
                settings = Settings.from_env()
            peer_port = settings.peer_mtls_port
            with mock.patch.object(drone_api._identity, "swarm_networking_allowed", return_value=True), \
                    mock.patch.object(drone_api, "_start_local_network_workers", side_effect=RuntimeError("boom")), \
                    mock.patch.object(drone_api, "_start_automation_poller"), \
                    mock.patch.object(drone_api, "_start_multiplayer_worker"):
                with self.assertRaises(RuntimeError):
                    drone_api.create_server(settings)
            peer_workers.stop_local_network_workers(timeout=1.0)

            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind(("0.0.0.0", peer_port))
            finally:
                probe.close()


if __name__ == "__main__":
    unittest.main()
