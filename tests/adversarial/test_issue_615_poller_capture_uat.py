"""Issue #615 UAT: a forever-loop poller must not check after retirement when retirement lands
before its thread has run.

Expected behavior, derived from the issue and the generation-retirement contract in CLAUDE.md:

- The generation a poller is retired against is the one current when the poller is *started*
  (create_server), not whatever is current when its thread first runs.
- A poller whose thread starts after retire_pollers() but was started before it returns before
  any check.
- A poller started after a retirement (a fresh generation) is not retired by that old retirement.
- Generation 0 (the value of a fresh process) is a real generation, not "unset".
- create_server() hands every sharing/revocation/self-heal/watchdog/mail poller the generation
  captured in the caller, so retire_background_pollers() after create_server() reaches them.

The existing #586 evidence suite (test_issue_586_out_of_scope_poller_capture_race_uat.py) drives
the generation=None fallback on the thread; this suite pins the production wiring and ordering.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import app.device.network_share_manager as network_share_manager
import app.device.smtp_manager as smtp_manager
import app.device.tailnet_service as tailnet_service
import app.device.vpn_manager as vpn_manager
import app.drone_api as drone_api
from app.common import identity_provisioning as identity
from app.common import runtime_state
from app.common.settings import Settings
from app.mock_data import seed_mock_userdata


_STARTER_FLAGS = (
    "_VPN_SHARING_POLLER_STARTED",
    "_VPN_SELF_HEAL_POLLER_STARTED",
    "_NETWORK_SHARE_WATCHDOG_STARTED",
    "_SMTP_SHARING_POLLER_STARTED",
    "_AUDIT_EMAIL_POLLER_STARTED",
    "_TAILNET_SHARING_POLLER_STARTED",
)

# Thread name -> the poller entry point create_server() starts on that thread.
_POLLER_TARGETS = {
    "drone-vpn-sharing-revocation": vpn_manager.run_sharing_revocation_poller,
    "drone-vpn-self-heal": vpn_manager.run_self_heal_poller,
    "drone-network-share-watchdog": network_share_manager.run_watchdog_poller,
    "drone-smtp-sharing-revocation": smtp_manager.run_sharing_revocation_poller,
    "drone-outbound-mail-worker": smtp_manager.run_audit_email_digest_poller,
    "drone-tailnet-sharing-revocation": tailnet_service.run_tailnet_sharing_revocation_poller,
}


class _RecordingThread:
    """Stands in for threading.Thread inside create_server: records, never runs."""

    created: list["_RecordingThread"] = []

    def __init__(self, target=None, args=(), name=None, daemon=None, **_kwargs) -> None:
        self.target = target
        self.args = tuple(args)
        self.name = name
        self.daemon = daemon
        self.started = False
        _RecordingThread.created.append(self)

    def start(self) -> None:
        self.started = True


class _BoundedSleep:
    """Replaces time.sleep for a poller run on this thread. Retires after ``retire_on``, and
    raises after ``limit`` sleeps so a poller that ignores retirement fails instead of hanging."""

    def __init__(self, retire_on: int | None, limit: int = 5) -> None:
        self.calls = 0
        self.retire_on = retire_on
        self.limit = limit

    def __call__(self, _seconds: float) -> None:
        self.calls += 1
        if self.retire_on is not None and self.calls == self.retire_on:
            runtime_state.retire_pollers()
        if self.calls > self.limit:
            raise RuntimeError("poller kept sleeping past its bound; retirement was not observed")


class PollerCaptureWiringUAT(unittest.TestCase):
    """create_server() wiring: generation is captured before the thread object exists."""

    def setUp(self) -> None:
        drone_api.retire_background_pollers()
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self._tmp = tempfile.TemporaryDirectory(prefix="issue-615-wiring-")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.userdata = root / "userdata"
        seed_mock_userdata(self.userdata)
        env = {
            "USERDATA_ROOT": str(self.userdata),
            "ROMS_ROOT": str(self.userdata / "roms"),
            "BIOS_ROOT": str(self.userdata / "bios"),
            "SAVES_ROOT": str(self.userdata / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "issue-615-host",
            "HTTPS_PORT": "0",
            "HTTP_ONLY": "1",
            "DRONE_CAST_ENABLED": "0",
            "ROM_METADATA_POLL_SECONDS": "0",
            "DRONE_AUTO_UPDATE_POLL_SECONDS": "0",
            "LOG_DIR": str(root / "logs"),
            "TLS_SELF_SIGNED_DIR": str(self.userdata / "system" / "certs"),
            "DRONE_APP_USERNAME": "admin",
            "DRONE_APP_PASSWORD": "changeme",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            self.settings = Settings.from_env()
        self.addCleanup(drone_api.retire_background_pollers)

        # Quiet every starter that is not a sharing/revocation/self-heal/watchdog/mail poller, and
        # latch the six under test off so create_server starts them.
        originals = {}
        for name in ("_ROM_METADATA_POLLER_STARTED", "_ROM_METADATA_WATCHER_STARTED",
                     "_GAME_PROCESS_MONITOR_STARTED", "_AUTOMATION_POLLER_STARTED",
                     "_LOCAL_NETWORK_WORKERS_STARTED", "_STREAMDECK_SUPERVISOR_STARTED",
                     "_NETWORK_SHARE_BOOT_REPLAY_ATTEMPTED", "_NFS_EXPORT_BOOT_REPLAY_ATTEMPTED",
                     "_SMTP_BOOTSTRAP_ATTEMPTED", "_TAILNET_BOOTSTRAP_ATTEMPTED",
                     "_VPN_AUTO_CONNECT_ATTEMPTED") + _STARTER_FLAGS:
            originals[name] = getattr(drone_api, name)
            setattr(drone_api, name, False)
        for name in ("_ROM_METADATA_POLLER_STARTED", "_ROM_METADATA_WATCHER_STARTED",
                     "_GAME_PROCESS_MONITOR_STARTED", "_AUTOMATION_POLLER_STARTED",
                     "_NETWORK_SHARE_BOOT_REPLAY_ATTEMPTED", "_NFS_EXPORT_BOOT_REPLAY_ATTEMPTED",
                     "_SMTP_BOOTSTRAP_ATTEMPTED", "_TAILNET_BOOTSTRAP_ATTEMPTED",
                     "_VPN_AUTO_CONNECT_ATTEMPTED", "_STREAMDECK_SUPERVISOR_STARTED"):
            setattr(drone_api, name, True)
        self.addCleanup(self._restore_flags, originals)

        _RecordingThread.created = []
        patches = [
            mock.patch.object(drone_api, "Thread", _RecordingThread),
            mock.patch.object(drone_api._identity, "swarm_networking_allowed", return_value=True),
            mock.patch.object(drone_api, "_start_local_network_workers", create=True),
            mock.patch.object(drone_api, "_get_streamdeck_integration", create=True),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _restore_flags(originals: dict) -> None:
        for name, value in originals.items():
            setattr(drone_api, name, value)

    def _create_server_recording_pollers(self) -> dict[str, _RecordingThread]:
        server = drone_api.create_server(self.settings)
        self.addCleanup(server.server_close)
        by_name = {thread.name: thread for thread in _RecordingThread.created if thread.name in _POLLER_TARGETS}
        return by_name

    def test_create_server_passes_pre_retirement_generation_for_every_starter(self) -> None:
        generation_before_start = runtime_state.poller_generation()
        threads = self._create_server_recording_pollers()

        self.assertEqual(set(threads), set(_POLLER_TARGETS), "create_server did not start every sharing/mail poller")
        for name, thread in threads.items():
            with self.subTest(thread=name):
                self.assertEqual(len(thread.args), 2, "poller must receive the caller-captured generation")
                self.assertIsInstance(thread.args[1], int)
                self.assertEqual(thread.args[1], generation_before_start)

    def test_retirement_after_create_server_stops_every_starter_before_any_check(self) -> None:
        threads = self._create_server_recording_pollers()
        runtime_state.retire_pollers()

        for name, thread in threads.items():
            with self.subTest(thread=name):
                sleep = _BoundedSleep(retire_on=None)
                with mock.patch("time.sleep", new=sleep):
                    # Run the real poller body on this thread, after retirement, as the started thread would.
                    _POLLER_TARGETS[name](*thread.args)
                self.assertEqual(sleep.calls, 1, "a retired poller must return on its first wake, before any check")


class PollerCaptureOrderingUAT(unittest.TestCase):
    """Real threads: the generation is captured by the caller, retirement lands before the thread runs."""

    def setUp(self) -> None:
        drone_api.retire_background_pollers()
        self.addCleanup(drone_api.retire_background_pollers)
        self.settings = mock.Mock(name="settings")
        self.check = mock.Mock(return_value=False)
        self.poller_generation_on_thread = mock.Mock(side_effect=AssertionError("poller read its generation on-thread"))

    def _run_thread(self, sleep: _BoundedSleep, args: tuple) -> threading.Thread:
        with mock.patch.object(vpn_manager, "poller_generation", self.poller_generation_on_thread), \
                mock.patch.object(vpn_manager, "check_sharing_revocation", self.check), \
                mock.patch("time.sleep", new=sleep):
            thread = threading.Thread(target=vpn_manager.run_sharing_revocation_poller, args=args, daemon=True)
            thread.start()
            thread.join(10)
        return thread

    def test_caller_captured_generation_retired_before_thread_runs_never_checks(self) -> None:
        # Mirrors create_server(): the generation is read here, in the caller, before the thread exists.
        captured = runtime_state.poller_generation()
        thread_object = threading.Thread(target=vpn_manager.run_sharing_revocation_poller, args=(self.settings, captured), daemon=True)
        runtime_state.retire_pollers()

        sleep = _BoundedSleep(retire_on=None)
        with mock.patch.object(vpn_manager, "poller_generation", self.poller_generation_on_thread), \
                mock.patch.object(vpn_manager, "check_sharing_revocation", self.check), \
                mock.patch("time.sleep", new=sleep):
            thread_object.start()
            thread_object.join(10)

        self.assertFalse(thread_object.is_alive(), "poller thread must exit once its captured generation is retired")
        self.assertEqual(self.check.call_count, 0, "poller ran a revocation check after retirement")
        self.poller_generation_on_thread.assert_not_called()

    def test_generation_zero_is_a_real_generation_not_unset(self) -> None:
        # A fresh process starts at generation 0, so an explicit 0 must be honored rather than
        # treated as "unset" and recaptured on-thread. The counter is only ever moved forward
        # (never reset to 0), so no earlier poller can be made current again.
        runtime_state.retire_pollers()
        self.assertGreater(runtime_state.poller_generation(), 0)
        sleep = _BoundedSleep(retire_on=None)
        self._run_thread(sleep, (self.settings, 0))

        self.assertEqual(self.check.call_count, 0, "explicit generation 0 was recaptured and not retired")
        self.assertEqual(sleep.calls, 1)

    def test_poller_started_after_retirement_is_not_retired_by_the_old_one(self) -> None:
        runtime_state.retire_pollers()
        fresh = runtime_state.poller_generation()

        # Two live ticks, then retirement on the third wake: the fresh poller checks exactly twice.
        sleep = _BoundedSleep(retire_on=3)
        thread = self._run_thread(sleep, (self.settings, fresh))

        self.assertFalse(thread.is_alive())
        self.assertEqual(self.check.call_count, 2, "a fresh poller must keep checking until its own generation is retired")
        self.assertEqual(sleep.calls, 3)

    def test_retirement_during_live_ticks_still_stops_the_poller(self) -> None:
        captured = runtime_state.poller_generation()
        # Retire on the second wake, after one live tick ran.
        sleep = _BoundedSleep(retire_on=2)
        thread = self._run_thread(sleep, (self.settings, captured))

        self.assertFalse(thread.is_alive())
        self.assertEqual(self.check.call_count, 1)


if __name__ == "__main__":
    unittest.main()
