"""client/logging_setup.py -- redirecting stdout/stderr into the same log
directory the Drone service writes to, prefixed so lines from this process
are unambiguous when interleaved with the Drone's own.
"""

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from client import logging_setup


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._real_stdout = sys.stdout
        self._real_stderr = sys.stderr

    def tearDown(self):
        self._tmp.cleanup()
        sys.stdout = self._real_stdout
        sys.stderr = self._real_stderr

    def test_configure_creates_the_same_log_dir_the_drone_service_uses(self):
        with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(self.root)}, clear=True):
            logging_setup.configure()
        expected_dir = self.root / "system" / "logs" / "drone-app"
        self.assertTrue(expected_dir.is_dir())
        self.assertTrue((expected_dir / "stdout.log").exists())
        self.assertTrue((expected_dir / "stderr.log").exists())

    def test_log_dir_env_override_is_honored(self):
        custom_dir = self.root / "custom-logs"
        with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(self.root), "LOG_DIR": str(custom_dir)}, clear=True):
            logging_setup.configure()
        self.assertTrue((custom_dir / "stdout.log").exists())

    def test_written_lines_are_prefixed_and_timestamped(self):
        with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(self.root)}, clear=True):
            logging_setup.configure()
        print("hello from ports-client")
        sys.stdout.flush()
        content = (self.root / "system" / "logs" / "drone-app" / "stdout.log").read_text()
        self.assertIn("[ports-client] hello from ports-client", content)
        self.assertRegex(content, r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\] \[ports-client\]")

    def test_stderr_writes_go_to_stderr_log_not_stdout_log(self):
        with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(self.root)}, clear=True):
            logging_setup.configure()
        print("an error", file=sys.stderr)
        sys.stderr.flush()
        log_dir = self.root / "system" / "logs" / "drone-app"
        self.assertIn("an error", (log_dir / "stderr.log").read_text())
        self.assertNotIn("an error", (log_dir / "stdout.log").read_text())

    def test_original_stream_still_receives_output(self):
        fake_original = io.StringIO()
        with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(self.root)}, clear=True):
            sys.stdout = fake_original
            logging_setup.configure()
        print("still visible on console")
        self.assertIn("still visible on console", fake_original.getvalue())

    def test_a_single_print_call_gets_exactly_one_timestamp(self):
        # Guards the partial-line buffering: print() does two writes (the
        # text, then "\n" separately in some paths) -- must not split into
        # two timestamped lines.
        with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(self.root)}, clear=True):
            logging_setup.configure()
        print("one line")
        sys.stdout.flush()
        content = (self.root / "system" / "logs" / "drone-app" / "stdout.log").read_text()
        self.assertEqual(content.count("[ports-client]"), 1)

    def test_unwritable_log_dir_does_not_raise(self):
        unwritable_parent = self.root / "not-a-dir"
        unwritable_parent.write_text("blocking file, not a directory")
        with mock.patch.dict(
            "os.environ",
            {"USERDATA_ROOT": str(self.root), "LOG_DIR": str(unwritable_parent / "logs")},
            clear=True,
        ):
            logging_setup.configure()  # must not raise
        self.assertIs(sys.stdout, self._real_stdout)


if __name__ == "__main__":
    unittest.main()
