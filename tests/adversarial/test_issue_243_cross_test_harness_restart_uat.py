"""Issue #243 UAT: the next harness create_server must start LAN workers.

Expected behavior from the issue, taken before treating the delivery as truth:

- Combined unittest runs share one process. A test that calls ``create_server``
  latches ``drone_api._LOCAL_NETWORK_WORKERS_STARTED``.
- Correct #212 teardown calls ``stop_local_network_workers`` and then removes
  the temp userdata. It does not know about that latch and does not clear it.
- The next test in the same process builds a new userdata tree and calls
  ``create_server`` again. That boot must start a live discovery generation
  against the *new* state DB, even though the process latch is still set.
- A leftover generation on the previous UDP port / deleted path is not a
  substitute: the new boot must listen on its own discovery port and persist
  peers into its own database.

This is the combined-run hole that #212 teardown order does not close.
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


class _ThreadExceptionGuard:
    def __init__(self) -> None:
        self.failures: list[BaseException] = []
        self._previous = None

    def __enter__(self) -> "_ThreadExceptionGuard":
        self._previous = threading.excepthook

        def hook(args: threading.ExceptHookArgs) -> None:
            if args.exc_value is not None:
                self.failures.append(args.exc_value)
            if self._previous is not None:
                self._previous(args)

        threading.excepthook = hook
        return self

    def __exit__(self, *exc: object) -> None:
        threading.excepthook = self._previous

    def disk_io_failures(self) -> list[BaseException]:
        hits = []
        for error in self.failures:
            text = f"{error.__class__.__name__}: {error}".lower()
            if isinstance(error, sqlite3.Error) or "disk i/o" in text:
                hits.append(error)
        return hits


class CrossTestHarnessRestartUAT(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self._flag_originals: dict[str, object] = {}
        for name in _QUIET_FLAGS + _MANAGER_FLAGS + ("_LOCAL_NETWORK_WORKERS_STARTED",):
            self._flag_originals[name] = getattr(drone_api, name)
        for name in _QUIET_FLAGS:
            setattr(drone_api, name, True)
        for name in _MANAGER_FLAGS:
            setattr(drone_api, name, object())
        drone_api._LOCAL_NETWORK_WORKERS_STARTED = False
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        self._mp_patch = mock.patch.object(drone_api, "_start_multiplayer_worker", return_value=None)
        self._mp_patch.start()
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
        for patcher in self._tailnet:
            patcher.start()
        self._join_patch = mock.patch.object(local_network, "_join_multicast_group")
        self._announce_patch = mock.patch.object(local_network, "announce", return_value=True)
        self._interval_patch = mock.patch.object(local_network, "DISCOVERY_INTERVAL_SECONDS", 3600)
        self._join_patch.start()
        self._announce_patch.start()
        self._interval_patch.start()
        self._port_patch = None
        self.servers: list = []
        self._tmps: list[tempfile.TemporaryDirectory] = []
        peer_workers.stop_local_network_workers(timeout=3.0)

    def tearDown(self) -> None:
        try:
            for server in self.servers:
                _close_server(server)
        finally:
            peer_workers.stop_local_network_workers(timeout=3.0)
            for thread in _discovery_threads():
                thread.join(timeout=3.0)
            if self._port_patch is not None:
                self._port_patch.stop()
            self._interval_patch.stop()
            self._announce_patch.stop()
            self._join_patch.stop()
            for patcher in self._tailnet:
                patcher.stop()
            self._mp_patch.stop()
            for name, value in self._flag_originals.items():
                setattr(drone_api, name, value)
            self._env.stop()
            for tmp in self._tmps:
                tmp.cleanup()

    def _new_userdata(self, label: str) -> tuple[Path, Path]:
        tmp = tempfile.TemporaryDirectory(prefix=f"issue-243-harness-{label}-")
        self._tmps.append(tmp)
        root = Path(tmp.name)
        userdata = root / "userdata"
        seed_mock_userdata(userdata)
        db_path = root / "state.sqlite3"
        os.environ.update(
            {
                "USERDATA_ROOT": str(userdata),
                "ROMS_ROOT": str(userdata / "roms"),
                "BIOS_ROOT": str(userdata / "bios"),
                "SAVES_ROOT": str(userdata / "saves"),
                "DRONE_STATE_DATABASE_FILE": str(db_path),
                "DRONE_DEVICE_ID": f"issue-243-harness-{label}",
                "HTTPS_PORT": "0",
                "HTTP_ONLY": "1",
                "DRONE_HTTP_REDIRECT_PORT": "0",
                "DRONE_COMPAT_HTTPS_PORTS": "",
                "DRONE_PEER_MTLS_PORT": str(_free_port()),
                "DRONE_CAST_ENABLED": "0",
                "ROM_METADATA_POLL_SECONDS": "0",
                "DRONE_AUTO_UPDATE_POLL_SECONDS": "0",
                "LOG_DIR": str(root / "logs"),
                "TLS_SELF_SIGNED_DIR": str(userdata / "system" / "certs"),
                "DRONE_LOCAL_HEALTH_INTERVAL_SECONDS": "10",
                "DRONE_APP_USERNAME": "admin",
                "DRONE_APP_PASSWORD": "changeme",
            }
        )
        return userdata, db_path

    def _listen_on(self, port: int) -> None:
        if self._port_patch is not None:
            self._port_patch.stop()
        self._port_patch = mock.patch.object(local_network, "DISCOVERY_PORT", port)
        self._port_patch.start()

    def _boot(self, settings: Settings):
        try:
            server = drone_api.create_server(settings)
        except PermissionError as error:
            self.skipTest(f"Socket bind is not allowed here: {error}")
        self.servers.append(server)
        return server

    def _send(self, payload: dict, port: int) -> None:
        packet = json.dumps(payload).encode("utf-8")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(packet, ("127.0.0.1", port))

    def _peer_payload(self, drone_id: str) -> dict:
        return {
            "service": local_network.DISCOVERY_SERVICE,
            "kind": "announce",
            "drone_id": drone_id,
            "name": drone_id,
            "scheme": "http",
            "api_port": 8080,
        }

    def _wait_for_peer(self, db_path: Path, drone_id: str, port: int) -> None:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            self._send(self._peer_payload(drone_id), port)
            try:
                peers = state_store.load_payload(db_path, "local_discovered_peers", {})
            except sqlite3.OperationalError:
                peers = {}
            if isinstance(peers, dict) and drone_id in peers:
                return
            time.sleep(0.1)
        self.fail(f"discovery worker did not persist {drone_id} into {db_path}")

    def test_next_harness_create_server_starts_workers_on_fresh_userdata(self) -> None:
        """Two sequential tests in one process: stop+rmtree, then a new create_server."""
        first_port = _free_udp_port()
        second_port = _free_udp_port()
        self.assertNotEqual(first_port, second_port)

        first_userdata, first_db = self._new_userdata("a")
        first_root = first_userdata.parent
        self._listen_on(first_port)
        first_settings = Settings.from_env()

        with mock.patch.object(
            drone_api,
            "_start_local_network_workers",
            wraps=drone_api._start_local_network_workers,
        ) as start:
            first = self._boot(first_settings)
            self.assertEqual(start.call_count, 1)
            self.assertTrue(_discovery_threads(), "first harness create_server must start discovery")
            self._wait_for_peer(first_db, "issue-243-peer-a", first_port)

            _close_server(first)
            self.servers.remove(first)
            peer_workers.stop_local_network_workers(timeout=8.0)
            self.assertEqual(_discovery_threads(), [])
            self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)
            self.assertIsNone(peer_workers._LOCAL_NETWORK_DISCOVERY)
            # Harness teardown does not touch the process latch. Do not clear it.
            latch_after_first_teardown = drone_api._LOCAL_NETWORK_WORKERS_STARTED

            first_tmp = self._tmps.pop(0)
            first_tmp.cleanup()
            self.assertFalse(first_root.exists())

            second_userdata, second_db = self._new_userdata("b")
            self._listen_on(second_port)
            second_settings = Settings.from_env()
            self.assertEqual(
                drone_api._LOCAL_NETWORK_WORKERS_STARTED,
                latch_after_first_teardown,
                "the second harness must not need to reset _LOCAL_NETWORK_WORKERS_STARTED",
            )

            guard = _ThreadExceptionGuard()
            with guard:
                second = self._boot(second_settings)
                self.assertEqual(
                    start.call_count,
                    2,
                    "create_server after a prior test's stop must start LAN workers again",
                )
                self.assertTrue(
                    _discovery_threads(),
                    "second harness create_server must start drone-local-discovery",
                )
                self.assertIsNotNone(peer_workers._LOCAL_NETWORK_STOP)
                self.assertFalse(peer_workers._LOCAL_NETWORK_STOP.is_set())
                self._wait_for_peer(second_db, "issue-243-peer-b", second_port)

                # Packets aimed at the retired first port must not be what populated B.
                self._send(self._peer_payload("issue-243-stale-a"), first_port)
                time.sleep(0.4)
                peers = state_store.load_payload(second_db, "local_discovered_peers", {})
                self.assertIn("issue-243-peer-b", peers)
                self.assertNotIn("issue-243-stale-a", peers)
                self.assertTrue(second_db.is_file())
                self.assertNotEqual(first_db.resolve(), second_db.resolve())
                _close_server(second)
                self.servers.remove(second)

            self.assertEqual(guard.disk_io_failures(), [])


if __name__ == "__main__":
    unittest.main()
