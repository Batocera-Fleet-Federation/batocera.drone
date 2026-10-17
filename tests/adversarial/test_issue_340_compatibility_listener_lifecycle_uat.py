"""#340: the compatibility listener belongs to the server create_server returns.

Expected independently of the implementation: a caller that starts the returned
server and then invokes its documented shutdown/close lifecycle must be able to
start another server on every configured compatibility port in the same process.
The compatibility listener is not an independent process-global service.
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import app.drone_api as drone_api
from app.common.settings import Settings


_QUIET_FLAGS = (
    "_ROM_METADATA_POLLER_STARTED",
    "_ROM_METADATA_WATCHER_STARTED",
    "_GAME_PROCESS_MONITOR_STARTED",
    "_AUTOMATION_POLLER_STARTED",
    "_NETWORK_SHARE_BOOT_REPLAY_ATTEMPTED",
    "_NETWORK_SHARE_WATCHDOG_STARTED",
    "_NFS_EXPORT_BOOT_REPLAY_ATTEMPTED",
    "_SMTP_BOOTSTRAP_ATTEMPTED",
    "_SMTP_SHARING_POLLER_STARTED",
    "_AUDIT_EMAIL_POLLER_STARTED",
    "_TAILNET_BOOTSTRAP_ATTEMPTED",
    "_TAILNET_SHARING_POLLER_STARTED",
    "_STREAMDECK_SUPERVISOR_STARTED",
    "_VPN_AUTO_CONNECT_ATTEMPTED",
    "_VPN_SHARING_POLLER_STARTED",
    "_VPN_SELF_HEAL_POLLER_STARTED",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class CompatibilityListenerLifecycleUAT(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="issue-340-uat-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.compatibility_port = _free_port()
        self._originals = {
            name: getattr(drone_api, name)
            for name in _QUIET_FLAGS + ("_DOWNLOAD_MANAGER", "_TORRENT_MANAGER")
        }
        for name in _QUIET_FLAGS:
            setattr(drone_api, name, True)
        drone_api._DOWNLOAD_MANAGER = object()
        drone_api._TORRENT_MANAGER = object()
        self.addCleanup(self._restore_runtime)

        env = {
            "USERDATA_ROOT": str(root / "userdata"),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "issue-340-host",
            "HTTPS_PORT": "0",
            "HTTP_ONLY": "1",
            "DRONE_COMPAT_HTTPS_PORTS": str(self.compatibility_port),
            "DRONE_PEER_MTLS_PORT": "0",
            "DRONE_HTTP_REDIRECT_PORT": "0",
            "DRONE_CAST_ENABLED": "0",
            "ROM_METADATA_POLL_SECONDS": "0",
            "ROM_METADATA_WATCH_ENABLED": "0",
            "DRONE_APP_PASSWORD": "test-password",
            "TLS_SELF_SIGNED_DIR": str(root / "certs"),
        }
        environment = mock.patch.dict(os.environ, env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.settings = Settings.from_env()

    def _restore_runtime(self) -> None:
        for name, value in self._originals.items():
            setattr(drone_api, name, value)

    def _close_all(self, server) -> None:
        """Force-cleanup after an assertion failure without masking the regression."""
        for listener in getattr(server, "all_tls_servers", (server,)):
            try:
                if getattr(listener, "thread", None) is not None:
                    listener.shutdown()
            except Exception:
                pass
            try:
                listener.server_close()
            except OSError:
                pass
        for listener in getattr(server, "all_tls_servers", (server,)):
            thread = getattr(listener, "thread", None)
            if thread is not None:
                thread.join(timeout=3)

    def _start_primary(self, server) -> threading.Thread:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.assertTrue(thread.is_alive())
        return thread

    def test_shutdown_then_close_releases_compatibility_port_for_a_second_boot(self) -> None:
        with mock.patch.object(drone_api._identity, "swarm_networking_allowed", return_value=False), \
                mock.patch.object(drone_api.DroneCertificateManager, "ensure_certificate", return_value={}), \
                mock.patch.object(drone_api, "_apply_server_tls"):
            first = drone_api.create_server(self.settings)
            self.addCleanup(self._close_all, first)
            self.assertEqual([self.compatibility_port], [item.server_address[1] for item in first.compatibility_servers])
            compatibility = first.compatibility_servers[0]
            primary_thread = self._start_primary(first)

            first.shutdown()
            first.server_close()
            primary_thread.join(timeout=3)
            compatibility.thread.join(timeout=3)
            self.assertFalse(primary_thread.is_alive(), "returned server must stop its own serving loop")
            self.assertFalse(compatibility.thread.is_alive(), "returned server must stop its compatibility loop")

            # A full second create_server boot is the externally visible regression
            # boundary; a raw socket probe would not catch lifecycle bookkeeping bugs.
            second = drone_api.create_server(self.settings)
            self.addCleanup(self._close_all, second)
            self.assertEqual([self.compatibility_port], [item.server_address[1] for item in second.compatibility_servers])


if __name__ == "__main__":
    unittest.main()
