"""Issue #249 UAT: a leaky create_server harness must not poison the next test.

The #213 combined-discovery failure (517 tests) showed 14 ``open_database``
calls after stop and two recreated temporary directories. Issue #191 suites
passed in isolation. Expected behavior from that report:

- ``create_server`` starts process-global ``drone-local-discovery`` against
  that harness's real state DB. HTTP shutdown leaves it running (#212).
- ``open_database`` does ``path.parent.mkdir(parents=True)`` before WAL.
  A leftover worker therefore recreates a temp tree that unittest already
  deleted, then writes into it.
- Combined runs share one process. After harness A removes its tree, harness
  B's stop-and-spy must not see those late opens, and A's directory must stay
  gone.
- Harness A must call ``stop_local_network_workers`` and join before rmtree.
  HTTP-only teardown is the interference pattern, not a substitute.
- Stop is idempotent and a no-op when nothing was started, so a later harness
  can always call it.
- Malformed UDP on the leftover socket must not keep the worker alive through
  a later stop.
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


def _free_udp_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return port


def _discovery_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "drone-local-discovery"]


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


class CombinedRunIsolationUAT(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self._flag_originals: dict[str, object] = {}
        for name in _QUIET_FLAGS + _MANAGER_FLAGS + ("_LOCAL_NETWORK_WORKERS_STARTED",):
            self._flag_originals[name] = getattr(drone_api, name)
        self._tailnet = [
            mock.patch.object(peer_workers, "ensure_tailnet_networking"),
            mock.patch.object(peer_workers, "bootstrap_tailnet_from_swarm"),
            mock.patch.object(
                peer_workers,
                "tailnet_status",
                return_value={"running": True, "installed": True, "enrolled": True},
            ),
            mock.patch.object(peer_workers, "_repair_tailnet_data_path"),
            mock.patch("app.multiplayer.worker.start_multiplayer_worker", return_value=None),
        ]
        for patcher in self._tailnet:
            patcher.start()
        self._join_patch = mock.patch.object(local_network, "_join_multicast_group")
        self._announce_patch = mock.patch.object(local_network, "announce", return_value=True)
        self._join_patch.start()
        self._announce_patch.start()
        self._servers: list = []
        self._serve_threads: list[threading.Thread] = []
        self._env_patchers: list = []
        self._port_patchers: list = []

    def tearDown(self) -> None:
        try:
            for server in self._servers:
                try:
                    server.shutdown()
                except Exception:
                    pass
                try:
                    server.server_close()
                except OSError:
                    pass
            for thread in self._serve_threads:
                thread.join(timeout=3)
        finally:
            peer_workers.stop_local_network_workers(timeout=3.0)
            for thread in _discovery_threads():
                thread.join(timeout=3.0)
            for name, value in self._flag_originals.items():
                setattr(drone_api, name, value)
            for patcher in self._port_patchers:
                patcher.stop()
            self._announce_patch.stop()
            self._join_patch.stop()
            for patcher in self._tailnet:
                patcher.stop()
            for patcher in reversed(self._env_patchers):
                patcher.stop()

    def _quiet_starters(self) -> None:
        for name in _QUIET_FLAGS:
            setattr(drone_api, name, True)
        for name in _MANAGER_FLAGS:
            setattr(drone_api, name, object())
        drone_api._LOCAL_NETWORK_WORKERS_STARTED = False

    def _make_tree(self, prefix: str, device_id: str) -> tuple[tempfile.TemporaryDirectory, Path, Path, int]:
        tmp = tempfile.TemporaryDirectory(prefix=prefix)
        root = Path(tmp.name)
        userdata = root / "userdata"
        db_path = root / "state.sqlite3"
        port = _free_udp_port()
        # seed_mock_userdata writes through database_path(), which prefers
        # DRONE_STATE_DATABASE_FILE. Bind env first so a prior harness's
        # deleted path is not mkdir'd back into existence.
        self._activate_env(userdata, db_path, device_id)
        seed_mock_userdata(userdata)
        return tmp, root, db_path, port

    def _activate_env(self, userdata: Path, db_path: Path, device_id: str) -> None:
        patcher = mock.patch.dict(
            os.environ,
            {
                "USERDATA_ROOT": str(userdata),
                "ROMS_ROOT": str(userdata / "roms"),
                "BIOS_ROOT": str(userdata / "bios"),
                "SAVES_ROOT": str(userdata / "saves"),
                "DRONE_STATE_DATABASE_FILE": str(db_path),
                "DRONE_DEVICE_ID": device_id,
                "HTTPS_PORT": "0",
                "HTTP_ONLY": "1",
                "DRONE_CAST_ENABLED": "0",
                "ROM_METADATA_POLL_SECONDS": "0",
                "DRONE_AUTO_UPDATE_POLL_SECONDS": "0",
                "LOG_DIR": str(userdata.parent / "logs"),
                "TLS_SELF_SIGNED_DIR": str(userdata / "system" / "certs"),
                "DRONE_LOCAL_HEALTH_INTERVAL_SECONDS": "3600",
                "DRONE_APP_USERNAME": "admin",
                "DRONE_APP_PASSWORD": "changeme",
                "HTTP_REDIRECT_PORT": "0",
                "DRONE_COMPAT_HTTPS_PORTS": "",
                "DRONE_PEER_MTLS_PORT": "0",
            },
            clear=False,
        )
        patcher.start()
        self._env_patchers.append(patcher)

    def _bind_discovery_port(self, port: int) -> None:
        port_patch = mock.patch.object(local_network, "DISCOVERY_PORT", port)
        interval_patch = mock.patch.object(local_network, "DISCOVERY_INTERVAL_SECONDS", 3600)
        port_patch.start()
        interval_patch.start()
        self._port_patchers.extend([port_patch, interval_patch])

    def _boot(self, settings: Settings, port: int):
        self._bind_discovery_port(port)
        try:
            server = drone_api.create_server(settings)
        except PermissionError as error:
            self.skipTest(f"Socket bind is not allowed here: {error}")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._servers.append(server)
        self._serve_threads.append(thread)
        return server, thread

    def _http_shutdown(self, server, thread: threading.Thread) -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        if server in self._servers:
            self._servers.remove(server)
        if thread in self._serve_threads:
            self._serve_threads.remove(thread)

    def _send(self, port: int, payload: dict | bytes) -> None:
        packet = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
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

    def _wait_for_peer(self, db_path: Path, port: int, drone_id: str) -> None:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            self._send(port, self._peer_payload(drone_id))
            try:
                peers = state_store.load_payload(db_path, "local_discovered_peers", {})
            except sqlite3.OperationalError:
                peers = {}
            if isinstance(peers, dict) and drone_id in peers:
                return
            time.sleep(0.1)
        self.fail("discovery worker did not persist the injected peer")

    def test_http_only_teardown_recreates_deleted_temp_tree(self) -> None:
        """The mkdir-before-WAL path: leftover discovery rebuilds rmtree'd parents."""
        self._quiet_starters()
        tmp, root, db_path, port = self._make_tree("issue-249-leak-", "issue-249-leak")
        settings = Settings.from_env()
        server, thread = self._boot(settings, port)
        self._wait_for_peer(db_path, port, "issue-249-leak-peer")
        self._http_shutdown(server, thread)
        self.assertTrue(_discovery_threads(), "HTTP shutdown must leave discovery running")
        tmp.cleanup()
        self.assertFalse(root.exists())
        for _ in range(12):
            self._send(port, self._peer_payload("late-peer"))
            time.sleep(0.15)
            if root.exists() or db_path.exists():
                break
        self.assertTrue(
            root.exists() or db_path.exists(),
            "leftover open_database must recreate the deleted temp parent via mkdir",
        )
        peer_workers.stop_local_network_workers(timeout=8.0)
        if root.exists():
            tmp.cleanup()

    def test_stop_before_rmtree_does_not_recreate_the_temp_tree(self) -> None:
        self._quiet_starters()
        tmp, root, db_path, port = self._make_tree("issue-249-stop-", "issue-249-stop")
        settings = Settings.from_env()
        server, thread = self._boot(settings, port)
        self._wait_for_peer(db_path, port, "issue-249-stop-peer")
        self._http_shutdown(server, thread)
        peer_workers.stop_local_network_workers(timeout=8.0)
        self.assertEqual(_discovery_threads(), [])
        tmp.cleanup()
        for _ in range(8):
            self._send(port, self._peer_payload("after-stop"))
            time.sleep(0.1)
        self.assertFalse(root.exists())
        self.assertFalse(db_path.exists())

    def test_http_only_first_harness_pollutes_next_harness_open_spy(self) -> None:
        """#191 isolation passes; combined with a leaky prior harness it must not.

        The next suite starts its own discovery thread and joins that thread.
        Process-global workers left by a prior create_server keep calling
        ``open_database`` (and mkdir of the deleted parent) after that join.
        """
        self._quiet_starters()
        first, first_root, first_db, first_port = self._make_tree("issue-249-a-", "issue-249-a")
        first_settings = Settings.from_env()
        server, thread = self._boot(first_settings, first_port)
        self._wait_for_peer(first_db, first_port, "issue-249-a-peer")
        self._http_shutdown(server, thread)
        self.assertTrue(_discovery_threads())
        first.cleanup()

        second, second_root, second_db, _second_port = self._make_tree("issue-249-b-", "issue-249-b")
        second_settings = Settings.from_env()
        opens_after_stop: list[str] = []
        stop_joined = threading.Event()
        real_open = state_store.open_database

        def spy_open(path, *args, **kwargs):
            if stop_joined.is_set():
                opens_after_stop.append(str(path))
            return real_open(path, *args, **kwargs)

        with mock.patch.object(state_store, "open_database", side_effect=spy_open):
            local_stop = threading.Event()
            worker = local_network.start_discovery_worker(
                second_settings, lambda: "", stop_event=local_stop
            )
            local_stop.set()
            worker.join(timeout=8)
            self.assertFalse(worker.is_alive())
            stop_joined.set()
            second.cleanup()
            time.sleep(0.4)
            for packet in (
                b"",
                b"\x00 not-json",
                json.dumps({"service": local_network.DISCOVERY_SERVICE, "drone_id": None}).encode("utf-8"),
                json.dumps(self._peer_payload("pollute")).encode("utf-8"),
            ):
                self._send(first_port, packet)
                time.sleep(0.15)

        self.assertTrue(
            opens_after_stop or first_root.exists() or first_db.exists(),
            "a leaky prior create_server harness must still be visible to the next stop-and-spy",
        )
        peer_workers.stop_local_network_workers(timeout=8.0)
        if first_root.exists():
            first.cleanup()

    def test_stop_then_next_harness_sees_no_late_opens_or_recreated_dirs(self) -> None:
        guard = _ThreadExceptionGuard()
        self._quiet_starters()
        first, first_root, first_db, first_port = self._make_tree("issue-249-ok-a-", "issue-249-ok-a")
        first_settings = Settings.from_env()
        server, thread = self._boot(first_settings, first_port)
        self._wait_for_peer(first_db, first_port, "issue-249-ok-a-peer")
        self._http_shutdown(server, thread)
        peer_workers.stop_local_network_workers(timeout=8.0)
        for thread in _discovery_threads():
            thread.join(timeout=3.0)
        self.assertEqual(_discovery_threads(), [])
        first.cleanup()
        self.assertFalse(first_root.exists())

        second, second_root, second_db, second_port = self._make_tree("issue-249-ok-b-", "issue-249-ok-b")
        second_settings = Settings.from_env()
        opens_after_stop: list[str] = []
        stop_joined = threading.Event()
        real_open = state_store.open_database

        def spy_open(path, *args, **kwargs):
            if stop_joined.is_set():
                opens_after_stop.append(str(path))
            return real_open(path, *args, **kwargs)

        self._bind_discovery_port(second_port)
        with guard, mock.patch.object(state_store, "open_database", side_effect=spy_open):
            stop = threading.Event()
            worker = local_network.start_discovery_worker(
                second_settings, lambda: "", stop_event=stop
            )
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not worker.is_alive():
                time.sleep(0.02)
            self._wait_for_peer(second_db, second_port, "issue-249-ok-b-peer")
            stop.set()
            worker.join(timeout=8)
            self.assertFalse(worker.is_alive())
            stop_joined.set()
            second.cleanup()
            time.sleep(0.8)
            self._send(first_port, self._peer_payload("ghost-a"))
            self._send(second_port, self._peer_payload("ghost-b"))
            time.sleep(0.8)

        self.assertFalse(first_root.exists())
        self.assertFalse(second_root.exists())
        self.assertEqual(opens_after_stop, [])
        self.assertEqual(guard.disk_io_failures(), [])

    def test_stop_is_noop_and_idempotent_between_harnesses(self) -> None:
        peer_workers._LOCAL_NETWORK_STOP = None
        peer_workers._LOCAL_NETWORK_DISCOVERY = None
        peer_workers.stop_local_network_workers()
        peer_workers.stop_local_network_workers(timeout=0.1)
        self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)
        self.assertIsNone(peer_workers._LOCAL_NETWORK_DISCOVERY)
        self._quiet_starters()
        tmp, root, db_path, port = self._make_tree("issue-249-noop-", "issue-249-noop")
        settings = Settings.from_env()
        server, thread = self._boot(settings, port)
        self._http_shutdown(server, thread)
        peer_workers.stop_local_network_workers(timeout=8.0)
        peer_workers.stop_local_network_workers(timeout=0.1)
        tmp.cleanup()
        self.assertFalse(root.exists())

    def test_malformed_udp_on_a_leaked_socket_does_not_block_later_stop(self) -> None:
        self._quiet_starters()
        tmp, root, db_path, port = self._make_tree("issue-249-garbage-", "issue-249-garbage")
        settings = Settings.from_env()
        server, thread = self._boot(settings, port)
        self._wait_for_peer(db_path, port, "issue-249-garbage-peer")
        self._http_shutdown(server, thread)
        garbage = (
            b"",
            b"\x00\xff not-json",
            json.dumps({"service": "wrong-service", "drone_id": "x"}).encode("utf-8"),
            json.dumps({"service": local_network.DISCOVERY_SERVICE, "drone_id": ""}).encode("utf-8"),
            json.dumps({"service": local_network.DISCOVERY_SERVICE, "drone_id": None}).encode("utf-8"),
            b"[]",
            b"null",
            b'"peer"',
            b"42",
        )
        for packet in garbage:
            self._send(port, packet)
        time.sleep(0.3)
        self.assertTrue(_discovery_threads())
        peer_workers.stop_local_network_workers(timeout=8.0)
        self.assertEqual(_discovery_threads(), [])
        tmp.cleanup()
        self.assertFalse(root.exists())
