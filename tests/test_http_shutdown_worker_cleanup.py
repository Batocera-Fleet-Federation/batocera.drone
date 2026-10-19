"""HTTP shutdown must retire LAN workers even when a listener raises."""

import unittest
from unittest import mock

from app import drone_api


class HttpShutdownWorkerCleanupTests(unittest.TestCase):
    def test_owned_listener_failure_still_stops_local_network_workers(self) -> None:
        listener = mock.Mock(thread=object())
        listener.shutdown.side_effect = RuntimeError("listener shutdown failed")
        server = mock.Mock(_owned_compatibility_servers=(listener,))

        with mock.patch.object(drone_api.ThreadingHTTPServer, "shutdown") as base_shutdown, \
                mock.patch.object(drone_api, "stop_local_network_workers") as stop_workers:
            with self.assertRaisesRegex(RuntimeError, "listener shutdown failed"):
                drone_api.DroneThreadingHTTPServer.shutdown(server)

        base_shutdown.assert_not_called()
        stop_workers.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
