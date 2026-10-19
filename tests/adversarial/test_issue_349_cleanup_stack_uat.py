"""Issue #349: exercise registered cleanup through unittest, without LAN or HTTP.

Workers must retire before userdata is deleted even if either HTTP teardown step
raises. Use the original harness setUp and real unittest LIFO cleanup execution;
fake only external resources so no discovery workers or sockets are started.
"""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent
HARNESSES = (
    ("test_issue_152_media_keys_http_malformed_input.py", "MalformedHttpInputTests"),
    ("test_issue_152_media_keys_hold_uat.py", "HoldHttpContractTests"),
)


class CleanupStackUatTests(unittest.TestCase):
    def _exercise_cleanup_stack(self, failure_step):
        for filename, class_name in HARNESSES:
            with self.subTest(harness=filename, failure_step=failure_step):
                spec = importlib.util.spec_from_file_location("issue349_fixture", ROOT / filename)
                harness = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(harness)
                # Keep the real setup and cleanup registrations, without executing
                # media-key assertions belonging to the separate #152 suites.
                fixture_type = type("CleanupFixture", (getattr(harness, class_name),),
                                    {"runTest": lambda self: None})
                fixture = fixture_type("runTest")
                events = []
                real_temporary_directory = tempfile.TemporaryDirectory

                def temporary_directory():
                    directory = real_temporary_directory()
                    cleanup = directory.cleanup

                    def observed_cleanup():
                        events.append(("userdata_cleanup", Path(directory.name).is_dir()))
                        cleanup()

                    directory.cleanup = observed_cleanup
                    return directory

                server = mock.Mock(server_address=("127.0.0.1", 12345))
                if failure_step:
                    getattr(server, failure_step).side_effect = RuntimeError("injected HTTP failure")
                response = mock.MagicMock()
                response.__enter__.return_value.headers = {"Set-Cookie": "session=fixture"}
                with mock.patch.dict(os.environ, {}, clear=True), \
                        mock.patch("app.drone_api.Settings"), \
                        mock.patch("app.drone_api.create_server", return_value=server), \
                        mock.patch("app.mock_data.seed_mock_userdata"), \
                        mock.patch.object(harness.tempfile, "TemporaryDirectory", temporary_directory), \
                        mock.patch.object(harness.threading, "Thread"), \
                        mock.patch.object(harness.urllib.request, "urlopen", return_value=response), \
                        mock.patch.object(harness, "stop_local_network_workers",
                                          side_effect=lambda: events.append(("lan_stopped", True))):
                    result = unittest.TestResult()
                    fixture.run(result)

                self.assertEqual(result.testsRun, 1)
                self.assertEqual(result.failures, [])
                if failure_step:
                    self.assertEqual(len(result.errors), 1, result.errors)
                    self.assertIn("injected HTTP failure", result.errors[0][1])
                else:
                    self.assertEqual(result.errors, [])
                self.assertEqual(events, [("lan_stopped", True), ("userdata_cleanup", True)],
                                 "LAN workers must stop before unittest deletes userdata")
                self.assertFalse(Path(fixture._tmp.name).exists())

    def test_successful_http_teardown_retires_workers_before_userdata(self):
        self._exercise_cleanup_stack(None)

    def test_shutdown_error_still_retires_workers_before_userdata(self):
        self._exercise_cleanup_stack("shutdown")

    def test_server_close_error_still_retires_workers_before_userdata(self):
        self._exercise_cleanup_stack("server_close")


if __name__ == "__main__":
    unittest.main()
