"""Issue #243: restart after hold-release, failed restart, and a signalled generation.

Invariants from the issue and the identity/lifecycle contracts, taken before
treating the delivery as truth:

- After ``stop_local_network_workers``, a later swarm-allowed ``create_server``
  must start a new generation even when the process latch is still set.
- A swarm-held boot after that stop must not start workers. A following
  unheld boot in the same process must start them; the held boot must not
  consume the only restart chance.
- Three stop/create_server cycles must each produce a new live generation.
- If a restart attempt raises, the next ``create_server`` must still try.
- A stop that sets the generation event but fails before clearing handles
  still leaves workers not live; the next ``create_server`` must start again.
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


class HoldReleaseAndRetryCycleTests(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self._tmp = tempfile.TemporaryDirectory(prefix="issue-243-cycles-")
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
                "DRONE_DEVICE_ID": "issue-243-cycles",
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

    def _retire(self, server) -> None:
        _close_server(server)
        if server in self.servers:
            self.servers.remove(server)

    def _close_live_tls_servers(self) -> None:
        live = getattr(server_tls, "_LIVE_TLS_SERVERS", None)
        if not live:
            return
        leftover = list(live)
        live.clear()
        for tls in leftover:
            _close_server(tls)

    def _assert_live_generation(self) -> threading.Event:
        threads = _discovery_threads()
        self.assertTrue(threads, "expected a live drone-local-discovery thread")
        self.assertTrue(all(thread.is_alive() for thread in threads))
        stop = peer_workers._LOCAL_NETWORK_STOP
        self.assertIsNotNone(stop)
        self.assertFalse(stop.is_set())
        self.assertIs(peer_workers._LOCAL_NETWORK_DISCOVERY, threads[0])
        return stop

    def test_unheld_create_server_after_held_boot_starts_workers(self) -> None:
        with mock.patch.object(
            drone_api,
            "_start_local_network_workers",
            wraps=drone_api._start_local_network_workers,
        ) as start:
            first = self._boot()
            self.assertEqual(start.call_count, 1)
            self._assert_live_generation()
            self._retire(first)
            peer_workers.stop_local_network_workers(timeout=8.0)
            self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)
            self.assertEqual(_discovery_threads(), [])

            with mock.patch.object(drone_api._identity, "swarm_networking_allowed", return_value=False):
                held = self._boot()
            self.assertEqual(start.call_count, 1)
            self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)
            self.assertEqual(_discovery_threads(), [])
            self._retire(held)

            released = self._boot()
            self.assertEqual(
                start.call_count,
                2,
                "unheld create_server after a held boot must start LAN workers",
            )
            self._assert_live_generation()
            self._retire(released)

    def test_three_stop_create_server_cycles_each_start_a_new_generation(self) -> None:
        generations: list[threading.Event] = []
        with mock.patch.object(
            drone_api,
            "_start_local_network_workers",
            wraps=drone_api._start_local_network_workers,
        ) as start:
            for cycle in range(3):
                server = self._boot()
                self.assertEqual(start.call_count, cycle + 1, f"cycle {cycle} must start workers")
                stop = self._assert_live_generation()
                for previous in generations:
                    self.assertIsNot(stop, previous)
                    self.assertTrue(previous.is_set())
                generations.append(stop)
                self._retire(server)
                peer_workers.stop_local_network_workers(timeout=8.0)
                self.assertTrue(stop.is_set())
                self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)
                self.assertEqual(_discovery_threads(), [])
            self.assertEqual(len(generations), 3)

    def test_failed_restart_does_not_skip_the_following_create_server(self) -> None:
        real_start = drone_api._start_local_network_workers

        def boom(_settings) -> None:
            raise RuntimeError("issue-243-restart-failed")

        with mock.patch.object(
            drone_api,
            "_start_local_network_workers",
            wraps=real_start,
        ) as start:
            first = self._boot()
            self.assertEqual(start.call_count, 1)
            self._retire(first)
            peer_workers.stop_local_network_workers(timeout=8.0)
            self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)

        with mock.patch.object(drone_api, "_start_local_network_workers", side_effect=boom):
            with self.assertRaises(RuntimeError):
                drone_api.create_server(self.settings)
        self._close_live_tls_servers()
        os.environ["DRONE_PEER_MTLS_PORT"] = str(_free_port())
        self.settings = Settings.from_env()

        with mock.patch.object(drone_api, "_start_local_network_workers", wraps=real_start) as start:
            recovered = self._boot()
            self.assertEqual(
                start.call_count,
                1,
                "create_server must retry start after a failed restart attempt",
            )
            self._assert_live_generation()
            self._retire(recovered)

    def test_signalled_generation_with_handles_still_allows_restart(self) -> None:
        with mock.patch.object(
            drone_api,
            "_start_local_network_workers",
            wraps=drone_api._start_local_network_workers,
        ) as start:
            first = self._boot()
            self.assertEqual(start.call_count, 1)
            live_stop = self._assert_live_generation()
            self._retire(first)

            # Stop that set the event, then failed while joining discovery, so
            # the module handles were not cleared. Workers are no longer live.
            discovery = peer_workers._LOCAL_NETWORK_DISCOVERY
            self.assertIsNotNone(discovery)
            real_join = discovery.join
            join_calls: list[object] = []

            def join_once(timeout=None):
                if not join_calls:
                    join_calls.append(timeout)
                    raise RuntimeError("issue-243-join-failed")
                return real_join(timeout)

            with mock.patch.object(discovery, "join", side_effect=join_once):
                with self.assertRaises(RuntimeError):
                    peer_workers.stop_local_network_workers(timeout=8.0)
            self.assertTrue(live_stop.is_set())
            self.assertIs(peer_workers._LOCAL_NETWORK_STOP, live_stop)

            second = self._boot()
            self.assertEqual(
                start.call_count,
                2,
                "create_server must start again when the prior generation is signalled",
            )
            new_stop = self._assert_live_generation()
            self.assertIsNot(new_stop, live_stop)
            self._retire(second)


if __name__ == "__main__":
    unittest.main()
