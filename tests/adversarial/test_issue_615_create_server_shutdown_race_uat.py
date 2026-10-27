"""Issue #615 adversarial UAT: create_server()-started pollers must not check after shutdown.

Expected behavior, from the issue and the generation-retirement contract:

- create_server() binds each forever-loop poller to the generation current in the caller
  before Thread.start(). Retirement after start() returns, including before the worker
  body runs, yields zero checks.
- The same contract holds when create_server() fails after the VPN (and sibling) threads
  start and main()'s finally calls retire_background_pollers().
- On-thread poller_generation() is not the capture point for a production start: gating
  it until after retirement must not let the worker adopt the new generation.
- Generation is snapshotted per starter. A poller started after a retirement is live
  under the new generation.
- The outbound-mail worker is started even when swarm networking is held, and still
  receives a caller-captured generation.
- Explicit generation 0 is a real generation. The generation=None fallback is a test
  hook, not the create_server() path under test here.
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

_QUIET_FLAGS = (
    "_ROM_METADATA_POLLER_STARTED",
    "_ROM_METADATA_WATCHER_STARTED",
    "_GAME_PROCESS_MONITOR_STARTED",
    "_AUTOMATION_POLLER_STARTED",
    "_LOCAL_NETWORK_WORKERS_STARTED",
    "_STREAMDECK_SUPERVISOR_STARTED",
    "_NETWORK_SHARE_BOOT_REPLAY_ATTEMPTED",
    "_NFS_EXPORT_BOOT_REPLAY_ATTEMPTED",
    "_SMTP_BOOTSTRAP_ATTEMPTED",
    "_TAILNET_BOOTSTRAP_ATTEMPTED",
    "_VPN_AUTO_CONNECT_ATTEMPTED",
)

_POLLER_CASES = (
    ("vpn-sharing", vpn_manager, "run_sharing_revocation_poller", "check_sharing_revocation"),
    ("vpn-self-heal", vpn_manager, "run_self_heal_poller", "check_and_self_heal"),
    ("smtp-sharing", smtp_manager, "run_sharing_revocation_poller", "check_sharing_revocation"),
    ("tailnet-sharing", tailnet_service, "run_tailnet_sharing_revocation_poller", "check_tailnet_sharing_revocation"),
    ("network-watchdog", network_share_manager, "run_watchdog_poller", "list_shares"),
)

_THREAD_NAMES = {
    "drone-vpn-sharing-revocation",
    "drone-vpn-self-heal",
    "drone-network-share-watchdog",
    "drone-smtp-sharing-revocation",
    "drone-outbound-mail-worker",
    "drone-tailnet-sharing-revocation",
}

_AUDIT_THREAD = "drone-outbound-mail-worker"


class _RecordingThread:
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


class _DualGate:
    """Stall on-thread poller_generation() and time.sleep independently.

    A production start captures generation in the caller, so the worker blocks on
    sleep. A worker that still recaptures on-thread blocks on the generation gate;
    releasing that gate after retirement lets it adopt the new generation and check.
    """

    def __init__(self) -> None:
        self.generation_gate = threading.Event()
        self.sleep_gate = threading.Event()
        self.sleeps = 0

    def open(self) -> None:
        self.generation_gate.set()
        self.sleep_gate.set()

    def gated_generation(self, real):
        def inner() -> int:
            self.generation_gate.wait(10)
            return real()

        return inner

    def fake_sleep(self, _seconds: float) -> None:
        self.sleeps += 1
        if self.sleeps > 24:
            raise RuntimeError("poller kept sleeping after retirement")
        self.sleep_gate.wait(10)


def _poller_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name in _THREAD_NAMES]


class _CreateServerHarness:
    def __init__(self, test: unittest.TestCase) -> None:
        self.test = test
        drone_api.retire_background_pollers()
        identity.reset_runtime_state_for_tests()
        test.addCleanup(identity.reset_runtime_state_for_tests)
        test.addCleanup(drone_api.retire_background_pollers)
        self._tmp = tempfile.TemporaryDirectory(prefix="issue-615-shutdown-")
        test.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        userdata = root / "userdata"
        seed_mock_userdata(userdata)
        env = {
            "USERDATA_ROOT": str(userdata),
            "ROMS_ROOT": str(userdata / "roms"),
            "BIOS_ROOT": str(userdata / "bios"),
            "SAVES_ROOT": str(userdata / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "issue-615-shutdown-host",
            "HTTPS_PORT": "0",
            "HTTP_ONLY": "1",
            "DRONE_CAST_ENABLED": "0",
            "DRONE_COMPAT_HTTPS_PORTS": "",
            "DRONE_PEER_MTLS_PORT": "0",
            "ROM_METADATA_POLL_SECONDS": "0",
            "DRONE_AUTO_UPDATE_POLL_SECONDS": "0",
            "LOG_DIR": str(root / "logs"),
            "TLS_SELF_SIGNED_DIR": str(userdata / "system" / "certs"),
            "DRONE_APP_USERNAME": "admin",
            "DRONE_APP_PASSWORD": "changeme",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            self.settings = Settings.from_env()

        originals = {}
        for name in _QUIET_FLAGS + _STARTER_FLAGS:
            originals[name] = getattr(drone_api, name)
            setattr(drone_api, name, False)
        for name in _QUIET_FLAGS:
            setattr(drone_api, name, True)
        test.addCleanup(self._restore_flags, originals)

    @staticmethod
    def _restore_flags(originals: dict) -> None:
        for name, value in originals.items():
            setattr(drone_api, name, value)


class PollerStartArgsContractUAT(unittest.TestCase):
    """create_server()'s helper must snapshot generation for every real poller."""

    def test_poller_start_args_binds_current_generation_for_every_production_poller(self) -> None:
        settings = mock.Mock(name="settings")
        snapshot = runtime_state.poller_generation()
        pollers = [
            vpn_manager.run_sharing_revocation_poller,
            vpn_manager.run_self_heal_poller,
            smtp_manager.run_sharing_revocation_poller,
            smtp_manager.run_audit_email_digest_poller,
            tailnet_service.run_tailnet_sharing_revocation_poller,
            network_share_manager.run_watchdog_poller,
        ]
        for poller in pollers:
            with self.subTest(poller=poller.__name__):
                args = drone_api._poller_start_args(poller, settings)
                self.assertEqual(args, (settings, snapshot))

    def test_one_argument_observer_still_starts_without_generation(self) -> None:
        settings = mock.Mock(name="settings")

        def observer(current_settings) -> None:
            return None

        args = drone_api._poller_start_args(observer, settings)
        self.assertEqual(args, (settings,))


class ProductionStartCaptureRaceUAT(unittest.TestCase):
    """Live threads started the way create_server() starts them."""

    def setUp(self) -> None:
        drone_api.retire_background_pollers()
        self.addCleanup(drone_api.retire_background_pollers)
        self.settings = mock.Mock(name="settings")

    def _run_with_dual_gate(self, module, poller_name: str, check_name: str, args: tuple) -> mock.Mock:
        gate = _DualGate()
        self.addCleanup(gate.open)
        check = mock.Mock(return_value=False if check_name != "list_shares" else [])
        extra_patches = []
        if module is vpn_manager and poller_name == "run_self_heal_poller":
            extra_patches.append(mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel", mock.Mock()))
        if module is smtp_manager and poller_name == "run_audit_email_digest_poller":
            check = mock.Mock()
            extra_patches.extend(
                [
                    mock.patch.object(smtp_manager._crash_history, "ingest_new_crashes", check),
                    mock.patch.object(smtp_manager._usb_enumeration, "check_usb_enumeration_throttled"),
                    mock.patch.object(smtp_manager, "_load_state", return_value={}),
                ]
            )
            check_patch = None
        else:
            check_patch = mock.patch.object(module, check_name, check)

        real_generation = module.poller_generation
        with mock.patch.object(module, "poller_generation", gate.gated_generation(real_generation)), \
                mock.patch.object(module.time, "sleep", new=gate.fake_sleep):
            if check_patch is not None:
                check_patch.start()
                self.addCleanup(check_patch.stop)
            for patcher in extra_patches:
                patcher.start()
                self.addCleanup(patcher.stop)
            thread = threading.Thread(
                target=getattr(module, poller_name),
                args=args,
                name=f"issue-615-{poller_name}",
                daemon=True,
            )
            thread.start()
            drone_api.retire_background_pollers()
            gate.open()
            thread.join(10)
        self.assertFalse(thread.is_alive(), f"{poller_name} did not exit after retirement")
        self.assertGreaterEqual(gate.sleeps, 1, f"{poller_name} never entered its loop")
        return check

    def test_each_production_poller_start_then_retire_before_on_thread_capture_never_checks(self) -> None:
        for label, module, poller_name, check_name in _POLLER_CASES:
            with self.subTest(poller=label):
                poller = getattr(module, poller_name)
                args = drone_api._poller_start_args(poller, self.settings)
                self.assertEqual(len(args), 2)
                check = self._run_with_dual_gate(module, poller_name, check_name, args)
                self.assertEqual(check.call_count, 0, f"{label} ran a check after retirement")

    def test_audit_mail_worker_start_then_retire_before_on_thread_capture_never_ingests(self) -> None:
        poller = smtp_manager.run_audit_email_digest_poller
        args = drone_api._poller_start_args(poller, self.settings)
        self.assertEqual(len(args), 2)
        check = self._run_with_dual_gate(
            smtp_manager, "run_audit_email_digest_poller", "ingest_new_crashes", args,
        )
        self.assertEqual(check.call_count, 0, "outbound-mail worker ingested after retirement")

    def test_retirement_between_snapshot_and_thread_start_never_checks(self) -> None:
        args = drone_api._poller_start_args(vpn_manager.run_sharing_revocation_poller, self.settings)
        drone_api.retire_background_pollers()
        check = mock.Mock(return_value=False)
        sleep = mock.Mock(side_effect=[None, RuntimeError("poller ignored retirement")])
        with mock.patch.object(vpn_manager, "check_sharing_revocation", check), \
                mock.patch.object(vpn_manager.time, "sleep", new=sleep), \
                mock.patch.object(
                    vpn_manager,
                    "poller_generation",
                    side_effect=AssertionError("poller recaptured generation on-thread"),
                ):
            vpn_manager.run_sharing_revocation_poller(*args)
        self.assertEqual(check.call_count, 0)
        self.assertEqual(sleep.call_count, 1)

    def test_fresh_snapshot_after_retirement_still_checks_until_its_own_generation_retires(self) -> None:
        drone_api.retire_background_pollers()
        args = drone_api._poller_start_args(vpn_manager.run_sharing_revocation_poller, self.settings)
        check = mock.Mock(return_value=False)
        sleeps = {"n": 0}

        def fake_sleep(_seconds: float) -> None:
            sleeps["n"] += 1
            if sleeps["n"] == 3:
                drone_api.retire_background_pollers()
            if sleeps["n"] > 5:
                raise RuntimeError("fresh poller did not observe its own retirement")

        with mock.patch.object(vpn_manager, "check_sharing_revocation", check), \
                mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep):
            vpn_manager.run_sharing_revocation_poller(*args)
        self.assertEqual(check.call_count, 2)
        self.assertEqual(sleeps["n"], 3)


class CreateServerShutdownRaceUAT(unittest.TestCase):
    """create_server() itself: failure after start, then main()'s finally."""

    def setUp(self) -> None:
        self.harness = _CreateServerHarness(self)

    def _start_patch(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def _patch_poller_io(self, gate: _DualGate) -> dict[str, mock.Mock]:
        checks = {
            "vpn-sharing": mock.Mock(return_value=False),
            "vpn-self-heal": mock.Mock(return_value=None),
            "vpn-rgs": mock.Mock(),
            "smtp-sharing": mock.Mock(return_value=False),
            "smtp-ingest": mock.Mock(),
            "tailnet-sharing": mock.Mock(return_value=False),
            "watchdog": mock.Mock(return_value=[]),
        }
        for module in (vpn_manager, smtp_manager, tailnet_service, network_share_manager):
            real_generation = module.poller_generation
            self._start_patch(
                mock.patch.object(module, "poller_generation", gate.gated_generation(real_generation))
            )
            self._start_patch(mock.patch.object(module.time, "sleep", new=gate.fake_sleep))
        self._start_patch(mock.patch.object(vpn_manager, "check_sharing_revocation", checks["vpn-sharing"]))
        self._start_patch(mock.patch.object(vpn_manager, "check_and_self_heal", checks["vpn-self-heal"]))
        self._start_patch(mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel", checks["vpn-rgs"]))
        self._start_patch(mock.patch.object(smtp_manager, "check_sharing_revocation", checks["smtp-sharing"]))
        self._start_patch(
            mock.patch.object(smtp_manager._crash_history, "ingest_new_crashes", checks["smtp-ingest"])
        )
        self._start_patch(mock.patch.object(smtp_manager._usb_enumeration, "check_usb_enumeration_throttled"))
        self._start_patch(mock.patch.object(smtp_manager, "_load_state", return_value={}))
        self._start_patch(
            mock.patch.object(tailnet_service, "check_tailnet_sharing_revocation", checks["tailnet-sharing"])
        )
        self._start_patch(mock.patch.object(network_share_manager, "list_shares", checks["watchdog"]))
        return checks

    def _join_pollers(self) -> None:
        for _ in range(50):
            alive = [thread for thread in _poller_threads() if thread.is_alive()]
            if not alive:
                return
            for thread in alive:
                thread.join(0.1)
        alive = [thread.name for thread in _poller_threads() if thread.is_alive()]
        self.assertFalse(alive, f"poller threads still running after retirement: {alive}")

    def test_create_server_failure_then_main_finally_stops_every_poller_before_any_check(self) -> None:
        gate = _DualGate()
        self.addCleanup(gate.open)
        checks = self._patch_poller_io(gate)
        with mock.patch.object(drone_api._identity, "swarm_networking_allowed", return_value=True), \
                mock.patch.object(drone_api, "_start_local_network_workers"), \
                mock.patch.object(drone_api, "_get_streamdeck_integration"), \
                mock.patch.object(drone_api, "_start_multiplayer_worker", create=True), \
                mock.patch.object(drone_api, "_build_handler", side_effect=RuntimeError("listener setup failed")):
            with self.assertRaisesRegex(RuntimeError, "listener setup failed"):
                drone_api.create_server(self.harness.settings)

        started = {thread.name for thread in _poller_threads()}
        self.assertTrue(
            _THREAD_NAMES.issubset(started),
            f"create_server did not start every sharing poller before failing; saw {started}",
        )
        # main()'s finally: retire even though create_server never returned a server.
        drone_api.retire_background_pollers()
        gate.open()
        self._join_pollers()
        self.assertGreaterEqual(gate.sleeps, 1, "no create_server poller entered its loop before retirement")

        self.assertEqual(checks["vpn-sharing"].call_count, 0)
        self.assertEqual(checks["vpn-self-heal"].call_count, 0)
        self.assertEqual(checks["vpn-rgs"].call_count, 0)
        self.assertEqual(checks["smtp-sharing"].call_count, 0)
        self.assertEqual(checks["smtp-ingest"].call_count, 0)
        self.assertEqual(checks["tailnet-sharing"].call_count, 0)
        self.assertEqual(checks["watchdog"].call_count, 0)

    def test_create_server_passes_generation_before_thread_start_for_held_swarm_mail_worker(self) -> None:
        _RecordingThread.created = []
        with mock.patch.object(drone_api, "Thread", _RecordingThread), \
                mock.patch.object(drone_api._identity, "swarm_networking_allowed", return_value=False), \
                mock.patch.object(drone_api, "_start_local_network_workers"), \
                mock.patch.object(drone_api, "_get_streamdeck_integration"), \
                mock.patch.object(drone_api, "_start_multiplayer_worker", create=True), \
                mock.patch.object(drone_api, "_build_handler", side_effect=RuntimeError("listener setup failed")):
            snapshot = runtime_state.poller_generation()
            with self.assertRaisesRegex(RuntimeError, "listener setup failed"):
                drone_api.create_server(self.harness.settings)

        by_name = {thread.name: thread for thread in _RecordingThread.created}
        self.assertNotIn("drone-vpn-sharing-revocation", by_name)
        mail = by_name.get(_AUDIT_THREAD)
        self.assertIsNotNone(mail, "held-swarm create_server must still start the outbound-mail worker")
        self.assertEqual(len(mail.args), 2)
        self.assertEqual(mail.args[1], snapshot)
        self.assertTrue(mail.started)

    def test_restart_after_retire_background_pollers_binds_the_new_generation(self) -> None:
        _RecordingThread.created = []
        patches = [
            mock.patch.object(drone_api, "Thread", _RecordingThread),
            mock.patch.object(drone_api._identity, "swarm_networking_allowed", return_value=True),
            mock.patch.object(drone_api, "_start_local_network_workers"),
            mock.patch.object(drone_api, "_get_streamdeck_integration"),
            mock.patch.object(drone_api, "_start_multiplayer_worker"),
            mock.patch.object(drone_api, "_build_handler", side_effect=RuntimeError("listener setup failed")),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        first = runtime_state.poller_generation()
        with self.assertRaisesRegex(RuntimeError, "listener setup failed"):
            drone_api.create_server(self.harness.settings)
        first_threads = {
            thread.name: thread.args[1]
            for thread in _RecordingThread.created
            if thread.name in _THREAD_NAMES and len(thread.args) == 2
        }
        self.assertEqual(set(first_threads), _THREAD_NAMES)
        self.assertTrue(all(generation == first for generation in first_threads.values()))

        drone_api.retire_background_pollers()
        _RecordingThread.created = []
        second = runtime_state.poller_generation()
        self.assertNotEqual(second, first)
        with self.assertRaisesRegex(RuntimeError, "listener setup failed"):
            drone_api.create_server(self.harness.settings)
        second_threads = {
            thread.name: thread.args[1]
            for thread in _RecordingThread.created
            if thread.name in _THREAD_NAMES and len(thread.args) == 2
        }
        self.assertEqual(set(second_threads), _THREAD_NAMES)
        self.assertTrue(all(generation == second for generation in second_threads.values()))


if __name__ == "__main__":
    unittest.main()
