"""Issue #312 in-process shutdown boundaries for ``drone_api.main``.

Expected behavior, derived from the issue before reading the delivery:

* Once ``create_server`` has started a local-network generation, every
  in-process exit from ``main`` must retire it: normal serve-loop return,
  ``KeyboardInterrupt`` during serve, and a failure after the generation starts.
* Retiring means the generation's stop event is set first, then LAN discovery
  has its socket closed and its thread joined.
* Retiring with nothing running, or retiring twice, is a harmless no-op.
* A restarted process-local generation gets a fresh, unset stop event; the old
  one stays set.

Real generation bookkeeping and the real stop routine are used. Network I/O,
tailnet calls and the discovery thread are replaced with recording doubles.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from app import drone_api
from app.transfer import local_network, peer_workers
from app.web import api_bridge


class _RecordingDiscovery:
    """Stands in for the LAN discovery thread and records shutdown calls."""

    def __init__(self, stop_event: object, log: list) -> None:
        self._stop_event = stop_event
        self._log = log

    def close_discovery_socket(self) -> None:
        self._log.append(("close", self._stop_event.is_set()))

    def join(self, timeout: float | None = None) -> None:
        self._log.append(("join", self._stop_event.is_set(), timeout))


class ShutdownBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        peer_workers.stop_local_network_workers(timeout=0.1)
        self.addCleanup(peer_workers.stop_local_network_workers, 0.1)
        self.discovery_log: list = []
        self.discovery_stop_events: list = []

    def _settings(self) -> SimpleNamespace:
        return SimpleNamespace(
            use_fake_data=False,
            https_port=8443,
            http_only=True,
            log_dir=Path("/tmp"),
            stdout_log_file="drone.stdout.log",
            stderr_log_file="drone.stderr.log",
            username="admin",
        )

    def _fake_start_discovery(self, settings, fingerprint, on_discovery=None, stop_event=None):
        self.discovery_stop_events.append(stop_event)
        return _RecordingDiscovery(stop_event, self.discovery_log)

    def _run_main(self, *, swarm_enabled: bool = True, serve_effect=None, create_effect=None):
        settings = self._settings()
        server = mock.Mock(auth=None)
        server.serve_forever.side_effect = serve_effect

        def create_server(received_settings: object) -> mock.Mock:
            self.assertIs(received_settings, settings)
            if swarm_enabled:
                peer_workers._start_local_network_workers(settings)
            if create_effect is not None:
                raise create_effect
            return server

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(drone_api.Settings, "from_env", return_value=settings))
            stack.enter_context(mock.patch.object(drone_api, "_configure_rotating_logs"))
            stack.enter_context(mock.patch.object(
                drone_api._identity, "ensure_identity", return_value=SimpleNamespace(settings=settings),
            ))
            stack.enter_context(mock.patch.object(drone_api, "create_server", side_effect=create_server))
            stack.enter_context(mock.patch.object(drone_api, "_start_drone_auto_update_poller"))
            stack.enter_context(mock.patch.object(api_bridge, "maybe_start"))
            stack.enter_context(mock.patch.object(peer_workers, "ensure_tailnet_networking"))
            stack.enter_context(mock.patch.object(peer_workers, "Thread"))
            stack.enter_context(mock.patch.object(
                local_network, "start_discovery_worker", side_effect=self._fake_start_discovery,
            ))
            drone_api.main()
        return server

    def _assert_retired_in_order(self) -> None:
        self.assertEqual(len(self.discovery_stop_events), 1)
        self.assertTrue(self.discovery_stop_events[0].is_set())
        # The event must be set before discovery's socket is closed and joined.
        self.assertEqual(
            self.discovery_log,
            [("close", True), ("join", True, 2.0)],
        )
        self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)
        self.assertIsNone(peer_workers._LOCAL_NETWORK_DISCOVERY)

    def test_normal_serve_return_retires_generation_in_order(self) -> None:
        server = self._run_main()

        server.serve_forever.assert_called_once_with()
        self._assert_retired_in_order()

    def test_keyboard_interrupt_during_serve_retires_generation(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            self._run_main(serve_effect=KeyboardInterrupt())

        self._assert_retired_in_order()

    def test_failure_after_generation_start_retires_it_and_keeps_original_error(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "multiplayer worker exploded"):
            self._run_main(create_effect=RuntimeError("multiplayer worker exploded"))

        self._assert_retired_in_order()

    def test_swarm_disabled_exit_starts_no_discovery_and_is_harmless(self) -> None:
        server = self._run_main(swarm_enabled=False)

        server.serve_forever.assert_called_once_with()
        self.assertEqual(self.discovery_stop_events, [])
        self.assertEqual(self.discovery_log, [])
        self.assertFalse(peer_workers.local_network_workers_running())

    def test_retiring_again_after_exit_does_not_close_or_join_twice(self) -> None:
        self._run_main()
        before = list(self.discovery_log)

        peer_workers.stop_local_network_workers(timeout=0.1)
        peer_workers.stop_local_network_workers(timeout=0.1)

        self.assertEqual(self.discovery_log, before)

    def test_restart_after_shutdown_gets_fresh_unset_stop_event(self) -> None:
        self._run_main()
        old_stop = self.discovery_stop_events[0]

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(peer_workers, "ensure_tailnet_networking"))
            stack.enter_context(mock.patch.object(peer_workers, "Thread"))
            stack.enter_context(mock.patch.object(
                local_network, "start_discovery_worker", side_effect=self._fake_start_discovery,
            ))
            peer_workers._start_local_network_workers(self._settings())
            new_stop = self.discovery_stop_events[-1]
            self.assertIsNot(new_stop, old_stop)
            self.assertFalse(new_stop.is_set())
            self.assertTrue(old_stop.is_set())
            self.assertTrue(peer_workers.local_network_workers_running())
            peer_workers.stop_local_network_workers(timeout=0.1)

        self.assertTrue(new_stop.is_set())
        self.assertFalse(peer_workers.local_network_workers_running())


if __name__ == "__main__":
    unittest.main()
