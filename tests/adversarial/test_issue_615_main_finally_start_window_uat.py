"""Issue #615 round-2 UAT: shutdown after start must retire a poller that has not run yet.

Expected behavior, from the issue and the generation-retirement contract:

- create_server() starts forever-loop pollers and returns (or fails later). The
  generation those workers are retired against is the one current in the starter
  *before* Thread.start(), not the one current when the OS thread first runs.
- main()'s finally calls retire_background_pollers() after a create_server()
  failure. That path must yield zero checks even if on-thread poller_generation()
  is gated until after retirement.
- Retirement that lands between Thread.__init__ and Thread.start() is still a
  started-then-retired worker: zero checks.
- Work the worker does before its loop (interval env parse) must not recapture
  generation. Gating that parse until after retirement still yields zero checks.
- The production starter helper must keep binding generation for real pollers
  when inspect.signature fails. Silently falling back to (settings,) reopens the
  original race.
- create_server() must pass the helper's snapshot as Thread args for the same
  callable it uses as the thread target.
"""

from __future__ import annotations

import ast
import inspect
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

_PRODUCTION_POLLERS = (
    vpn_manager.run_sharing_revocation_poller,
    vpn_manager.run_self_heal_poller,
    smtp_manager.run_sharing_revocation_poller,
    smtp_manager.run_audit_email_digest_poller,
    tailnet_service.run_tailnet_sharing_revocation_poller,
    network_share_manager.run_watchdog_poller,
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

_INTERVAL_POLLERS = (
    ("vpn-sharing", vpn_manager, "run_sharing_revocation_poller", "check_sharing_revocation"),
    ("smtp-sharing", smtp_manager, "run_sharing_revocation_poller", "check_sharing_revocation"),
    ("tailnet-sharing", tailnet_service, "run_tailnet_sharing_revocation_poller", "check_tailnet_sharing_revocation"),
)


class _DualGate:
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


class _RetireThenStart(threading.Thread):
    """Production Thread.start() window: retirement can land after __init__ and before run."""

    created: list["_RetireThenStart"] = []

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.bound_args = tuple(self._args)  # type: ignore[attr-defined]
        _RetireThenStart.created.append(self)

    def start(self) -> None:
        runtime_state.retire_pollers()
        super().start()


class _CreateServerHarness:
    def __init__(self, test: unittest.TestCase) -> None:
        self.test = test
        drone_api.retire_background_pollers()
        identity.reset_runtime_state_for_tests()
        test.addCleanup(identity.reset_runtime_state_for_tests)
        test.addCleanup(drone_api.retire_background_pollers)
        self._tmp = tempfile.TemporaryDirectory(prefix="issue-615-main-")
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
            "DRONE_DEVICE_ID": "issue-615-main-host",
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


def _poller_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name in _THREAD_NAMES]


def _start_patch(test: unittest.TestCase, patcher):
    patcher.start()
    test.addCleanup(patcher.stop)
    return patcher


def _patch_poller_io(test: unittest.TestCase, gate: _DualGate) -> dict[str, mock.Mock]:
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
        _start_patch(
            test,
            mock.patch.object(module, "poller_generation", gate.gated_generation(real_generation)),
        )
        _start_patch(test, mock.patch.object(module.time, "sleep", new=gate.fake_sleep))
    _start_patch(test, mock.patch.object(vpn_manager, "check_sharing_revocation", checks["vpn-sharing"]))
    _start_patch(test, mock.patch.object(vpn_manager, "check_and_self_heal", checks["vpn-self-heal"]))
    _start_patch(test, mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel", checks["vpn-rgs"]))
    _start_patch(test, mock.patch.object(smtp_manager, "check_sharing_revocation", checks["smtp-sharing"]))
    _start_patch(test, mock.patch.object(smtp_manager._crash_history, "ingest_new_crashes", checks["smtp-ingest"]))
    _start_patch(test, mock.patch.object(smtp_manager._usb_enumeration, "check_usb_enumeration_throttled"))
    _start_patch(test, mock.patch.object(smtp_manager, "_load_state", return_value={}))
    _start_patch(
        test,
        mock.patch.object(tailnet_service, "check_tailnet_sharing_revocation", checks["tailnet-sharing"]),
    )
    _start_patch(test, mock.patch.object(network_share_manager, "list_shares", checks["watchdog"]))
    return checks


def _join_pollers(test: unittest.TestCase) -> None:
    for _ in range(50):
        alive = [thread for thread in _poller_threads() if thread.is_alive()]
        if not alive:
            return
        for thread in alive:
            thread.join(0.1)
    alive = [thread.name for thread in _poller_threads() if thread.is_alive()]
    test.assertFalse(alive, f"poller threads still running after retirement: {alive}")


def _assert_zero_checks(test: unittest.TestCase, checks: dict[str, mock.Mock]) -> None:
    test.assertEqual(checks["vpn-sharing"].call_count, 0)
    test.assertEqual(checks["vpn-self-heal"].call_count, 0)
    test.assertEqual(checks["vpn-rgs"].call_count, 0)
    test.assertEqual(checks["smtp-sharing"].call_count, 0)
    test.assertEqual(checks["smtp-ingest"].call_count, 0)
    test.assertEqual(checks["tailnet-sharing"].call_count, 0)
    test.assertEqual(checks["watchdog"].call_count, 0)


class MainFinallyCaptureRaceUAT(unittest.TestCase):
    """The issue's production unwind: create_server fails, main()'s finally retires."""

    def setUp(self) -> None:
        self.harness = _CreateServerHarness(self)

    def test_main_finally_after_create_server_failure_never_checks(self) -> None:
        gate = _DualGate()
        self.addCleanup(gate.open)
        checks = _patch_poller_io(self, gate)
        with mock.patch.object(drone_api.Settings, "from_env", return_value=self.harness.settings), \
                mock.patch.object(drone_api, "_configure_rotating_logs"), \
                mock.patch.object(
                    drone_api._identity,
                    "ensure_identity",
                    return_value=mock.Mock(settings=self.harness.settings),
                ), \
                mock.patch.object(drone_api._identity, "swarm_networking_allowed", return_value=True), \
                mock.patch.object(drone_api, "_start_local_network_workers"), \
                mock.patch.object(drone_api, "stop_local_network_workers"), \
                mock.patch.object(drone_api, "_get_streamdeck_integration"), \
                mock.patch.object(drone_api, "_start_multiplayer_worker", create=True), \
                mock.patch.object(drone_api, "_start_drone_auto_update_poller"), \
                mock.patch.object(drone_api, "_build_handler", side_effect=RuntimeError("listener setup failed")):
            with self.assertRaisesRegex(RuntimeError, "listener setup failed"):
                drone_api.main()

        started = {thread.name for thread in _poller_threads()}
        # main()'s finally already retired; threads may have exited if they captured
        # the starter snapshot. Either they are still gated on sleep/generation, or
        # they already returned. Both are fine as long as checks stayed at zero.
        gate.open()
        _join_pollers(self)
        self.assertGreaterEqual(
            gate.sleeps + sum(1 for name in _THREAD_NAMES if name in started),
            1,
            "create_server via main() never started a sharing poller before failing",
        )
        _assert_zero_checks(self, checks)


class ThreadStartWindowUAT(unittest.TestCase):
    """Retirement between Thread construction and OS start is still a retired worker."""

    def setUp(self) -> None:
        self.harness = _CreateServerHarness(self)

    def test_retire_inside_thread_start_before_run_never_checks(self) -> None:
        gate = _DualGate()
        self.addCleanup(gate.open)
        checks = _patch_poller_io(self, gate)
        _RetireThenStart.created = []
        with mock.patch.object(drone_api, "Thread", _RetireThenStart), \
                mock.patch.object(drone_api._identity, "swarm_networking_allowed", return_value=True), \
                mock.patch.object(drone_api, "_start_local_network_workers"), \
                mock.patch.object(drone_api, "_get_streamdeck_integration"), \
                mock.patch.object(drone_api, "_start_multiplayer_worker", create=True), \
                mock.patch.object(drone_api, "_build_handler", side_effect=RuntimeError("listener setup failed")):
            with self.assertRaisesRegex(RuntimeError, "listener setup failed"):
                drone_api.create_server(self.harness.settings)

        started = {thread.name for thread in _RetireThenStart.created if thread.name in _THREAD_NAMES}
        self.assertTrue(_THREAD_NAMES.issubset(started), f"missing poller threads: {started}")
        for thread in _RetireThenStart.created:
            if thread.name in _THREAD_NAMES:
                self.assertEqual(len(thread.bound_args), 2, f"{thread.name} was started without a generation snapshot")
                self.assertIsInstance(thread.bound_args[1], int)

        gate.open()
        _join_pollers(self)
        _assert_zero_checks(self, checks)

    def test_each_sibling_retire_between_init_and_start_never_checks(self) -> None:
        settings = mock.Mock(name="settings")
        for label, module, poller_name, check_name in _POLLER_CASES:
            with self.subTest(poller=label):
                drone_api.retire_background_pollers()
                poller = getattr(module, poller_name)
                args = drone_api._poller_start_args(poller, settings)
                self.assertEqual(len(args), 2)
                gate = _DualGate()
                self.addCleanup(gate.open)
                check = mock.Mock(return_value=False if check_name != "list_shares" else [])
                extra = []
                if module is vpn_manager and poller_name == "run_self_heal_poller":
                    extra.append(mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel", mock.Mock()))
                real_generation = module.poller_generation
                with mock.patch.object(module, "poller_generation", gate.gated_generation(real_generation)), \
                        mock.patch.object(module.time, "sleep", new=gate.fake_sleep), \
                        mock.patch.object(module, check_name, check):
                    for patcher in extra:
                        patcher.start()
                        self.addCleanup(patcher.stop)
                    thread = threading.Thread(target=poller, args=args, daemon=True, name=f"issue-615-window-{label}")
                    runtime_state.retire_pollers()
                    thread.start()
                    gate.open()
                    thread.join(10)
                self.assertFalse(thread.is_alive(), f"{label} did not exit after retirement")
                self.assertGreaterEqual(gate.sleeps, 1, f"{label} never entered its loop")
                self.assertEqual(check.call_count, 0, f"{label} ran a check after retirement between init and start")


class PreLoopCaptureUAT(unittest.TestCase):
    """On-thread work before the forever-loop must not recapture generation."""

    def setUp(self) -> None:
        drone_api.retire_background_pollers()
        self.addCleanup(drone_api.retire_background_pollers)
        self.settings = mock.Mock(name="settings")

    def test_retirement_during_interval_parse_never_checks(self) -> None:
        for label, module, poller_name, check_name in _INTERVAL_POLLERS:
            with self.subTest(poller=label):
                drone_api.retire_background_pollers()
                poller = getattr(module, poller_name)
                args = drone_api._poller_start_args(poller, self.settings)
                self.assertEqual(len(args), 2)
                parsed = threading.Event()
                release_parse = threading.Event()
                self.addCleanup(release_parse.set)
                check = mock.Mock(return_value=False)
                sleeps: list[float] = []

                def gated_interval(*_a, **_k) -> float:
                    parsed.set()
                    self.assertTrue(release_parse.wait(10), f"{label} interval parse was not released")
                    return 0.0

                def fake_sleep(seconds: float) -> None:
                    sleeps.append(seconds)
                    if len(sleeps) > 6:
                        raise RuntimeError(f"{label} kept sleeping after retirement")

                with mock.patch.object(module, "poller_interval_from_env", gated_interval), \
                        mock.patch.object(module, check_name, check), \
                        mock.patch.object(module.time, "sleep", new=fake_sleep), \
                        mock.patch.object(
                            module,
                            "poller_generation",
                            side_effect=AssertionError(f"{label} recaptured generation during interval parse"),
                        ):
                    thread = threading.Thread(target=poller, args=args, daemon=True, name=f"issue-615-interval-{label}")
                    thread.start()
                    self.assertTrue(parsed.wait(10), f"{label} never reached interval parse")
                    runtime_state.retire_pollers()
                    release_parse.set()
                    thread.join(10)
                self.assertFalse(thread.is_alive(), f"{label} did not exit after retirement during interval parse")
                self.assertGreaterEqual(len(sleeps), 1, f"{label} never entered its loop")
                self.assertEqual(check.call_count, 0, f"{label} checked after retirement during interval parse")


class HelperErrorPathUAT(unittest.TestCase):
    """The starter helper cannot silently reopen the on-thread capture race."""

    def test_inspect_signature_failure_still_binds_generation_for_production_pollers(self) -> None:
        settings = mock.Mock(name="settings")
        snapshot = runtime_state.poller_generation()
        with mock.patch.object(drone_api.inspect, "signature", side_effect=ValueError("no signature")):
            for poller in _PRODUCTION_POLLERS:
                with self.subTest(poller=poller.__name__):
                    args = drone_api._poller_start_args(poller, settings)
                    self.assertEqual(
                        args,
                        (settings, snapshot),
                        "inspect.signature failure dropped the caller snapshot and reopened the #615 race",
                    )

    def test_one_argument_observer_still_starts_without_generation(self) -> None:
        settings = mock.Mock(name="settings")

        def observer(current_settings) -> None:
            return None

        args = drone_api._poller_start_args(observer, settings)
        self.assertEqual(args, (settings,))

    def test_production_poller_second_parameter_is_positional_generation(self) -> None:
        for poller in _PRODUCTION_POLLERS:
            with self.subTest(poller=poller.__name__):
                params = list(inspect.signature(poller).parameters.values())
                self.assertGreaterEqual(len(params), 2, "production poller must accept a caller-captured generation")
                generation = params[1]
                self.assertEqual(generation.name, "generation")
                self.assertIn(
                    generation.kind,
                    (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD),
                    "create_server passes generation positionally in Thread args",
                )

    def test_create_server_passes_helper_snapshot_to_the_same_thread_target(self) -> None:
        source = Path(drone_api.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        create_server = None
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "create_server":
                create_server = node
                break
        self.assertIsNotNone(create_server, "create_server() is missing")

        started: dict[str, tuple[str, str]] = {}
        for node in ast.walk(create_server):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_thread = (isinstance(func, ast.Name) and func.id == "Thread") or (
                isinstance(func, ast.Attribute) and func.attr == "Thread"
            )
            if not is_thread:
                continue
            keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            if "name" not in keywords or "target" not in keywords or "args" not in keywords:
                continue
            name_node = keywords["name"]
            if not isinstance(name_node, ast.Constant) or not isinstance(name_node.value, str):
                continue
            name = name_node.value
            if name not in _THREAD_NAMES:
                continue
            target_src = ast.unparse(keywords["target"])
            args_node = keywords["args"]
            self.assertIsInstance(args_node, ast.Call, f"{name} Thread args is not a call")
            helper = args_node.func
            helper_name = helper.id if isinstance(helper, ast.Name) else getattr(helper, "attr", "")
            self.assertEqual(helper_name, "_poller_start_args", f"{name} does not bind generation via _poller_start_args")
            self.assertGreaterEqual(len(args_node.args), 1, f"{name} helper is missing the poller callable")
            helper_target = ast.unparse(args_node.args[0])
            self.assertEqual(
                helper_target,
                target_src,
                f"{name} inspects a different callable than the thread target",
            )
            started[name] = (target_src, helper_target)

        self.assertEqual(set(started), _THREAD_NAMES, f"create_server Thread names missing { _THREAD_NAMES - set(started) }")


class InspectFailureLiveRaceUAT(unittest.TestCase):
    """If the helper drops generation, the original capture race is back on live threads."""

    def setUp(self) -> None:
        self.harness = _CreateServerHarness(self)

    def test_create_server_still_binds_generation_when_inspect_cannot_read_signature(self) -> None:
        gate = _DualGate()
        self.addCleanup(gate.open)
        checks = _patch_poller_io(self, gate)
        with mock.patch.object(drone_api.inspect, "signature", side_effect=ValueError("no signature")), \
                mock.patch.object(drone_api._identity, "swarm_networking_allowed", return_value=True), \
                mock.patch.object(drone_api, "_start_local_network_workers"), \
                mock.patch.object(drone_api, "_get_streamdeck_integration"), \
                mock.patch.object(drone_api, "_start_multiplayer_worker", create=True), \
                mock.patch.object(drone_api, "_build_handler", side_effect=RuntimeError("listener setup failed")):
            with self.assertRaisesRegex(RuntimeError, "listener setup failed"):
                drone_api.create_server(self.harness.settings)

        drone_api.retire_background_pollers()
        gate.open()
        _join_pollers(self)
        self.assertGreaterEqual(gate.sleeps, 1, "no create_server poller entered its loop")
        _assert_zero_checks(self, checks)


if __name__ == "__main__":
    unittest.main()
