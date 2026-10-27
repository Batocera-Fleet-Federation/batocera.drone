"""Fresh-process UAT for #592 sharing-check interval env vars.

Issue #592: a malformed, non-finite, or unrepresentable
DRONE_{VPN,SMTP,TAILNET}_SHARING_CHECK_INTERVAL_SECONDS value must not kill
the poller before its first tick and must not prevent `import app.drone_api`.
The operator-facing contract is a safe parse that falls back to the 300s
default (still floored at 30s for usable numbers).

Why a separate suite from test_issue_592_interval_env_resilience_uat.py:

- SMTP and tailnet bind the interval at *module import*. Patching os.environ
  in the already-imported test process cannot reproduce the operator's boot.
- `float('inf')` / `float('1e400')` already succeeded at import before the
  fix; an import-only check of those values is a false pass. The child must
  run the poller and feed time.sleep a CPython-like OverflowError.
- A sleep mock that only records seconds would hide OverflowError if the
  fallback were still inf.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INTERVAL = 300.0
MINIMUM_INTERVAL = 30.0
_PYTIME_T_NANOSECONDS_LIMIT = 2 ** 63

# Values float() accepts or rejects that must not be used as a sleep duration.
UNUSABLE_INTERVALS = (
    "abc",
    "5m",
    "0x1.0p+4",
    "--1",
    "inf",
    "+inf",
    "-inf",
    "Infinity",
    "+Infinity",
    "-Infinity",
    "nan",
    "NaN",
    "NAN",
    "1e10",
    "1e400",
    "1e309",
    "  inf  ",
    "\t1e400\n",
)

INTERVAL_ENV_VARS = (
    "DRONE_VPN_SHARING_CHECK_INTERVAL_SECONDS",
    "DRONE_SMTP_SHARING_CHECK_INTERVAL_SECONDS",
    "DRONE_TAILNET_SHARING_CHECK_INTERVAL_SECONDS",
)

CHILD_CONSTANTS = textwrap.dedent(
    """
    import json
    from app.device import smtp_manager, tailnet_service
    print(json.dumps({
        "smtp": smtp_manager.SMTP_SHARING_CHECK_INTERVAL_SECONDS,
        "tailnet": tailnet_service.TAILNET_SHARING_CHECK_INTERVAL_SECONDS,
    }))
    """
).strip()

CHILD_POLLER = textwrap.dedent(
    """
    import json
    import math
    import sys
    from unittest import mock

    from app.drone_api import Settings
    import app.device.smtp_manager as smtp_manager
    import app.device.tailnet_service as tailnet_service
    import app.device.vpn_manager as vpn_manager

    PYTIME_LIMIT = 2 ** 63

    def sleepable(seconds):
        return math.isfinite(seconds) and seconds * 1e9 < PYTIME_LIMIT

    def run(module, fn_name, check_name):
        sleeps = []
        checks = mock.Mock(return_value=False)
        escaped = None

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if not sleepable(seconds):
                raise OverflowError("timestamp too large to convert to C _PyTime_t")

        def retired(_generation):
            return len(sleeps) > 1

        with mock.patch.object(module, "poller_generation", return_value=19), \\
                mock.patch.object(module, "poller_retired", new=retired), \\
                mock.patch.object(module.time, "sleep", new=fake_sleep), \\
                mock.patch.object(module, check_name, checks):
            try:
                getattr(module, fn_name)(Settings.from_env())
            except Exception as error:
                escaped = type(error).__name__ + ": " + str(error)

        return {
            "checks": checks.call_count,
            "escaped": escaped,
            "sleeps": [s if sleepable(s) else repr(s) for s in sleeps],
            "sleepable": all(sleepable(s) for s in sleeps),
        }

    print(json.dumps({
        "smtp": run(smtp_manager, "run_sharing_revocation_poller", "check_sharing_revocation"),
        "tailnet": run(tailnet_service, "run_tailnet_sharing_revocation_poller", "check_tailnet_sharing_revocation"),
        "vpn": run(vpn_manager, "run_sharing_revocation_poller", "check_sharing_revocation"),
    }))
    """
).strip()


def _sleep_like_cpython(seconds: float) -> None:
    if not math.isfinite(seconds) or seconds * 1e9 >= _PYTIME_T_NANOSECONDS_LIMIT:
        raise OverflowError("timestamp too large to convert to C _PyTime_t")


class Issue592SiblingPollerFreshProcessUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(ROOT),
            "USERDATA_ROOT": str(self.root),
            "ROMS_ROOT": str(self.root / "roms"),
            "BIOS_ROOT": str(self.root / "bios"),
            "SAVES_ROOT": str(self.root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(self.root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-592-fresh-process",
        }
        if extra:
            env.update(extra)
        return env

    def _run_child(self, script: str, extra: dict[str, str] | None = None, *, timeout: int = 90) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=self._env(extra),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _load_json(self, result: subprocess.CompletedProcess[str]) -> dict:
        self.assertEqual(result.returncode, 0, result.stderr[-4000:] if result.stderr else result.stdout[-4000:])
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            self.fail(f"child stdout was not JSON: {error}; stdout={result.stdout!r}; stderr={result.stderr[-2000:]!r}")

    def test_smtp_and_tailnet_constants_fallback_in_a_fresh_process(self) -> None:
        for variable in (
            "DRONE_SMTP_SHARING_CHECK_INTERVAL_SECONDS",
            "DRONE_TAILNET_SHARING_CHECK_INTERVAL_SECONDS",
        ):
            for raw in ("abc", "inf", "+Infinity", "nan", "1e10", "1e400", "  inf  "):
                with self.subTest(variable=variable, raw=raw):
                    payload = self._load_json(self._run_child(CHILD_CONSTANTS, {variable: raw}))
                    expected_key = "smtp" if "SMTP" in variable else "tailnet"
                    self.assertEqual(payload[expected_key], DEFAULT_INTERVAL)
                    other = "tailnet" if expected_key == "smtp" else "smtp"
                    self.assertEqual(payload[other], DEFAULT_INTERVAL)

    def test_usable_numeric_interval_is_kept_at_smtp_and_tailnet_import(self) -> None:
        payload = self._load_json(
            self._run_child(
                CHILD_CONSTANTS,
                {
                    "DRONE_SMTP_SHARING_CHECK_INTERVAL_SECONDS": "120",
                    "DRONE_TAILNET_SHARING_CHECK_INTERVAL_SECONDS": "45.5",
                },
            )
        )
        self.assertEqual(payload["smtp"], 120.0)
        self.assertEqual(payload["tailnet"], 45.5)

    def test_sub_minimum_numeric_interval_is_floored_not_used_as_zero(self) -> None:
        payload = self._load_json(
            self._run_child(
                CHILD_CONSTANTS,
                {
                    "DRONE_SMTP_SHARING_CHECK_INTERVAL_SECONDS": "5",
                    "DRONE_TAILNET_SHARING_CHECK_INTERVAL_SECONDS": "0",
                },
            )
        )
        self.assertEqual(payload["smtp"], MINIMUM_INTERVAL)
        self.assertEqual(payload["tailnet"], MINIMUM_INTERVAL)

    def test_all_three_pollers_tick_once_with_unusable_intervals(self) -> None:
        for raw in UNUSABLE_INTERVALS:
            with self.subTest(raw=raw):
                extra = {name: raw for name in INTERVAL_ENV_VARS}
                payload = self._load_json(self._run_child(CHILD_POLLER, extra, timeout=120))
                for name in ("vpn", "smtp", "tailnet"):
                    with self.subTest(poller=name, raw=raw):
                        result = payload[name]
                        self.assertIsNone(
                            result["escaped"],
                            f"{name} poller died on interval {raw!r}: {result['escaped']}",
                        )
                        self.assertTrue(
                            result["sleepable"],
                            f"{name} poller slept an unrepresentable interval {result['sleeps']!r}",
                        )
                        self.assertEqual(result["checks"], 1, f"{name} never reached its first check")
                        self.assertEqual(result["sleeps"], [DEFAULT_INTERVAL, DEFAULT_INTERVAL])

    def test_all_three_pollers_keep_a_valid_custom_interval(self) -> None:
        extra = {name: "90" for name in INTERVAL_ENV_VARS}
        payload = self._load_json(self._run_child(CHILD_POLLER, extra, timeout=120))
        for name in ("vpn", "smtp", "tailnet"):
            result = payload[name]
            self.assertIsNone(result["escaped"], result)
            self.assertEqual(result["checks"], 1)
            self.assertEqual(result["sleeps"], [90.0, 90.0])

    def test_import_app_drone_api_with_every_unusable_value_on_every_var(self) -> None:
        samples = (
            ("DRONE_VPN_SHARING_CHECK_INTERVAL_SECONDS", "abc"),
            ("DRONE_VPN_SHARING_CHECK_INTERVAL_SECONDS", "inf"),
            ("DRONE_SMTP_SHARING_CHECK_INTERVAL_SECONDS", "abc"),
            ("DRONE_SMTP_SHARING_CHECK_INTERVAL_SECONDS", "1e400"),
            ("DRONE_TAILNET_SHARING_CHECK_INTERVAL_SECONDS", "Infinity"),
            ("DRONE_TAILNET_SHARING_CHECK_INTERVAL_SECONDS", "nan"),
        )
        for variable, raw in samples:
            with self.subTest(variable=variable, raw=raw):
                result = self._run_child("import app.drone_api", {variable: raw})
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])

    def test_import_survives_all_three_unusable_vars_together(self) -> None:
        extra = {
            "DRONE_VPN_SHARING_CHECK_INTERVAL_SECONDS": "abc",
            "DRONE_SMTP_SHARING_CHECK_INTERVAL_SECONDS": "inf",
            "DRONE_TAILNET_SHARING_CHECK_INTERVAL_SECONDS": "1e400",
        }
        result = self._run_child("import app.drone_api", extra)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])

    def test_flat_entrypoint_import_survives_malformed_smtp_interval(self) -> None:
        env = self._env({"DRONE_SMTP_SHARING_CHECK_INTERVAL_SECONDS": "not-a-number"})
        env["PYTHONPATH"] = str(ROOT / "app")
        result = subprocess.run(
            [sys.executable, "-c", "import drone_api"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])


class Issue592VpnPollerOverflowEmulationUAT(unittest.TestCase):
    """VPN reads the interval at poller start, so the already-imported module is the real path."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        with mock.patch.dict(
            os.environ,
            {
                "USERDATA_ROOT": str(root),
                "ROMS_ROOT": str(root / "roms"),
                "BIOS_ROOT": str(root / "bios"),
                "SAVES_ROOT": str(root / "saves"),
                "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
                "DRONE_DEVICE_ID": "adversarial-592-vpn-overflow",
            },
            clear=True,
        ):
            self.settings = Settings.from_env()

    def test_vpn_poller_survives_unusable_intervals_with_cpython_sleep(self) -> None:
        for raw in UNUSABLE_INTERVALS:
            with self.subTest(raw=raw):
                checks = mock.Mock(return_value=False)
                sleeps: list[float] = []
                escaped = None

                def fake_sleep(seconds: float) -> None:
                    sleeps.append(seconds)
                    _sleep_like_cpython(seconds)

                def retired(_generation: int) -> bool:
                    return len(sleeps) > 1

                with mock.patch.dict(os.environ, {"DRONE_VPN_SHARING_CHECK_INTERVAL_SECONDS": raw}), \
                        mock.patch.object(vpn_manager, "poller_generation", return_value=23), \
                        mock.patch.object(vpn_manager, "poller_retired", new=retired), \
                        mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep), \
                        mock.patch.object(vpn_manager, "check_sharing_revocation", checks):
                    try:
                        vpn_manager.run_sharing_revocation_poller(self.settings)
                    except (OverflowError, ValueError) as error:
                        escaped = error

                self.assertIsNone(escaped, f"interval {raw!r} killed the VPN poller: {escaped!r}")
                self.assertEqual(checks.call_count, 1)
                self.assertEqual(sleeps, [DEFAULT_INTERVAL, DEFAULT_INTERVAL])
                for seconds in sleeps:
                    _sleep_like_cpython(seconds)


if __name__ == "__main__":
    unittest.main()
