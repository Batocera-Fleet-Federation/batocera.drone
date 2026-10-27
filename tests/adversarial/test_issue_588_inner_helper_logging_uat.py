"""Issue #588: inner VPN/RGS helper logging must not kill the poller.

The poller-level exception tests do not exercise failures caught inside
check_and_self_heal() and reconcile_rgs_with_tunnel(). Those helpers are also
used independently and must preserve their no-raise contract when formatting
an os.fsdecode-style surrogate or writing to a broken stderr.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


class _BrokenStderr(io.TextIOBase):
    encoding = "utf-8"
    errors = "strict"

    def write(self, _text: str) -> int:
        raise OSError("simulated stderr EIO")

    def flush(self) -> None:
        raise OSError("simulated stderr EIO")


class Issue588InnerHelperLoggingUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        with mock.patch.dict("os.environ", {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-588-inner-logging",
        }, clear=True):
            self.settings = Settings.from_env()

    def _run_two_ticks(self, stderr: io.TextIOBase) -> tuple[int, int, int, list[str]]:
        sleeps = 0

        def fake_sleep(_seconds: float) -> None:
            nonlocal sleeps
            sleeps += 1

        def retired(_generation: int) -> bool:
            return sleeps > 2

        # The first call from each real helper fails while preparing its
        # state. The second tick takes each helper's normal early-return path.
        state_results = iter([
            OSError("vpn path has invalid byte \udcff"),
            OSError("RGS path has invalid byte \udcff"),
            {"self_heal_enabled": False},
            {"has_config": False},
        ])

        def load_state(_settings):
            result = next(state_results)
            if isinstance(result, Exception):
                raise result
            return result

        with mock.patch.object(vpn_manager, "_log_poller_failure", wraps=vpn_manager._log_poller_failure) as log_failure, \
                mock.patch.object(vpn_manager, "poller_generation", return_value=588), \
                mock.patch.object(vpn_manager, "poller_retired", side_effect=retired), \
                mock.patch.object(vpn_manager.time, "sleep", side_effect=fake_sleep), \
                mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=True), \
                mock.patch.object(vpn_manager, "_load_state", side_effect=load_state) as load, \
                mock.patch.object(vpn_manager.sys, "stderr", stderr):
            vpn_manager.run_self_heal_poller(self.settings)

        return (
            sleeps,
            load.call_count,
            len(list(state_results)),
            [call.args[0] for call in log_failure.call_args_list],
        )

    def test_real_helpers_escape_surrogates_and_run_again_on_strict_utf8_stderr(self) -> None:
        output = io.BytesIO()
        strict_stderr = io.TextIOWrapper(
            output, encoding="utf-8", errors="strict", write_through=True,
        )

        sleeps, load_calls, remaining_states, log_messages = self._run_two_ticks(strict_stderr)

        self.assertEqual(sleeps, 3, "poller should retire only after completing two ticks")
        self.assertEqual(load_calls, 4, "both real helpers should execute on both ticks")
        self.assertEqual(remaining_states, 0)
        self.assertEqual(log_messages, [
            "VPN self-heal check failed", "RGS tunnel reconcile failed",
        ])
        self.assertEqual(output.getvalue().count(b"\\udcff"), 2)

    def test_real_helpers_swallow_broken_stderr_and_complete_later_tick(self) -> None:
        sleeps, load_calls, remaining_states, log_messages = self._run_two_ticks(_BrokenStderr())

        self.assertEqual(sleeps, 3, "broken stderr must not terminate the poller")
        self.assertEqual(load_calls, 4, "both real helpers should execute on both ticks")
        self.assertEqual(remaining_states, 0)
        self.assertEqual(log_messages, [
            "VPN self-heal check failed", "RGS tunnel reconcile failed",
        ])


if __name__ == "__main__":
    unittest.main()
