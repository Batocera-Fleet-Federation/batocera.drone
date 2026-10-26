"""Fresh adversarial UAT for #586: surrogate-escaped tick failures on the sharing poller.

Issue #586: one unexpected exception from check_sharing_revocation must not stop
run_sharing_revocation_poller. Expected behavior, derived from the issue and the
repo's stderr contract (a broken stderr is never a reason to retire a poller):

* A tick failure whose text carries a lone surrogate (os.fsdecode leaves these in
  exception text for undecodable paths) is reported on a strict UTF-8 stderr
  without raising, and the next tick still runs.
* The report keeps the failure's class and message, escaped rather than dropped.

The #582 suites cover this surrogate path for run_self_heal_poller only; this file
covers the sharing-revocation poller that #586 is about.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


def _strict_utf8_stderr() -> tuple[io.TextIOWrapper, io.BytesIO]:
    raw = io.BytesIO()
    return io.TextIOWrapper(raw, encoding="utf-8", errors="strict", write_through=True), raw


class SurrogateStderrSharingPollerUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patcher = mock.patch.object(vpn_manager, "_drone_install_root", return_value=root / "install-root")
        self.addCleanup(patcher.stop)
        patcher.start()
        with mock.patch.dict("os.environ", {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-586-fresh-surrogate-stderr",
        }, clear=True):
            self.settings = Settings.from_env()

    def _run_ticks(self, *, live_ticks: int, check, stream) -> list[float]:
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        def retired(_generation: int) -> bool:
            return len(sleeps) > live_ticks

        with mock.patch.object(vpn_manager, "poller_generation", return_value=31), \
                mock.patch.object(vpn_manager, "poller_retired", new=retired), \
                mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep), \
                mock.patch.object(vpn_manager, "check_sharing_revocation", check), \
                mock.patch.object(vpn_manager.sys, "stderr", stream):
            vpn_manager.run_sharing_revocation_poller(self.settings)
        return sleeps

    def test_premise_strict_utf8_stderr_rejects_a_raw_surrogate(self) -> None:
        # Guards the premise: if this stream accepted surrogates, the test below
        # would not exercise the encoding hazard at all.
        stream, _ = _strict_utf8_stderr()
        with self.assertRaises(UnicodeEncodeError):
            stream.write("bad \udcff path\n")

    def test_surrogate_failure_on_strict_stderr_does_not_stop_the_poller(self) -> None:
        stream, raw = _strict_utf8_stderr()
        check = mock.Mock(side_effect=[RuntimeError("bad \udcff path"), False])

        sleeps = self._run_ticks(live_ticks=2, check=check, stream=stream)

        self.assertEqual(check.call_count, 2, "a surrogate tick failure stopped the sharing poller")
        self.assertEqual(len(sleeps), 3)
        stream.flush()
        report = raw.getvalue().decode("utf-8")
        self.assertIn("VPN sharing revocation check failed", report)
        self.assertIn("RuntimeError", report)
        self.assertIn("\\udcff", report, "the surrogate must be escaped, not dropped")

    def test_surrogate_failure_on_strict_stderr_still_reports_the_next_real_failure(self) -> None:
        stream, raw = _strict_utf8_stderr()
        check = mock.Mock(side_effect=[
            OSError("first \udcff path"),
            RuntimeError("second plain failure"),
            False,
        ])

        self._run_ticks(live_ticks=3, check=check, stream=stream)

        self.assertEqual(check.call_count, 3)
        stream.flush()
        report = raw.getvalue().decode("utf-8")
        self.assertIn("OSError", report)
        self.assertIn("RuntimeError: second plain failure", report)


if __name__ == "__main__":
    unittest.main()
