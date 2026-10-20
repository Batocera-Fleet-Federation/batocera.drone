"""Issue #420 UAT: a poller left behind by a closed server must not poison the next test.

The #191/#212/#249 teardown suites pass alone but failed in combined discovery: an earlier
``create_server`` harness had latched the process-wide forever-loop pollers (automation, outbound
mail, ...). They cannot be interrupted mid-sleep and re-resolve ``DRONE_STATE_DATABASE_FILE`` on
every tick, so they kept opening the *current* test's database after its stop, and ``open_database``
re-created temporary roots that had just been deleted.

Expected behavior:

- ``retire_background_pollers()`` makes every such poller return, before any storage access, the
  first time it wakes, and clears the start latches so a later ``create_server`` restarts them.
- A poller started after the retirement is unaffected.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from app import drone_api
from app.common import runtime_state
from app.common.settings import Settings
from app.device import automation, network_share_manager, smtp_manager, tailnet_service, vpn_manager
from app.storage import state_store

_LATCHES = (
    "_AUTOMATION_POLLER_STARTED",
    "_VPN_SHARING_POLLER_STARTED",
    "_VPN_SELF_HEAL_POLLER_STARTED",
    "_NETWORK_SHARE_WATCHDOG_STARTED",
    "_SMTP_SHARING_POLLER_STARTED",
    "_AUDIT_EMAIL_POLLER_STARTED",
    "_TAILNET_SHARING_POLLER_STARTED",
)


class _Sleep:
    """``time.sleep`` stand-in: retire the pollers on the first sleep, then let the loop wake."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, seconds: float) -> None:
        self.calls += 1
        if self.calls > 3:
            raise AssertionError("poller kept looping after it was retired")
        if self.calls == 1:
            drone_api.retire_background_pollers()


class PollerRetirementUAT(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="issue-420-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.settings = mock.Mock(spec=Settings)
        self.settings.userdata_root = self.root / "userdata"

    def test_retire_clears_every_start_latch(self) -> None:
        for name in _LATCHES:
            self.addCleanup(setattr, drone_api, name, getattr(drone_api, name))
            setattr(drone_api, name, True)
        generation = runtime_state.poller_generation()
        drone_api.retire_background_pollers()
        self.assertTrue(runtime_state.poller_retired(generation))
        for name in _LATCHES:
            self.assertFalse(getattr(drone_api, name), name)

    def _assert_returns_without_work(self, module, runner: str, work: str) -> None:
        sleeper = _Sleep()
        with mock.patch.object(module.time, "sleep", sleeper), \
                mock.patch.object(module, work) as worked:
            getattr(module, runner)(self.settings)
        self.assertEqual(sleeper.calls, 1, f"{runner} must return on its first wake after retirement")
        worked.assert_not_called()

    def test_vpn_sharing_poller_returns_without_checking(self) -> None:
        self._assert_returns_without_work(vpn_manager, "run_sharing_revocation_poller", "check_sharing_revocation")

    def test_vpn_self_heal_poller_returns_without_healing(self) -> None:
        self._assert_returns_without_work(vpn_manager, "run_self_heal_poller", "check_and_self_heal")

    def test_smtp_sharing_poller_returns_without_checking(self) -> None:
        self._assert_returns_without_work(smtp_manager, "run_sharing_revocation_poller", "check_sharing_revocation")

    def test_tailnet_sharing_poller_returns_without_checking(self) -> None:
        self._assert_returns_without_work(
            tailnet_service, "run_tailnet_sharing_revocation_poller", "check_tailnet_sharing_revocation"
        )

    def test_network_share_watchdog_returns_without_listing_shares(self) -> None:
        self._assert_returns_without_work(network_share_manager, "run_watchdog_poller", "list_shares")

    def test_outbound_mail_worker_returns_before_touching_storage(self) -> None:
        sleeper = _Sleep()
        with mock.patch.object(smtp_manager.time, "sleep", sleeper), \
                mock.patch.object(smtp_manager._crash_history, "ingest_new_crashes") as crashes, \
                mock.patch.object(smtp_manager._usb_enumeration, "check_usb_enumeration_throttled") as usb, \
                mock.patch.object(smtp_manager, "_load_state") as load_state, \
                mock.patch.object(smtp_manager, "send_digest_if_needed") as digest, \
                mock.patch.object(smtp_manager, "process_outbound_mail_queue") as queue:
            smtp_manager.run_audit_email_digest_poller(self.settings)
        self.assertEqual(sleeper.calls, 1)
        for spy in (crashes, usb, load_state, digest, queue):
            spy.assert_not_called()

    def test_automation_poller_thread_exits_without_running_after_retirement(self) -> None:
        released = threading.Event()
        runs = []

        def run_once(_settings) -> None:
            runs.append(time.monotonic())

        def sleep_until_released(_seconds: float) -> None:
            released.wait(5)

        with mock.patch.dict(os.environ, {"AUTOMATION_POLL_SECONDS": "5"}), \
                mock.patch.object(automation.time, "sleep", sleep_until_released), \
                mock.patch.object(automation, "_run_idle_volume_automation_once", run_once), \
                mock.patch.object(automation, "_run_idle_game_exit_automation_once", run_once), \
                mock.patch.object(automation, "_run_wifi_recovery_automation_once", run_once):
            automation._start_automation_poller(self.settings)
            deadline = time.monotonic() + 5
            while not runs and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(runs, "the poller should run its first pass before retirement")
            drone_api.retire_background_pollers()
            ran_before_wake = len(runs)
            released.set()
            thread = next(t for t in threading.enumerate() if t.name == "drone-automation-poller" and t.is_alive())
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(runs), ran_before_wake, "a retired poller must not run another pass")

    def test_retired_mail_worker_never_opens_a_database_selected_after_retirement(self) -> None:
        """The combined-run failure: env selects database B after the poller was started for A."""
        first = self.root / "a" / "state.sqlite3"
        second = self.root / "b" / "state.sqlite3"
        opened: list[str] = []
        real_open = state_store.open_database

        def spy_open(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        settings = Settings.from_env()
        with mock.patch.dict(os.environ, {"DRONE_STATE_DATABASE_FILE": str(first)}), \
                mock.patch.object(smtp_manager, "DIGEST_POLLER_TICK_SECONDS", 0.05), \
                mock.patch.object(state_store, "open_database", side_effect=spy_open), \
                mock.patch.object(smtp_manager._crash_history, "ingest_new_crashes"), \
                mock.patch.object(smtp_manager._usb_enumeration, "check_usb_enumeration_throttled"), \
                mock.patch.object(smtp_manager, "send_digest_if_needed"), \
                mock.patch.object(smtp_manager, "process_outbound_mail_queue"):
            worker = threading.Thread(
                target=smtp_manager.run_audit_email_digest_poller, args=(settings,), daemon=True
            )
            worker.start()
            deadline = time.monotonic() + 5
            while str(first.resolve()) not in opened and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIn(str(first.resolve()), opened, "the live poller should use its own database")
            drone_api.retire_background_pollers()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            opened.clear()
            with mock.patch.dict(os.environ, {"DRONE_STATE_DATABASE_FILE": str(second)}):
                time.sleep(0.3)
        self.assertEqual(opened, [])
        self.assertFalse(second.parent.exists())

    def test_poller_started_after_retirement_keeps_running(self) -> None:
        drone_api.retire_background_pollers()
        sleeper_calls: list[int] = []

        def sleep(_seconds: float) -> None:
            sleeper_calls.append(1)
            if len(sleeper_calls) >= 3:
                raise StopIteration

        with mock.patch.object(vpn_manager.time, "sleep", sleep), \
                mock.patch.object(vpn_manager, "check_and_self_heal") as heal:
            with self.assertRaises(StopIteration):
                vpn_manager.run_self_heal_poller(self.settings)
        self.assertEqual(heal.call_count, 2)


if __name__ == "__main__":
    unittest.main()
