"""Issue #349: the production HTTP server owns cleanup on shutdown failure.

The protected harness callbacks call server.shutdown() before their explicit
LAN cleanup. When socketserver shutdown raises, the server wrapper must retire
the LAN generation before propagating the original exception to that callback.
"""

import unittest
from unittest import mock

from app import drone_api


class ServerShutdownBoundaryTests(unittest.TestCase):
    def test_base_exception_from_primary_shutdown_stops_workers_and_is_preserved(self):
        server = drone_api.DroneThreadingHTTPServer.__new__(
            drone_api.DroneThreadingHTTPServer
        )
        shutdown_error = KeyboardInterrupt("injected shutdown interruption")

        with mock.patch.object(
            drone_api.ThreadingHTTPServer,
            "shutdown",
            side_effect=shutdown_error,
        ), mock.patch.object(
            drone_api, "stop_local_network_workers"
        ) as stop_workers:
            with self.assertRaises(KeyboardInterrupt) as raised:
                server.shutdown()

        self.assertIs(raised.exception, shutdown_error)
        stop_workers.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
