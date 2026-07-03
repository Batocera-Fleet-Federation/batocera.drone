"""Overmind-related logs are routed to a dedicated overmind.log, separate from stdout,
and surfaced as the 'drone_overmind' Log Source.
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.drone_api as drone_api
import app.common.logging_setup as logging_setup
from app.drone_api import Settings, _TeeRotatingStream, _overmind_log
from app.overmind.overmind_reporting import collect_log_sources


class OvermindLoggingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self._tmp.name)
        self._prev_stream = logging_setup._OVERMIND_LOG_STREAM
        self.overmind_path = self.log_dir / "overmind.log"
        logging_setup._OVERMIND_LOG_STREAM = _TeeRotatingStream(
            original_stream=None, log_path=self.overmind_path, max_bytes=0, backup_count=0
        )

    def tearDown(self):
        logging_setup._OVERMIND_LOG_STREAM = self._prev_stream
        self._tmp.cleanup()

    def test_detail_goes_to_overmind_log_only_high_level_also_stdout(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _overmind_log("Heartbeat send succeeded: status=200")          # detail -> overmind.log only
            _overmind_log("Asset metadata sync finished: uploaded=True", also_stdout=True)  # high level -> both
        stdout_text = buf.getvalue()
        overmind_text = self.overmind_path.read_text(encoding="utf-8")

        # overmind.log captures everything (timestamped).
        self.assertIn("Heartbeat send succeeded: status=200", overmind_text)
        self.assertIn("Asset metadata sync finished: uploaded=True", overmind_text)
        self.assertRegex(overmind_text.splitlines()[0], r"^\[.*Z\] ")  # timestamped

        # stdout gets only the high-level line, not the heartbeat detail.
        self.assertNotIn("Heartbeat send succeeded", stdout_text)
        self.assertIn("Asset metadata sync finished: uploaded=True", stdout_text)

    def test_fallback_to_stdout_when_stream_unconfigured(self):
        logging_setup._OVERMIND_LOG_STREAM = None
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _overmind_log("orphan overmind line")
        self.assertIn("orphan overmind line", buf.getvalue())

    def test_drone_overmind_registered_as_log_source(self):
        with mock.patch.dict(
            "os.environ",
            {"USERDATA_ROOT": str(self.log_dir / "userdata"), "LOG_DIR": str(self.log_dir),
             "OVERMIND_LOG_FILE": "overmind.log"},
            clear=True,
        ):
            settings = Settings.from_env()
        self.overmind_path.write_text("[2026-01-01T00:00:00Z] Heartbeat send succeeded\n", encoding="utf-8")

        payload = collect_log_sources(settings, include_unchanged=True)
        sources = {entry["source"] for entry in payload["logs"]}
        self.assertIn("drone_overmind", sources)


if __name__ == "__main__":
    unittest.main()
