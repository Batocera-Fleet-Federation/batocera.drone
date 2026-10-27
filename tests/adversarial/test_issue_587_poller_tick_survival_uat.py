"""UAT for #587: unexpected VPN revocation-poller ticks must not end revocation.

Expected behavior, derived from the issue before reading the patch: a tick that
raises an unexpected Exception is logged and the poller keeps its cadence, so a
later tick can still see the source peer's 404 (or "no longer paired") and wipe
imported credentials and the live tunnel. Hostile failure text (surrogates,
unprintable exceptions, a closed stderr) is logged without escaping the poller.
A retired generation still stops the poller before its next check. Only
Exception is absorbed; a BaseException is a shutdown signal and is not covered.
"""

from __future__ import annotations

import contextlib
import errno
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


SAMPLE_OVPN = "client\ndev tun\nproto udp\nremote vpn.example.net 1194\nauth-user-pass\n"
PEER = {"drone_id": "peer-1", "name": "Peer"}
REAL = object()  # marker: run the real check_sharing_revocation for this tick


class _UnprintableError(RuntimeError):
    def __str__(self) -> str:
        raise UnicodeEncodeError("utf-8", "\udcff", 0, 1, "surrogates not allowed")


class _NonStringError(RuntimeError):
    def __str__(self) -> str:
        return 42  # type: ignore[return-value]


class _ScriptedClock:
    """Stands in for time.sleep and poller_retired: retire after N completed sleeps."""

    def __init__(self, retire_after: int) -> None:
        self.retire_after = retire_after
        self.sleeps = 0

    def sleep(self, _seconds: float) -> None:
        self.sleeps += 1

    def retired(self, _generation: int) -> bool:
        return self.sleeps > self.retire_after


class Issue587PollerTickSurvivalUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        install = mock.patch.object(vpn_manager, "_drone_install_root", return_value=root / "install")
        self.addCleanup(install.stop)
        install.start()
        with mock.patch.dict("os.environ", {
            "USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"), "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-587-tick-survival",
        }, clear=True):
            self.settings = Settings.from_env()
        vpn_manager.import_from_peer(
            self.settings,
            {
                "config_filename": "client.ovpn", "config_text": SAMPLE_OVPN,
                "has_credentials": True, "username": "peer-user", "password": "secret",
            },
            source_peer_id="peer-1", source_peer_name="Peer",
        )
        self.assertTrue(vpn_manager.auth_path(self.settings).exists(), "precondition: imported credentials on disk")

    @contextlib.contextmanager
    def _peer_says_sharing_off(self, paired=PEER):
        missing = vpn_manager.HTTPError("https://peer/v1/api/peer/vpn/config", 404, "missing", None, None)
        if isinstance(paired, list):
            paired_patch = mock.patch("app.transfer.local_network.get_paired_peer", side_effect=paired)
        else:
            paired_patch = mock.patch("app.transfer.local_network.get_paired_peer", return_value=paired)
        with paired_patch, \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=missing):
            yield

    def _run_poller(self, outcomes, retire_after, paired=PEER, disconnect=None):
        """Run the real poller; each tick consumes one outcome (exception, REAL, or a value)."""
        clock = _ScriptedClock(retire_after)
        disconnect_patch = mock.patch.object(vpn_manager, "disconnect", **(disconnect or {"return_value": {"status": "not_running"}}))
        real_check = vpn_manager.check_sharing_revocation
        queue = list(outcomes)
        calls = []

        def scripted_check(settings):
            calls.append(settings)
            outcome = queue.pop(0)
            if outcome is REAL:
                return real_check(settings)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        with mock.patch.object(vpn_manager, "poller_generation", return_value=587), \
                mock.patch.object(vpn_manager, "poller_retired", side_effect=clock.retired), \
                mock.patch.object(vpn_manager.time, "sleep", side_effect=clock.sleep), \
                mock.patch.object(vpn_manager, "check_sharing_revocation", side_effect=scripted_check), \
                disconnect_patch, \
                self._peer_says_sharing_off(paired):
            vpn_manager.run_sharing_revocation_poller(self.settings)
        return len(calls)

    def _assert_revoked(self) -> None:
        state = vpn_manager._load_state(self.settings)
        self.assertFalse(state["has_credentials"])
        self.assertFalse(vpn_manager.auth_path(self.settings).exists())
        self.assertIn("turned off sharing", state["revoked_reason"])

    def test_repeated_hostile_failures_then_real_revocation_still_happens(self) -> None:
        outcomes = [
            RuntimeError("broken stderr path: \udcff"),
            _UnprintableError("unused"),
            OSError(errno.EPIPE, "Broken pipe"),
            _NonStringError("unused"),
            REAL,
        ]
        raw = io.BytesIO()
        strict_stderr = io.TextIOWrapper(raw, encoding="utf-8", errors="strict", write_through=True)
        with mock.patch.object(vpn_manager.sys, "stderr", strict_stderr):
            calls = self._run_poller(outcomes, retire_after=5)
        strict_stderr.flush()

        self.assertEqual(calls, 5, "every failing tick must be followed by another check")
        self.assertIn(b"\\udcff", raw.getvalue(), "surrogate failure text must be escaped, not raised")
        self._assert_revoked()

    def test_closed_stderr_on_failed_tick_does_not_stop_later_revocation(self) -> None:
        closed = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        closed.close()
        with mock.patch.object(vpn_manager.sys, "stderr", closed):
            calls = self._run_poller([RuntimeError("first tick"), REAL], retire_after=2)

        self.assertEqual(calls, 2)
        self._assert_revoked()

    def test_retirement_after_failing_tick_stops_before_next_check(self) -> None:
        calls = self._run_poller([RuntimeError("boom"), REAL], retire_after=1)

        self.assertEqual(calls, 1, "a retired generation must not run another check")
        self.assertTrue(vpn_manager.auth_path(self.settings).exists(), "no later tick ran, so nothing was revoked")

    def test_long_run_of_unexpected_failures_does_not_retire_poller(self) -> None:
        outcomes = [RuntimeError(f"tick {index}") for index in range(50)] + [REAL]
        calls = self._run_poller(outcomes, retire_after=51)

        self.assertEqual(calls, 51)
        self._assert_revoked()

    def test_failed_real_check_then_peer_removed_revokes_with_peer_gone_reason(self) -> None:
        # Real check path: get_paired_peer raises once (an unexpected roster failure), then the
        # peer is no longer paired at all. The second tick must still revoke.
        calls = self._run_poller([REAL, REAL], retire_after=2, paired=[RuntimeError("roster db locked"), None])

        self.assertEqual(calls, 2)
        state = vpn_manager._load_state(self.settings)
        self.assertFalse(state["has_credentials"])
        self.assertFalse(vpn_manager.auth_path(self.settings).exists())
        self.assertIn("no longer paired", state["revoked_reason"])

    def test_surrogate_roster_failure_inside_real_check_is_survived(self) -> None:
        # No scripted check: the real check's own stderr report of this failure raises
        # UnicodeEncodeError on strict stderr. The poller guard must absorb that and tick 2 revokes.
        raw = io.BytesIO()
        strict_stderr = io.TextIOWrapper(raw, encoding="utf-8", errors="strict", write_through=True)
        with mock.patch.object(vpn_manager.sys, "stderr", strict_stderr):
            calls = self._run_poller([REAL, REAL], retire_after=2, paired=[RuntimeError("roster \udcff"), PEER])
        strict_stderr.flush()

        self.assertEqual(calls, 2)
        self._assert_revoked()

    def test_broken_stderr_during_real_check_error_report_is_survived(self) -> None:
        # The issue's own scenario: the roster lookup fails and the check's inner stderr report then
        # hits a closed pipe. That escapes check_sharing_revocation; tick 2 must still revoke.
        closed = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        closed.close()
        with mock.patch.object(vpn_manager.sys, "stderr", closed):
            calls = self._run_poller([REAL, REAL], retire_after=2, paired=[RuntimeError("roster lookup failed"), PEER])

        self.assertEqual(calls, 2)
        self._assert_revoked()

    def test_non_string_error_during_real_check_is_survived(self) -> None:
        # An exception whose __str__ is broken makes the check's own f-string report raise too.
        calls = self._run_poller([REAL, REAL], retire_after=2, paired=[_NonStringError("roster"), PEER])

        self.assertEqual(calls, 2)
        self._assert_revoked()

    def test_disconnect_failure_on_revoking_tick_is_retried_by_next_tick(self) -> None:
        # Real check and real revocation: the first revocation's disconnect fails before credentials
        # are removed, so nothing is wiped on tick 1. Tick 2 must finish the job.
        disconnect = mock.Mock(side_effect=[RuntimeError("tunnel busy"), {"status": "not_running"}])
        calls = self._run_poller([REAL, REAL], retire_after=2, disconnect={"new": disconnect})

        self.assertEqual(calls, 2)
        self.assertEqual(disconnect.call_count, 2)
        self._assert_revoked()


if __name__ == "__main__":
    unittest.main()
