"""Issue #588 (from #582): real VPN self-heal and RGS helper failures keep the poller ticking.

Expected behaviour, derived from the #582 findings before reading the #588 diff:

* an exception raised inside the real ``check_and_self_heal`` body, whose text
  holds a lone surrogate (``os.fsdecode`` path), must be reported on a strict
  UTF-8 stderr and must not stop the poller, which keeps running every tick;
* the same holds for the real ``reconcile_rgs_with_tunnel`` body;
* an exception whose ``__str__`` itself raises must be reported as an
  unprintable exception rather than killing the poller.

These suites patch the helpers *below* the poller (status, state, RGS status)
instead of the poller's own call targets, so they exercise the real helper
bodies whichever name the poller calls them through.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


def _utf8_stream() -> io.TextIOWrapper:
    return io.TextIOWrapper(
        io.BytesIO(),
        encoding="utf-8",
        errors="strict",
        newline="\n",
        write_through=True,
    )


def _stream_text(stream: io.TextIOWrapper) -> str:
    return stream.buffer.getvalue().decode("utf-8")


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


class _Unprintable(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError("no text for you")


class Issue588RealHelperTickUAT(unittest.TestCase):
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
            "DRONE_DEVICE_ID": "adversarial-588-real-helper-tick",
        }, clear=True):
            self.settings = Settings.from_env()

    def _run_poller(self, *, live_ticks: int, stderr, extra_patches=()):
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        def retired(_generation: int) -> bool:
            return len(sleeps) > live_ticks

        caught: Optional[Exception] = None
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(vpn_manager, "poller_generation", return_value=588))
            stack.enter_context(mock.patch.object(vpn_manager, "poller_retired", new=retired))
            stack.enter_context(mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep))
            stack.enter_context(mock.patch.object(vpn_manager.sys, "stderr", stderr))
            for patch in extra_patches:
                stack.enter_context(patch)
            try:
                vpn_manager.run_self_heal_poller(self.settings)
            except Exception as error:
                caught = error
        return sleeps, caught

    def test_surrogate_self_heal_failure_is_logged_escaped_and_ticks_continue(self) -> None:
        stderr = _utf8_stream()
        reconcile = mock.Mock(return_value=None)
        patches = [
            mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=True),
            mock.patch.object(vpn_manager, "_load_state", return_value=_state()),
            mock.patch.object(vpn_manager, "status",
                              side_effect=RuntimeError("openvpn log: bad \udcff path")),
            mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel", reconcile),
        ]

        sleeps, caught = self._run_poller(live_ticks=2, stderr=stderr, extra_patches=patches)

        self.assertIsNone(caught, f"poller died on a surrogate self-heal failure: {caught!r}")
        self.assertEqual(reconcile.call_count, 2, "RGS must still reconcile on every live tick")
        self.assertEqual(len(sleeps), 3)
        text = _stream_text(stderr)
        self.assertIn(
            "VPN self-heal check failed: RuntimeError: openvpn log: bad \\udcff path",
            text,
            "the surrogate must be reported as an escaped literal, not dropped",
        )
        self.assertEqual(text.count("VPN self-heal check failed"), 2)

    def test_surrogate_rgs_failure_in_real_reconcile_is_logged_and_ticks_continue(self) -> None:
        stderr = _utf8_stream()
        rgs_status = mock.Mock(side_effect=OSError("rgs unit: bad \udcff path"))
        patches = [
            mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=True),
            # Self-heal disabled keeps the real check a silent no-op; reconcile still
            # reads the same state with has_config True.
            mock.patch.object(vpn_manager, "_load_state",
                              return_value=_state(self_heal_enabled=False)),
            mock.patch.object(vpn_manager, "tunnel_is_up", return_value=True),
            mock.patch.object(vpn_manager, "_tunnel_ip", return_value="10.8.0.2"),
            mock.patch.object(vpn_manager._rgs_manager, "status", rgs_status),
        ]

        _, caught = self._run_poller(live_ticks=2, stderr=stderr, extra_patches=patches)

        self.assertIsNone(caught, f"poller died on a surrogate RGS failure: {caught!r}")
        self.assertEqual(rgs_status.call_count, 2, "the real reconcile must run on every live tick")
        self.assertIn(
            "RGS tunnel reconcile failed: OSError: rgs unit: bad \\udcff path",
            _stream_text(stderr),
        )

    def test_error_with_unprintable_text_is_reported_and_ticks_continue(self) -> None:
        stderr = _utf8_stream()
        reconcile = mock.Mock(return_value=None)
        patches = [
            mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=True),
            mock.patch.object(vpn_manager, "_load_state", return_value=_state()),
            mock.patch.object(vpn_manager, "status", side_effect=_Unprintable("x")),
            mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel", reconcile),
        ]

        _, caught = self._run_poller(live_ticks=2, stderr=stderr, extra_patches=patches)

        self.assertIsNone(caught, f"poller died on an unprintable error: {caught!r}")
        self.assertEqual(reconcile.call_count, 2)
        self.assertIn(
            "VPN self-heal check failed: _Unprintable: <unprintable exception>",
            _stream_text(stderr),
        )


if __name__ == "__main__":
    unittest.main()
