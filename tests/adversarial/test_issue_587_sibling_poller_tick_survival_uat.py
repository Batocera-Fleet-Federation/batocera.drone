"""UAT for #587: SMTP and tailnet sharing-revocation pollers must survive unexpected ticks.

The #587 issue asks for the same tick guard on the SMTP and tailnet sharing-revocation
pollers as on the VPN one. Expected behavior, derived from the issue: an unexpected
Exception from one check is logged and the poller keeps its cadence, so a later tick
can still revoke the imported credential or auth key once the source peer 404s or is
no longer paired. Hostile failure text and a closed stderr must not escape the poller,
and a retired generation still stops it before the next check.
"""

from __future__ import annotations

import contextlib
import errno
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.smtp_manager as smtp_manager
import app.device.tailnet_service as tailnet_service
from app.drone_api import Settings


def _verified_tls(error):
    """Mark a peer status as received on the pinned, verified TLS channel (#599)."""
    from app.transfer import peer_connectivity as _pc
    error._peer_tls_evidence = _pc._PEER_TLS_EVIDENCE
    return error


REAL = object()  # marker: run the real check for this tick
PEER = {"drone_id": "peer-1", "name": "Peer"}


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


class _SiblingPollerTickSurvival:
    """Shared contract for the SMTP and tailnet pollers; concrete classes bind the module."""

    module = None
    check_name = ""
    poller_name = ""
    paired_patch_target = "app.transfer.local_network.get_paired_peer"
    peer_path = ""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        with mock.patch.dict("os.environ", {
            "USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"), "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-587-sibling-tick-survival",
        }, clear=True):
            self.settings = Settings.from_env()
        self.seed_imported_share(self.settings)
        self.assertFalse(self.is_revoked(self.settings), "precondition: imported share is still active")

    # Subclasses provide the module-specific state seeding and inspection.
    def seed_imported_share(self, settings: Settings) -> None:
        raise NotImplementedError

    def is_revoked(self, settings: Settings) -> bool:
        raise NotImplementedError

    def extra_patches(self):
        return []

    def _run_poller(self, outcomes, retire_after, paired=PEER):
        clock = _ScriptedClock(retire_after)
        real_check = getattr(self.module, self.check_name)
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

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(self.module, "poller_generation", return_value=587))
            stack.enter_context(mock.patch.object(self.module, "poller_retired", side_effect=clock.retired))
            stack.enter_context(mock.patch.object(self.module.time, "sleep", side_effect=clock.sleep))
            stack.enter_context(mock.patch.object(self.module, self.check_name, side_effect=scripted_check))
            for patch in self.extra_patches():
                stack.enter_context(patch)
            if isinstance(paired, list):
                stack.enter_context(mock.patch(self.paired_patch_target, side_effect=paired))
            else:
                stack.enter_context(mock.patch(self.paired_patch_target, return_value=paired))
            stack.enter_context(mock.patch(self.peer_patch_target, side_effect=self.peer_missing_error()))
            getattr(self.module, self.poller_name)(self.settings)
        return len(calls)

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
        with mock.patch.object(self.module.sys, "stderr", strict_stderr):
            calls = self._run_poller(outcomes, retire_after=5)
        strict_stderr.flush()

        self.assertEqual(calls, 5, "every failing tick must be followed by another check")
        self.assertIn(b"\\udcff", raw.getvalue(), "surrogate failure text must be escaped, not raised")
        self.assertTrue(self.is_revoked(self.settings))

    def test_closed_stderr_on_failed_tick_does_not_stop_later_revocation(self) -> None:
        closed = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        closed.close()
        with mock.patch.object(self.module.sys, "stderr", closed):
            calls = self._run_poller([RuntimeError("first tick"), REAL], retire_after=2)

        self.assertEqual(calls, 2)
        self.assertTrue(self.is_revoked(self.settings))

    def test_retirement_after_failing_tick_stops_before_next_check(self) -> None:
        calls = self._run_poller([RuntimeError("boom"), REAL], retire_after=1)

        self.assertEqual(calls, 1, "a retired generation must not run another check")
        self.assertFalse(self.is_revoked(self.settings), "no later tick ran, so nothing was revoked")

    def test_surrogate_roster_failure_inside_real_check_is_survived(self) -> None:
        # Real check: the roster lookup raises with surrogate text, so the check's own stderr report
        # fails on strict stderr. The poller guard must absorb it and tick 2 must still revoke.
        raw = io.BytesIO()
        strict_stderr = io.TextIOWrapper(raw, encoding="utf-8", errors="strict", write_through=True)
        with mock.patch.object(self.module.sys, "stderr", strict_stderr):
            calls = self._run_poller([REAL, REAL], retire_after=2, paired=[RuntimeError("roster \udcff"), PEER])
        strict_stderr.flush()

        self.assertEqual(calls, 2)
        self.assertTrue(self.is_revoked(self.settings))

    def test_broken_stderr_during_real_check_error_report_is_survived(self) -> None:
        # The roster lookup fails and the check's own stderr report then hits a closed pipe.
        closed = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        closed.close()
        with mock.patch.object(self.module.sys, "stderr", closed):
            calls = self._run_poller([REAL, REAL], retire_after=2, paired=[RuntimeError("roster lookup failed"), PEER])

        self.assertEqual(calls, 2)
        self.assertTrue(self.is_revoked(self.settings))


class SmtpSharingPollerTickSurvivalUAT(_SiblingPollerTickSurvival, unittest.TestCase):
    module = smtp_manager
    check_name = "check_sharing_revocation"
    poller_name = "run_sharing_revocation_poller"
    peer_patch_target = "app.transfer.peer_connectivity._peer_get_json_for_peer"

    def peer_missing_error(self):
        return _verified_tls(smtp_manager.HTTPError("https://peer/v1/api/peer/smtp/config", 404, "missing", None, None))

    def seed_imported_share(self, settings: Settings) -> None:
        smtp_manager._save_state(
            settings, has_config=True, host="smtp.example.net", username="peer-user", password="smtp-secret",
            source_peer_id="peer-1", source_peer_name="Peer",
        )

    def is_revoked(self, settings: Settings) -> bool:
        # SMTP revocation wipes the imported password and keeps the rest of the config.
        return smtp_manager._load_state(settings)["password"] == ""


class TailnetSharingPollerTickSurvivalUAT(_SiblingPollerTickSurvival, unittest.TestCase):
    module = tailnet_service
    check_name = "check_tailnet_sharing_revocation"
    poller_name = "run_tailnet_sharing_revocation_poller"
    peer_patch_target = "app.transfer.peer_connectivity._peer_get_json_for_peer"

    def peer_missing_error(self):
        return _verified_tls(tailnet_service.urllib.error.HTTPError("https://peer/v1/api/peer/tailnet/config", 404, "missing", None, None))

    def seed_imported_share(self, settings: Settings) -> None:
        tailnet_service._save_sharing_state(settings, auth_key="tskey-adversarial-587", source_peer_id="peer-1", source_peer_name="Peer")

    def is_revoked(self, settings: Settings) -> bool:
        return tailnet_service._load_sharing_state(settings)["auth_key"] == ""


if __name__ == "__main__":
    unittest.main()
