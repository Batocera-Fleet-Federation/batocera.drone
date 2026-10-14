"""Issue #243 boundaries: swarm gate, idle stop, failed start, malformed stop.

Invariants from the issue and related lifecycle contracts:

- Swarm-held ``create_server`` must not start LAN workers, including after a
  stop that left the process latch set.
- ``stop_local_network_workers`` is a no-op when idle (#191) and is
  idempotent; a later swarm-allowed ``create_server`` must still start.
- A ``create_server`` whose worker start raises must not consume the chance
  to start on the next boot.
- A malformed stop timeout must not leave ``create_server`` unable to start
  a generation after a subsequent successful stop.
- Direct stop without a prior ``create_server`` must not block the first boot.
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
from app.common import identity_provisioning as identity
from app.common.settings import Settings
from app.mock_data import seed_mock_userdata
from app.transfer import local_network, peer_workers


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
_MANAGER_FLAGS = ("_DOWNLOAD_MANAGER", "_TORRENT_MANAGER")


def _free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return port


def _free_udp_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return port


def _discovery_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "drone-local-discovery"]


def _close_server(server) -> None:
    extras = []
    for attr in ("http_redirect_server", "cast_http_server"):
        extra = getattr(server, attr, None)
        if extra is not None:
            extras.append(extra)
    seen: list = []
    for tls in list(getattr(server, "all_tls_servers", [server])) + extras:
        if tls is None or tls in seen:
            continue
        seen.append(tls)
        thread = getattr(tls, "thread", None)
        # shutdown() deadlocks unless serve_forever is running in another thread.
        if thread is not None:
            try:
                tls.shutdown()
            except Exception:
                pass
        try:
            tls.server_close()
        except OSError:
            pass
        if thread is not None:
            thread.join(timeout=3)


class LatchBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self._tmp = tempfile.TemporaryDirectory(prefix="issue-243-bound-")
        self.root = Path(self._tmp.name)
        self.userdata = self.root / "userdata"
        seed_mock_userdata(self.userdata)
        self.db_path = self.root / "state.sqlite3"
        self.port = _free_udp_port()
        self._flag_originals: dict[str, object] = {}
        for name in _QUIET_FLAGS + _MANAGER_FLAGS + ("_LOCAL_NETWORK_WORKERS_STARTED",):
            self._flag_originals[name] = getattr(drone_api, name)
        for name in _QUIET_FLAGS:
            setattr(drone_api, name, True)
        for name in _MANAGER_FLAGS:
            setattr(drone_api, name, object())
        drone_api._LOCAL_NETWORK_WORKERS_STARTED = False
        self._env = mock.patch.dict(
            os.environ,
            {
                "USERDATA_ROOT": str(self.userdata),
                "ROMS_ROOT": str(self.userdata / "roms"),
                "BIOS_ROOT": str(self.userdata / "bios"),
                "SAVES_ROOT": str(self.userdata / "saves"),
                "DRONE_STATE_DATABASE_FILE": str(self.db_path),
                "DRONE_DEVICE_ID": "issue-243-bound",
                "HTTPS_PORT": "0",
                "HTTP_ONLY": "1",
                "DRONE_HTTP_REDIRECT_PORT": "0",
                "DRONE_COMPAT_HTTPS_PORTS": "",
                "DRONE_PEER_MTLS_PORT": str(_free_port()),
                "DRONE_CAST_ENABLED": "0",
                "ROM_METADATA_POLL_SECONDS": "0",
                "DRONE_AUTO_UPDATE_POLL_SECONDS": "0",
                "LOG_DIR": str(self.root / "logs"),
                "TLS_SELF_SIGNED_DIR": str(self.userdata / "system" / "certs"),
                "DRONE_LOCAL_HEALTH_INTERVAL_SECONDS": "10",
                "DRONE_APP_USERNAME": "admin",
                "DRONE_APP_PASSWORD": "changeme",
            },
            clear=False,
        )
        self._env.start()
        self.settings = Settings.from_env()
        self._port_patch = mock.patch.object(local_network, "DISCOVERY_PORT", self.port)
        self._interval_patch = mock.patch.object(local_network, "DISCOVERY_INTERVAL_SECONDS", 3600)
        self._join_patch = mock.patch.object(local_network, "_join_multicast_group")
        self._announce_patch = mock.patch.object(local_network, "announce", return_value=True)
        self._mp_patch = mock.patch.object(drone_api, "_start_multiplayer_worker", return_value=None)
        self._tailnet = [
            mock.patch.object(peer_workers, "ensure_tailnet_networking"),
            mock.patch.object(peer_workers, "bootstrap_tailnet_from_swarm"),
            mock.patch.object(
                peer_workers,
                "tailnet_status",
                return_value={"running": True, "installed": True, "enrolled": True},
            ),
            mock.patch.object(peer_workers, "_repair_tailnet_data_path"),
        ]
        self._port_patch.start()
        self._interval_patch.start()
        self._join_patch.start()
        self._announce_patch.start()
        self._mp_patch.start()
        for patcher in self._tailnet:
            patcher.start()
        self.servers: list = []
        peer_workers.stop_local_network_workers(timeout=3.0)

    def tearDown(self) -> None:
        try:
            for server in self.servers:
                _close_server(server)
        finally:
            peer_workers.stop_local_network_workers(timeout=3.0)
            for thread in _discovery_threads():
                thread.join(timeout=3.0)
            for name, value in self._flag_originals.items():
                setattr(drone_api, name, value)
            for patcher in self._tailnet:
                patcher.stop()
            self._mp_patch.stop()
            self._announce_patch.stop()
            self._join_patch.stop()
            self._interval_patch.stop()
            self._port_patch.stop()
            self._env.stop()
            self._tmp.cleanup()

    def _boot(self):
        try:
            server = drone_api.create_server(self.settings)
        except PermissionError as error:
            self.skipTest(f"Socket bind is not allowed here: {error}")
        self.servers.append(server)
        return server

    def test_idle_stop_then_create_server_still_starts(self) -> None:
        self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)
        peer_workers.stop_local_network_workers()
        peer_workers.stop_local_network_workers(timeout=0.1)
        with mock.patch.object(
            drone_api,
            "_start_local_network_workers",
            wraps=drone_api._start_local_network_workers,
        ) as start:
            self._boot()
            self.assertEqual(start.call_count, 1)
        self.assertIsNotNone(peer_workers._LOCAL_NETWORK_STOP)
        self.assertFalse(peer_workers._LOCAL_NETWORK_STOP.is_set())
        self.assertTrue(_discovery_threads())

    def test_held_identity_after_stop_does_not_restart_workers(self) -> None:
        with mock.patch.object(
            drone_api,
            "_start_local_network_workers",
            wraps=drone_api._start_local_network_workers,
        ) as start:
            first = self._boot()
            self.assertEqual(start.call_count, 1)
            _close_server(first)
            self.servers.remove(first)
            peer_workers.stop_local_network_workers(timeout=8.0)
            self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)

            with mock.patch.object(drone_api._identity, "swarm_networking_allowed", return_value=False):
                held = self._boot()
            self.assertEqual(start.call_count, 1)
            self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)
            self.assertEqual(_discovery_threads(), [])
            _close_server(held)
            self.servers.remove(held)

    def test_start_failure_does_not_block_the_next_create_server(self) -> None:
        real = drone_api._start_local_network_workers

        def boom(_settings) -> None:
            raise RuntimeError("issue-243-start-failed")

        with mock.patch.object(drone_api, "_start_local_network_workers", side_effect=boom):
            with self.assertRaises(RuntimeError):
                drone_api.create_server(self.settings)

        peer_workers.stop_local_network_workers(timeout=3.0)
        with mock.patch.object(drone_api, "_start_local_network_workers", wraps=real) as start:
            self._boot()
            self.assertEqual(start.call_count, 1)
        self.assertIsNotNone(peer_workers._LOCAL_NETWORK_STOP)
        self.assertFalse(peer_workers._LOCAL_NETWORK_STOP.is_set())

    def test_malformed_stop_timeout_then_real_stop_still_allows_restart(self) -> None:
        with mock.patch.object(
            drone_api,
            "_start_local_network_workers",
            wraps=drone_api._start_local_network_workers,
        ) as start:
            first = self._boot()
            self.assertEqual(start.call_count, 1)
            with self.assertRaises(TypeError):
                peer_workers.stop_local_network_workers(timeout="not-a-timeout")
            peer_workers.stop_local_network_workers(timeout=8.0)
            self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)
            _close_server(first)
            self.servers.remove(first)
            self._boot()
            self.assertEqual(start.call_count, 2)
            self.assertIsNotNone(peer_workers._LOCAL_NETWORK_STOP)
            self.assertFalse(peer_workers._LOCAL_NETWORK_STOP.is_set())
            self.assertTrue(_discovery_threads())

    def test_direct_start_then_stop_clears_live_generation_handles(self) -> None:
        self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)
        with mock.patch.object(peer_workers, "Thread"), \
                mock.patch.object(local_network, "start_discovery_worker", return_value=mock.Mock()):
            peer_workers._start_local_network_workers(mock.Mock())
            self.assertIsNotNone(peer_workers._LOCAL_NETWORK_STOP)
            self.assertFalse(peer_workers._LOCAL_NETWORK_STOP.is_set())
            peer_workers.stop_local_network_workers(timeout=1.0)
        self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)
        self.assertIsNone(peer_workers._LOCAL_NETWORK_DISCOVERY)
