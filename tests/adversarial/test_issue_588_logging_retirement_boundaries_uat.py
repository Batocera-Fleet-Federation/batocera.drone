"""Issue #588 (from #582): a logging failure must not change poller control flow.

#582 requires that a generation retired while a *successful* self-heal check is
in flight performs no further RGS action. #588 gates that retirement check on
``not check_failed``, and ``check_failed`` is set whenever the real check
raises. The success summary is printed inside that same check, so a closed
stdout turns a successful reconnect into a "failed" check and skips the
retirement gate. A write to a closed log stream must therefore not change
whether a retired generation reconciles RGS.

Also covered (control, must keep passing): a failed check whose stderr is
closed still gets an RGS attempt on every live tick.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


def _closed_stream() -> io.TextIOWrapper:
    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", write_through=True)
    stream.close()
    return stream


def _utf8_stream() -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding="utf-8", write_through=True)


def _state(**overrides) -> dict:
    state = {
        "self_heal_enabled": True,
        "has_config": True,
        "has_credentials": True,
        "self_heal_last_at": None,
        "self_heal_attempts": [],
        "rgs_tunnel_ip": "",
        "rgs_restart_last_at": None,
    }
    state.update(overrides)
    return state


class Issue588LoggingRetirementBoundariesUAT(unittest.TestCase):
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
            "DRONE_DEVICE_ID": "adversarial-588-logging-retirement",
        }, clear=True):
            self.settings = Settings.from_env()

    def _run_poller(self, *, live_ticks: int, retired_fn, stdout, stderr, patches):
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        def retired(generation: int) -> bool:
            return retired_fn(generation) or len(sleeps) > live_ticks

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(vpn_manager, "poller_generation", return_value=588))
            stack.enter_context(mock.patch.object(vpn_manager, "poller_retired", new=retired))
            stack.enter_context(mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep))
            stack.enter_context(mock.patch.object(vpn_manager.sys, "stdout", stdout))
            stack.enter_context(mock.patch.object(vpn_manager.sys, "stderr", stderr))
            for patch in patches:
                stack.enter_context(patch)
            vpn_manager.run_self_heal_poller(self.settings)
        return sleeps

    def _reconnect_then_retire_patches(self, reconcile, retired_now, reconnect_result):
        def reconnect_then_retire(_settings):
            retired_now["value"] = True
            return reconnect_result

        return [
            mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=True),
            mock.patch.object(vpn_manager, "_load_state", return_value=_state()),
            mock.patch.object(vpn_manager, "status", return_value={}),
            mock.patch.object(vpn_manager, "_self_heal_reason", return_value="tunnel down"),
            mock.patch.object(vpn_manager, "reconnect", side_effect=reconnect_then_retire),
            mock.patch.object(vpn_manager, "_save_state", return_value={}),
            mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel", reconcile),
        ]

    def test_successful_reconnect_with_closed_stdout_still_stops_rgs_after_retirement(self) -> None:
        # Shutdown retires the generation while the reconnect is in flight. The
        # reconnect itself succeeds; only the stdout summary line fails to write.
        retired_now = {"value": False}
        reconcile = mock.Mock(return_value=None)
        sleeps = self._run_poller(
            live_ticks=5,
            retired_fn=lambda _generation: retired_now["value"],
            stdout=_closed_stream(),
            stderr=_utf8_stream(),
            patches=self._reconnect_then_retire_patches(
                reconcile, retired_now, {"connected": True},
            ),
        )

        self.assertEqual(len(sleeps), 1, "the retired poller must return after the first tick")
        self.assertEqual(
            reconcile.call_count, 0,
            "a generation retired during a successful reconnect must not reconcile RGS, "
            "even when the success summary cannot be written to stdout",
        )

    def test_successful_reconnect_retired_with_healthy_stdout_stops_rgs(self) -> None:
        # Control: the same retirement with a writable stdout is already correct.
        retired_now = {"value": False}
        reconcile = mock.Mock(return_value=None)
        sleeps = self._run_poller(
            live_ticks=5,
            retired_fn=lambda _generation: retired_now["value"],
            stdout=_utf8_stream(),
            stderr=_utf8_stream(),
            patches=self._reconnect_then_retire_patches(
                reconcile, retired_now, {"connected": True},
            ),
        )

        self.assertEqual(len(sleeps), 1)
        self.assertEqual(reconcile.call_count, 0)

    def test_failed_check_with_closed_stderr_still_reconciles_rgs_every_live_tick(self) -> None:
        # Control: a failed check whose report cannot be written still gets its
        # independent RGS attempt on each live tick.
        reconcile = mock.Mock(return_value=None)
        sleeps = self._run_poller(
            live_ticks=2,
            retired_fn=lambda _generation: False,
            stdout=_utf8_stream(),
            stderr=_closed_stream(),
            patches=[
                mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=True),
                mock.patch.object(vpn_manager, "_load_state", return_value=_state()),
                mock.patch.object(vpn_manager, "status",
                                  side_effect=RuntimeError("openvpn log: unexpected")),
                mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel", reconcile),
            ],
        )

        self.assertEqual(len(sleeps), 3)
        self.assertEqual(reconcile.call_count, 2)


if __name__ == "__main__":
    unittest.main()
