"""Issue #328 UAT: ``create_server`` replaces a discovery worker that exits.

Expected independently of the implementation: a server bootstrap that has
previously started LAN discovery must not treat an unset stop event as evidence
that discovery is still serving.  If discovery exits by itself, a later
``create_server`` call must create a new generation, even while the process
"workers started" latch remains set.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import app.drone_api as drone_api
from app.common import identity_provisioning as identity
from app.common.settings import Settings
from app.transfer import local_network, peer_workers
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


class _InertThread:
    """Suppress unrelated peer-worker loops without changing their lifecycle."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def start(self) -> None:
        pass


class _LiveDiscovery:
    def is_alive(self) -> bool:
        return True

    def join(self, timeout=None) -> None:
        pass

    def close_discovery_socket(self) -> None:
        pass


def _close_server(server) -> None:
    listeners = list(getattr(server, "all_tls_servers", (server,)))
    for listener in listeners:
        worker = getattr(listener, "thread", None)
        if worker is not None:
            listener.shutdown()
        listener.server_close()
        if worker is not None:
            worker.join(timeout=3)
    server_tls.unregister_live_tls_servers(listeners)


class CreateServerDiscoveryExitRestartUAT(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self.tmp = tempfile.TemporaryDirectory(prefix="issue-328-real-exit-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        names = _QUIET_FLAGS + ("_LOCAL_NETWORK_WORKERS_STARTED", "_DOWNLOAD_MANAGER", "_TORRENT_MANAGER")
        self.originals = {name: getattr(drone_api, name) for name in names}
        for name in _QUIET_FLAGS:
            setattr(drone_api, name, True)
        drone_api._LOCAL_NETWORK_WORKERS_STARTED = False
        drone_api._DOWNLOAD_MANAGER = object()
        drone_api._TORRENT_MANAGER = object()
        peer_workers.stop_local_network_workers(timeout=1)
        self.addCleanup(self._restore_runtime)

        env = {
            "USERDATA_ROOT": str(root / "userdata"),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "issue-328-real-exit-host",
            "HTTPS_PORT": "0",
            "HTTP_ONLY": "1",
            "DRONE_COMPAT_HTTPS_PORTS": "",
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
        peer_workers.stop_local_network_workers(timeout=1)
        for name, value in self.originals.items():
            setattr(drone_api, name, value)

    def test_second_boot_replaces_real_discovery_thread_that_exited_without_stop(self) -> None:
        """A naturally exited discovery thread cannot strand the process latch."""
        exited = threading.Event()
        discoveries = 0

        def start_discovery(*args, **kwargs):
            nonlocal discoveries
            discoveries += 1
            if discoveries != 1:
                return _LiveDiscovery()
            exited_worker = threading.Thread(target=exited.set, name="issue-328-exited-discovery")
            exited_worker.start()
            exited_worker.join(timeout=1)
            self.assertTrue(exited.is_set())
            self.assertFalse(exited_worker.is_alive())
            return exited_worker

        with mock.patch.object(drone_api._identity, "swarm_networking_allowed", return_value=True), \
                mock.patch.object(drone_api.DroneCertificateManager, "ensure_certificate", return_value={}), \
                mock.patch.object(drone_api, "_apply_server_tls"), \
                mock.patch.object(drone_api, "_start_multiplayer_worker", return_value=None), \
                mock.patch.object(peer_workers, "Thread", _InertThread), \
                mock.patch.object(local_network, "start_discovery_worker", side_effect=start_discovery) as start:
            first = drone_api.create_server(self.settings)
            self.addCleanup(_close_server, first)
            first_stop = start.call_args.kwargs["stop_event"]
            self.assertTrue(drone_api._LOCAL_NETWORK_WORKERS_STARTED)
            self.assertFalse(first_stop.is_set(), "discovery exited without a lifecycle stop")
            self.assertFalse(peer_workers.local_network_workers_running())

            second = drone_api.create_server(self.settings)
            self.addCleanup(_close_server, second)

        self.assertEqual(start.call_count, 2, "second startup must replace dead discovery")
        self.assertTrue(first_stop.is_set(), "retiring the dead generation prevents stale workers")
        self.assertTrue(peer_workers.local_network_workers_running())


if __name__ == "__main__":
    unittest.main()
