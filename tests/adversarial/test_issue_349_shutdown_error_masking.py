"""Out-of-scope evidence for #349: a worker-stop timeout masks the HTTP shutdown error.

Kept in its own suite so it is not a blocking #349 assertion. The #349 defect is
that LAN workers are skipped when HTTP shutdown raises; the wrapper now stops them.
This suite records a separate error-reporting defect in the same except path: if
that stop times out, its TimeoutError replaces the original shutdown exception, so
the protected harness callback reports the worker timeout instead of the HTTP failure.
"""

import unittest
from unittest import mock

from app import drone_api


class ShutdownErrorMaskingEvidenceTests(unittest.TestCase):
    def test_worker_stop_timeout_does_not_replace_the_original_shutdown_error(self):
        server = drone_api.DroneThreadingHTTPServer.__new__(drone_api.DroneThreadingHTTPServer)
        shutdown_error = RuntimeError("primary shutdown failed")

        with mock.patch.object(drone_api.ThreadingHTTPServer, "shutdown", side_effect=shutdown_error), \
                mock.patch.object(drone_api, "stop_local_network_workers",
                                  side_effect=TimeoutError("Local-network worker did not stop")):
            with self.assertRaises(RuntimeError) as raised:
                server.shutdown()

        self.assertIs(raised.exception, shutdown_error)


if __name__ == "__main__":
    unittest.main()
