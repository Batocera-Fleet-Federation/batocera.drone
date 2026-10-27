import math
import unittest
from unittest import mock

from app.common.poller_interval import (
    POLLER_INTERVAL_MAX_SECONDS,
    int_from_env,
    parse_poller_interval,
    poller_interval_from_env,
    seconds_from_env,
)


class ParsePollerIntervalTests(unittest.TestCase):
    def test_unset_or_blank_uses_default(self):
        self.assertEqual(parse_poller_interval(None, 300.0), 300.0)
        self.assertEqual(parse_poller_interval("", 300.0), 300.0)
        self.assertEqual(parse_poller_interval("   ", 300.0), 300.0)

    def test_non_numeric_uses_default(self):
        self.assertEqual(parse_poller_interval("abc", 300.0), 300.0)
        self.assertEqual(parse_poller_interval("5m", 300.0), 300.0)

    def test_non_finite_uses_default(self):
        for raw in ("inf", "-inf", "nan", "1e400"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_poller_interval(raw, 300.0), 300.0)

    def test_overflow_prone_values_use_default(self):
        for raw in ("1e10", str(POLLER_INTERVAL_MAX_SECONDS * 2)):
            with self.subTest(raw=raw):
                self.assertEqual(parse_poller_interval(raw, 300.0), 300.0)

    def test_usable_values_are_kept_and_clamped_to_minimum(self):
        self.assertEqual(parse_poller_interval("120", 300.0), 120.0)
        self.assertEqual(parse_poller_interval(" 90 ", 300.0), 90.0)
        self.assertEqual(parse_poller_interval("5", 300.0), 30.0)
        self.assertEqual(parse_poller_interval("-10", 300.0), 30.0)

    def test_accepted_values_are_sleepable(self):
        for raw in ("1", "86400", str(POLLER_INTERVAL_MAX_SECONDS)):
            with self.subTest(raw=raw):
                value = parse_poller_interval(raw, 300.0)
                self.assertTrue(math.isfinite(value))
                self.assertLessEqual(value * 1e9, 2 ** 63)


class PollerIntervalFromEnvTests(unittest.TestCase):
    def test_reads_named_variable(self):
        with mock.patch.dict("os.environ", {"DRONE_TEST_INTERVAL_SECONDS": "45"}, clear=True):
            self.assertEqual(poller_interval_from_env("DRONE_TEST_INTERVAL_SECONDS", 300.0), 45.0)

    def test_missing_variable_uses_default(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(poller_interval_from_env("DRONE_TEST_INTERVAL_SECONDS", 300.0), 300.0)

    def test_malformed_variable_uses_default_without_raising(self):
        with mock.patch.dict("os.environ", {"DRONE_TEST_INTERVAL_SECONDS": "abc"}, clear=True):
            self.assertEqual(poller_interval_from_env("DRONE_TEST_INTERVAL_SECONDS", 300.0), 300.0)

    def test_non_string_raw_uses_default_without_raising(self):
        self.assertEqual(parse_poller_interval(object(), 300.0), 300.0)
        self.assertEqual(parse_poller_interval(b"abc", 300.0), 300.0)

    def test_unusable_default_still_returns_a_sleepable_value(self):
        value = parse_poller_interval("abc", float("inf"))
        self.assertTrue(math.isfinite(value))
        self.assertLessEqual(value * 1e9, 2 ** 63)

    def test_hostile_conversion_hooks_use_the_safe_fallback(self):
        class ExplodingValue:
            def __float__(self):
                raise RuntimeError("conversion failed")

            def __str__(self):
                raise RuntimeError("string conversion failed")

        hostile = ExplodingValue()
        self.assertEqual(parse_poller_interval(hostile, 300.0), 300.0)
        self.assertEqual(parse_poller_interval("abc", hostile), 300.0)
        self.assertEqual(parse_poller_interval("120", 300.0, hostile), 120.0)


if __name__ == "__main__":
    unittest.main()


class SecondsAndIntFromEnvTests(unittest.TestCase):
    def test_seconds_reads_finite_non_negative_value(self):
        with mock.patch.dict("os.environ", {"DRONE_TEST_TIMEOUT_SECONDS": " 2.5 "}, clear=True):
            self.assertEqual(seconds_from_env("DRONE_TEST_TIMEOUT_SECONDS", 15.0), 2.5)

    def test_seconds_has_no_poller_floor(self):
        with mock.patch.dict("os.environ", {"DRONE_TEST_TIMEOUT_SECONDS": "0.5"}, clear=True):
            self.assertEqual(seconds_from_env("DRONE_TEST_TIMEOUT_SECONDS", 15.0), 0.5)

    def test_seconds_unusable_values_use_default_without_raising(self):
        for raw in ("", "   ", "abc", "nan", "inf", "-inf", "1e400", "-1", "1e12"):
            with self.subTest(raw=raw), mock.patch.dict("os.environ", {"DRONE_TEST_TIMEOUT_SECONDS": raw}, clear=True):
                self.assertEqual(seconds_from_env("DRONE_TEST_TIMEOUT_SECONDS", 15.0), 15.0)

    def test_seconds_missing_variable_uses_default(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(seconds_from_env("DRONE_TEST_TIMEOUT_SECONDS", 15.0), 15.0)

    def test_int_reads_bounded_value_and_rejects_the_rest(self):
        with mock.patch.dict("os.environ", {"DRONE_TEST_COUNT": "7"}, clear=True):
            self.assertEqual(int_from_env("DRONE_TEST_COUNT", 5), 7)
        for raw in ("abc", "1.5", "-1", "inf", "", "99999999999"):
            with self.subTest(raw=raw), mock.patch.dict("os.environ", {"DRONE_TEST_COUNT": raw}, clear=True):
                self.assertEqual(int_from_env("DRONE_TEST_COUNT", 5), 5)


class MalformedIntervalEnvImportTests(unittest.TestCase):
    """Import-time interval env reads must not abort ``import app.device.*``."""

    def test_malformed_import_time_env_vars_still_import(self):
        import subprocess
        import sys
        from pathlib import Path

        repo = Path(__file__).resolve().parents[1]
        env = {
            "PYTHONPATH": str(repo),
            "DRONE_SMTP_SEND_TIMEOUT_SECONDS": "abc",
            "DRONE_SMTP_ATTACHMENT_SEND_TIMEOUT_SECONDS": "abc",
            "DRONE_AUDIT_EMAIL_INTERVAL_SECONDS": "abc",
            "DRONE_VPN_CONNECT_TIMEOUT_SECONDS": "abc",
            "DRONE_VPN_SELF_HEAL_CHECK_INTERVAL_SECONDS": "inf",
            "DRONE_VPN_SELF_HEAL_MAX_ATTEMPTS_PER_WINDOW": "abc",
        }
        code = (
            "import math\n"
            "from app.device import smtp_manager as s, vpn_manager as v\n"
            "assert s.SMTP_SEND_TIMEOUT_SECONDS == 15.0\n"
            "assert s.SMTP_ATTACHMENT_SEND_TIMEOUT_SECONDS == 120.0\n"
            "assert s.AUDIT_EMAIL_POLL_INTERVAL_SECONDS == 300.0\n"
            "assert v.VPN_CONNECT_TIMEOUT_SECONDS == 15.0\n"
            "assert v.VPN_SELF_HEAL_CHECK_INTERVAL_SECONDS == 60.0\n"
            "assert v.VPN_SELF_HEAL_MAX_ATTEMPTS_PER_WINDOW == 5\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=str(repo), env=env,
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
