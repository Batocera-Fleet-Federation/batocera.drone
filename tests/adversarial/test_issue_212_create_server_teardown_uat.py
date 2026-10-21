"""Issue #212 UAT: create_server teardown must join discovery before rmtree.

The original combined-run failure: create_server starts ``drone-local-discovery``
against a real temp state DB, then the harness removes that tree. HTTP
shutdown is not the join. Expected behavior from the issue:

- After ``create_server``, a discovery thread is alive and can persist peers.
- ``server.shutdown`` / ``server_close`` / joining the serve thread leaves
  that discovery thread running.
- ``stop_local_network_workers`` then join leaves zero discovery threads.
- Removing the temp userdata after that join must not WAL-open the deleted
  path or raise an unhandled thread exception.
- Stopping twice, then removing the tree, stays safe (stop is idempotent).
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


# Starters unrelated to LAN discovery. Pre-mark so this suite exercises the
# create_server -> discovery -> teardown contract without inheriting torrents,
# SMTP, Tailscale enroll, share replay, or the game-process monitor.
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


class CreateServerTeardownUAT(unittest.TestCase):
    def setUp(self) -> None:
        # Pollers left behind by an earlier harness's create_server() re-resolve the env-selected
        # state DB on every tick; retire them so they cannot write into this test's database (#420).
        drone_api.retire_background_pollers()
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self._tmp = tempfile.TemporaryDirectory(prefix="issue-212-uat-")
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
                "DRONE_DEVICE_ID": "issue-212-host",
                "HTTPS_PORT": "0",
                "HTTP_ONLY": "1",
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
        for patcher in self._tailnet:
            patcher.start()
        self.server = None
        self.serve_thread = None

    def tearDown(self) -> None:
        try:
            if self.server is not None:
                try:
                    self.server.shutdown()
                except Exception:
                    pass
                try:
                    self.server.server_close()
                except OSError:
                    pass
            if self.serve_thread is not None:
                self.serve_thread.join(timeout=3)
        finally:
            peer_workers.stop_local_network_workers(timeout=3.0)
            for thread in _discovery_threads():
                thread.join(timeout=3.0)
            drone_api._LOCAL_NETWORK_WORKERS_STARTED = False
            for name, value in self._flag_originals.items():
                setattr(drone_api, name, value)
            for patcher in self._tailnet:
                patcher.stop()
            self._announce_patch.stop()
            self._join_patch.stop()
            self._interval_patch.stop()
            self._port_patch.stop()
            self._env.stop()
            self._tmp.cleanup()

    def _boot(self) -> None:
        try:
            self.server = drone_api.create_server(self.settings)
        except PermissionError as error:
            self.skipTest(f"Socket bind is not allowed here: {error}")
        self.serve_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.serve_thread.start()

    def _http_shutdown(self) -> None:
        assert self.server is not None
        self.server.shutdown()
        self.server.server_close()
        if self.serve_thread is not None:
            self.serve_thread.join(timeout=3)
        self.serve_thread = None
        self.server = None

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
        self.fail("create_server discovery worker did not persist the injected peer")

    def test_http_shutdown_leaves_discovery_running_against_the_temp_db(self) -> None:
        self._boot()
        self.assertTrue(_discovery_threads(), "create_server must start drone-local-discovery")
        self._wait_for_peer("issue-212-peer")
        self._http_shutdown()
        still = _discovery_threads()
        self.assertTrue(
            still,
            "HTTP shutdown/join is not stop_local_network_workers; discovery must still be alive",
        )
        self.assertTrue(all(thread.is_alive() for thread in still))
        self.assertTrue(self.db_path.is_file())

