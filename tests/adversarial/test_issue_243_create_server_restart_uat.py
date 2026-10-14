"""Issue #243 UAT: create_server must restart LAN workers after a direct stop.

Expected behavior from the issue, taken before treating the delivery as truth:

- ``create_server`` starts local-network workers when swarm networking is allowed.
- ``stop_local_network_workers`` retires that live generation (signals, joins
  discovery, clears the module handles).
- A later ``create_server`` in the same process must start a new generation.
  Harness teardown calls stop and does not clear
  ``drone_api._LOCAL_NETWORK_WORKERS_STARTED``; that once-per-process latch
  from #191/#210 is why the second boot previously skipped start.
- HTTP listener shutdown is not a worker stop (#212). This issue is the start
  gate after an explicit stop, not teardown ordering.
- While a generation is still live, a second ``create_server`` must not start a
  replacement generation. That is the latch's original job.

This suite deliberately leaves the latch set between the two boots.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import app.drone_api as drone_api
from app.common import identity_provisioning as identity
from app.common.settings import Settings
from app.mock_data import seed_mock_userdata
from app.storage import state_store
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
        # The main listener is only bound here; peer-mTLS/compat listeners are.
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


class CreateServerRestartAfterStopUAT(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self._tmp = tempfile.TemporaryDirectory(prefix="issue-243-uat-")
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
                "DRONE_DEVICE_ID": "issue-243-host",
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

    def _send(self, payload: dict) -> None:
        packet = json.dumps(payload).encode("utf-8")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(packet, ("127.0.0.1", self.port))

    def _peer_payload(self, drone_id: str) -> dict:
        return {
            "service": local_network.DISCOVERY_SERVICE,
            "kind": "announce",
            "drone_id": drone_id,
            "name": drone_id,
            "scheme": "http",
            "api_port": 8080,
        }

    def _wait_for_peer(self, drone_id: str) -> None:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            self._send(self._peer_payload(drone_id))
            try:
                peers = state_store.load_payload(self.db_path, "local_discovered_peers", {})
            except sqlite3.OperationalError:
                peers = {}
            if isinstance(peers, dict) and drone_id in peers:
                return
            time.sleep(0.1)
        self.fail(f"discovery worker did not persist {drone_id}")

    def test_create_server_stop_create_server_starts_a_new_generation(self) -> None:
        """Reproduction: same process, latch still set, workers were stopped."""
        with mock.patch.object(
            drone_api,
            "_start_local_network_workers",
            wraps=drone_api._start_local_network_workers,
        ) as start:
            first = self._boot()
            self.assertEqual(start.call_count, 1)
            self.assertTrue(_discovery_threads(), "first create_server must start discovery")
            self.assertIsNotNone(peer_workers._LOCAL_NETWORK_STOP)
            self.assertFalse(peer_workers._LOCAL_NETWORK_STOP.is_set())
            self._wait_for_peer("issue-243-first")

            _close_server(first)
            self.servers.remove(first)
            still = _discovery_threads()
            self.assertTrue(
                still,
                "HTTP close is not stop_local_network_workers; discovery must still be alive",
            )

            peer_workers.stop_local_network_workers(timeout=8.0)
            self.assertEqual(_discovery_threads(), [])
            self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)
            self.assertIsNone(peer_workers._LOCAL_NETWORK_DISCOVERY)
            # Leave drone_api._LOCAL_NETWORK_WORKERS_STARTED as create_server left it.
            # The original bug is that harness teardown stops workers and does not
            # clear this latch; the test must not paper over that by resetting it.

            second = self._boot()
            self.assertEqual(
                start.call_count,
                2,
                "create_server after stop must call _start_local_network_workers again",
            )
            self.assertTrue(
                _discovery_threads(),
                "second create_server must start a live drone-local-discovery thread",
            )
            self.assertIsNotNone(peer_workers._LOCAL_NETWORK_STOP)
            self.assertFalse(peer_workers._LOCAL_NETWORK_STOP.is_set())
            self.assertIs(peer_workers._LOCAL_NETWORK_DISCOVERY, _discovery_threads()[0])
            self._wait_for_peer("issue-243-second")
            _close_server(second)
            self.servers.remove(second)

    def test_second_create_server_while_live_does_not_restart_generation(self) -> None:
        with mock.patch.object(
            drone_api,
            "_start_local_network_workers",
            wraps=drone_api._start_local_network_workers,
        ) as start:
            self._boot()
            first_threads = list(_discovery_threads())
            self.assertEqual(len(first_threads), 1)
            self.assertEqual(start.call_count, 1)
            live_stop = peer_workers._LOCAL_NETWORK_STOP
            self.assertIsNotNone(live_stop)
            self.assertFalse(live_stop.is_set())

            self._boot()
            self.assertEqual(
                start.call_count,
                1,
                "create_server must not replace a still-live generation",
            )
            self.assertEqual(_discovery_threads(), first_threads)
            self.assertIs(peer_workers._LOCAL_NETWORK_STOP, live_stop)
            self.assertFalse(live_stop.is_set())
            self.assertTrue(first_threads[0].is_alive())
