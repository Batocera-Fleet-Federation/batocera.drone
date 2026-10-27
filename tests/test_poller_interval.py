import math
import unittest
from unittest import mock

from app.common.poller_interval import (
    POLLER_INTERVAL_MAX_SECONDS,
    parse_poller_interval,
    poller_interval_from_env,
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
