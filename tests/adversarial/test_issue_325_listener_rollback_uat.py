"""Issue #325 UAT: a failed boot must not retain its peer-mTLS listener.

Expected before reading the delivery: ``create_server`` either completes with a
usable listener set or propagates its startup failure with no listener from that
attempt still bound.  In particular, a LAN-worker failure occurs after the
dedicated peer listener is serving, so a retry in the same process must be able
to use the exact same peer port.  TLS live-server bookkeeping must likewise not
retain the abandoned listeners, or later pairing could mutate a closed server.
"""

from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.drone_api as drone_api
from app.common import identity_provisioning as identity
from app.common.settings import Settings
from app.transfer import peer_workers
from app.web import server_tls


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
    "_VPN_SELF_HEAL_POLLER_STARTED",
    "_VPN_SHARING_POLLER_STARTED",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _close_server(server) -> None:
    """Close a successful test boot without leaving its peer serving thread."""
    listeners = list(getattr(server, "all_tls_servers", (server,)))
    for listener in listeners:
        thread = getattr(listener, "thread", None)
        if thread is not None:
            listener.shutdown()
        listener.server_close()
        if thread is not None:
            thread.join(timeout=3)
    server_tls.unregister_live_tls_servers(listeners)


class CreateServerRollbackUAT(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self.tmp = tempfile.TemporaryDirectory(prefix="issue-325-uat-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.originals = {name: getattr(drone_api, name) for name in _QUIET_FLAGS + ("_LOCAL_NETWORK_WORKERS_STARTED", "_DOWNLOAD_MANAGER", "_TORRENT_MANAGER")}
        for name in _QUIET_FLAGS:
            setattr(drone_api, name, True)
        drone_api._LOCAL_NETWORK_WORKERS_STARTED = False
        drone_api._DOWNLOAD_MANAGER = object()
        drone_api._TORRENT_MANAGER = object()
        peer_workers.stop_local_network_workers(timeout=3)
        self.addCleanup(self._restore_runtime)

        env = {
            "USERDATA_ROOT": str(self.root / "userdata"),
            "ROMS_ROOT": str(self.root / "roms"),
            "BIOS_ROOT": str(self.root / "bios"),
            "SAVES_ROOT": str(self.root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(self.root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "issue-325-host",
            "HTTPS_PORT": "0",
            "HTTP_ONLY": "1",
            "DRONE_COMPAT_HTTPS_PORTS": "",
            "DRONE_PEER_MTLS_PORT": str(_free_port()),
            "DRONE_HTTP_REDIRECT_PORT": "0",
            "DRONE_CAST_ENABLED": "0",
            "ROM_METADATA_POLL_SECONDS": "0",
            "ROM_METADATA_WATCH_ENABLED": "0",
            "DRONE_APP_PASSWORD": "test-password",
            "TLS_SELF_SIGNED_DIR": str(self.root / "certs"),
        }
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = Settings.from_env()

    def _restore_runtime(self) -> None:
        peer_workers.stop_local_network_workers(timeout=3)
        for name, value in self.originals.items():
            setattr(drone_api, name, value)

    def test_base_exception_after_peer_listener_starts_is_rolled_back_before_retry(self) -> None:
        """A post-bind startup abort cannot poison its peer port or TLS registry."""
        registered: list[object] = []

        def record_registration(servers) -> None:
            registered.extend(servers)
            server_tls.register_live_tls_servers(servers)

        with mock.patch.object(drone_api._identity, "swarm_networking_allowed", return_value=True), \
                mock.patch.object(drone_api.DroneCertificateManager, "ensure_certificate", return_value={}), \
                mock.patch.object(drone_api, "_apply_server_tls"), \
                mock.patch.object(drone_api, "register_live_tls_servers", side_effect=record_registration), \
                mock.patch.object(drone_api, "_start_local_network_workers", side_effect=[KeyboardInterrupt("abort"), None]) as start_workers, \
                mock.patch.object(drone_api, "_start_multiplayer_worker", return_value=None):
            with self.assertRaisesRegex(KeyboardInterrupt, "abort"):
                drone_api.create_server(self.settings)

            first_attempt = list(registered)
            self.assertEqual(len(first_attempt), 2, "main and peer-mTLS listeners must have registered before worker startup")
            self.assertFalse(
                any(listener in server_tls._LIVE_TLS_SERVERS for listener in first_attempt),
                "rolled-back listeners must be forgotten by live TLS registration",
            )

            # This is the real regression boundary: creating the peer listener again
            # binds the same port, rather than merely proving a one-off socket probe.
            retry = drone_api.create_server(self.settings)
            self.addCleanup(_close_server, retry)
            self.assertEqual(start_workers.call_count, 2)
            peer = next(listener for listener in retry.all_tls_servers if getattr(listener, "is_peer_mtls_listener", False))
            self.assertEqual(peer.server_address[1], self.settings.peer_mtls_port)
            self.assertTrue(peer.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
