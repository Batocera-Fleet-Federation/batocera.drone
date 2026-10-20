import unittest
from unittest import mock

from app import drone_api
from app.transfer import peer_workers


class LocalNetworkWorkerShutdownTests(unittest.TestCase):
    def test_main_stops_local_network_workers_when_serve_loop_exits(self) -> None:
        settings = mock.MagicMock(use_fake_data=False, https_port=8443, http_only=False)
        server = mock.MagicMock()
        server.serve_forever.side_effect = KeyboardInterrupt()
        with mock.patch.object(drone_api.Settings, "from_env", return_value=settings), \
                mock.patch.object(drone_api, "_configure_rotating_logs"), \
                mock.patch.object(drone_api._identity, "ensure_identity", return_value=mock.Mock(settings=settings)), \
                mock.patch.object(drone_api, "create_server", return_value=server), \
                mock.patch.object(drone_api, "_start_drone_auto_update_poller"), \
                mock.patch.object(drone_api, "stop_local_network_workers") as stop_workers:
            with self.assertRaises(KeyboardInterrupt):
                drone_api.main()
        stop_workers.assert_called_once_with()

    def test_stop_retires_running_generation(self) -> None:
        with mock.patch.object(peer_workers, "ensure_tailnet_networking"), \
                mock.patch.object(peer_workers, "DroneCertificateManager"), \
                mock.patch.object(peer_workers, "_LOCAL_NETWORK_STOP", None):
            peer_workers._start_local_network_workers(mock.Mock())
            self.assertTrue(peer_workers.local_network_workers_running())
            peer_workers.stop_local_network_workers(timeout=1.0)
        self.assertFalse(peer_workers.local_network_workers_running())


if __name__ == "__main__":
    unittest.main()
