"""Issue #328 UAT: create_server replaces a discovery generation that crashed.

The lifecycle contract is process-facing: ``create_server`` may retain its
once-per-process start latch, but that latch cannot suppress recovery after
the live LAN discovery worker exits without receiving a stop request.  The
replacement must receive a new generation stop event; recovery also retires
the stale generation's event and discovery handle.
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
from app.transfer import local_network, peer_workers
from app.web import server_tls


_QUIET_FLAGS = (
    "_ROM_METADATA_POLLER_STARTED", "_ROM_METADATA_WATCHER_STARTED",
    "_GAME_PROCESS_MONITOR_STARTED", "_AUTOMATION_POLLER_STARTED",
    "_NETWORK_SHARE_BOOT_REPLAY_ATTEMPTED", "_NETWORK_SHARE_WATCHDOG_STARTED",
    "_NFS_EXPORT_BOOT_REPLAY_ATTEMPTED", "_SMTP_BOOTSTRAP_ATTEMPTED",
    "_SMTP_SHARING_POLLER_STARTED", "_AUDIT_EMAIL_POLLER_STARTED",
    "_TAILNET_BOOTSTRAP_ATTEMPTED", "_TAILNET_SHARING_POLLER_STARTED",
    "_STREAMDECK_SUPERVISOR_STARTED", "_VPN_AUTO_CONNECT_ATTEMPTED",
    "_VPN_SELF_HEAL_POLLER_STARTED", "_VPN_SHARING_POLLER_STARTED",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _close_server(server) -> None:
    listeners = list(getattr(server, "all_tls_servers", (server,)))
    for listener in listeners:
        thread = getattr(listener, "thread", None)
        if thread is not None:
            listener.shutdown()
        listener.server_close()
        if thread is not None:
            thread.join(timeout=3)
    server_tls.unregister_live_tls_servers(listeners)


class _Discovery:
    def __init__(self, alive: bool = True) -> None:
        self.alive = alive
        self.close_calls = 0
        self.join_timeouts: list[float] = []

    def is_alive(self) -> bool:
        return self.alive

    def close_discovery_socket(self) -> None:
        self.close_calls += 1

    def join(self, timeout: float) -> None:
        self.join_timeouts.append(timeout)


class CreateServerDiscoveryCrashUAT(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self.tmp = tempfile.TemporaryDirectory(prefix="issue-328-uat-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        originals = _QUIET_FLAGS + ("_LOCAL_NETWORK_WORKERS_STARTED", "_DOWNLOAD_MANAGER", "_TORRENT_MANAGER")
        self.originals = {name: getattr(drone_api, name) for name in originals}
        for name in _QUIET_FLAGS:
            setattr(drone_api, name, True)
        drone_api._LOCAL_NETWORK_WORKERS_STARTED = False
        drone_api._DOWNLOAD_MANAGER = object()
        drone_api._TORRENT_MANAGER = object()
        peer_workers.stop_local_network_workers(timeout=1)
        self.addCleanup(self._restore_runtime)

        env = {
            "USERDATA_ROOT": str(self.root / "userdata"),
            "ROMS_ROOT": str(self.root / "roms"),
            "BIOS_ROOT": str(self.root / "bios"),
            "SAVES_ROOT": str(self.root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(self.root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "issue-328-host",
            "HTTPS_PORT": "0", "HTTP_ONLY": "1", "DRONE_COMPAT_HTTPS_PORTS": "",
            "DRONE_PEER_MTLS_PORT": str(_free_port()), "DRONE_HTTP_REDIRECT_PORT": "0",
            "DRONE_CAST_ENABLED": "0", "ROM_METADATA_POLL_SECONDS": "0",
            "ROM_METADATA_WATCH_ENABLED": "0", "DRONE_APP_PASSWORD": "test-password",
            "TLS_SELF_SIGNED_DIR": str(self.root / "certs"),
        }
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = Settings.from_env()

    def _restore_runtime(self) -> None:
        peer_workers.stop_local_network_workers(timeout=1)
        for name, value in self.originals.items():
            setattr(drone_api, name, value)

    def test_second_create_server_replaces_discovery_that_died_without_stop(self) -> None:
        crashed = _Discovery()
        replacement = _Discovery()
        certificate = mock.Mock()
        certificate.metadata.return_value = {"fingerprint": ""}
        inert_thread = mock.Mock()

        with mock.patch.object(drone_api._identity, "swarm_networking_allowed", return_value=True), \
                mock.patch.object(drone_api.DroneCertificateManager, "ensure_certificate", return_value={}), \
                mock.patch.object(drone_api, "_apply_server_tls"), \
                mock.patch.object(drone_api, "_start_multiplayer_worker", return_value=None), \
                mock.patch.object(peer_workers, "Thread", return_value=inert_thread), \
                mock.patch.object(peer_workers, "DroneCertificateManager", return_value=certificate), \
                mock.patch.object(local_network, "start_discovery_worker", side_effect=[crashed, replacement]) as start:
            first_server = drone_api.create_server(self.settings)
            self.addCleanup(_close_server, first_server)
            self.assertEqual(start.call_count, 1)
            first_stop = start.call_args.kwargs["stop_event"]
            self.assertFalse(first_stop.is_set())
            self.assertTrue(peer_workers.local_network_workers_running())

            # No stop occurred: only the discovery worker has died.  Closing the
            # HTTP listener must not clear that generation or its process latch.
            crashed.alive = False
            _close_server(first_server)
            self.assertTrue(drone_api._LOCAL_NETWORK_WORKERS_STARTED)
            self.assertFalse(first_stop.is_set())
            self.assertFalse(peer_workers.local_network_workers_running())

            second_server = drone_api.create_server(self.settings)
            self.addCleanup(_close_server, second_server)
            self.assertEqual(start.call_count, 2)
            self.assertTrue(first_stop.is_set())
            self.assertEqual(crashed.close_calls, 1)
            self.assertEqual(crashed.join_timeouts, [2.0])
            second_stop = start.call_args.kwargs["stop_event"]
            self.assertIsNot(second_stop, first_stop)
            self.assertFalse(second_stop.is_set())
            self.assertIs(peer_workers._LOCAL_NETWORK_DISCOVERY, replacement)
            self.assertTrue(peer_workers.local_network_workers_running())


if __name__ == "__main__":
    unittest.main()
