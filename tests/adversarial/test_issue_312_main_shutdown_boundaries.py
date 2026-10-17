"""Issue #312 entrypoint shutdown boundaries.

Expected behavior derived from the issue before inspecting the delivery:

* ``create_server`` starts the local-network generation for an enabled Drone.
* Once that generation exists, every subsequent process-exit path in ``main``
  must retire it.  That includes a normal serve-loop return and startup failure
  after server creation but before ``serve_forever`` begins.
* Retiring a generation signals its event and closes and joins LAN discovery;
  leaving these resources alive during process teardown was the reported bug.

The test uses the real generation bookkeeping and stop routine, while replacing
network I/O and thread execution with deterministic handles.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from app import drone_api
from app.transfer import local_network, peer_workers
from app.web import api_bridge


class MainShutdownBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        peer_workers.stop_local_network_workers(timeout=0.1)
        self.addCleanup(peer_workers.stop_local_network_workers, 0.1)

    def _settings(self) -> mock.Mock:
        return mock.Mock(
            use_fake_data=False,
            https_port=8443,
            http_only=True,
            log_dir=Path("/tmp"),
            stdout_log_file="drone.stdout.log",
            stderr_log_file="drone.stderr.log",
            username="admin",
        )

    def _run_main(self, *, serve_effect: object = None, poller_effect: object = None):
        settings = self._settings()
        server = mock.Mock(auth=None)
        server.serve_forever.side_effect = serve_effect
        discovery = mock.Mock()

        def create_server_and_start_workers(received_settings: object) -> mock.Mock:
            self.assertIs(received_settings, settings)
            peer_workers._start_local_network_workers(settings)
            return server

        with mock.patch.object(drone_api.Settings, "from_env", return_value=settings), \
                mock.patch.object(drone_api, "_configure_rotating_logs"), \
                mock.patch.object(
                    drone_api._identity,
                    "ensure_identity",
                    return_value=SimpleNamespace(settings=settings),
                ), \
                mock.patch.object(drone_api, "create_server", side_effect=create_server_and_start_workers), \
                mock.patch.object(drone_api, "_start_drone_auto_update_poller", side_effect=poller_effect), \
                mock.patch.object(api_bridge, "maybe_start"), \
                mock.patch.object(peer_workers, "Thread"), \
                mock.patch.object(peer_workers, "ensure_tailnet_networking"), \
                mock.patch.object(peer_workers, "DroneCertificateManager"), \
                mock.patch.object(local_network, "start_discovery_worker", return_value=discovery):
            if isinstance(poller_effect, BaseException):
                with self.assertRaisesRegex(type(poller_effect), str(poller_effect)):
                    drone_api.main()
            elif isinstance(serve_effect, BaseException):
                with self.assertRaisesRegex(type(serve_effect), str(serve_effect)):
                    drone_api.main()
            else:
                drone_api.main()

        return server, discovery

    def _assert_generation_retired(self, discovery: mock.Mock) -> None:
        self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)
        self.assertIsNone(peer_workers._LOCAL_NETWORK_DISCOVERY)
        discovery.close_discovery_socket.assert_called_once_with()
        discovery.join.assert_called_once_with(2.0)

    def test_normal_serve_loop_return_retires_generation(self) -> None:
        server, discovery = self._run_main()

        server.serve_forever.assert_called_once_with()
        self._assert_generation_retired(discovery)

    def test_startup_failure_after_server_creation_retires_generation(self) -> None:
        error = RuntimeError("auto-update bootstrap failed")
        server, discovery = self._run_main(poller_effect=error)

        server.serve_forever.assert_not_called()
        self._assert_generation_retired(discovery)


if __name__ == "__main__":
    unittest.main()
