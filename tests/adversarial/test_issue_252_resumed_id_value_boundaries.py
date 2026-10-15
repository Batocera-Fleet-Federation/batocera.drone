"""Issue #252 boundaries: only a well-formed string may be resumed as a device id.

A MagicMock is the reported value. The same resume branch also fires for any
truthy journal['new_device_id']: other unittest mocks, a number, a list, bytes,
bool True, a malformed string. Production journals are dicts from json.loads;
anything that is not a real str matching _normalize_device_id must raise
IdentityError rather than stringify into a logical Drone id.

Expected, from the issue (mock / non-string / malformed resumed values should
raise) and the clone-id invariant in docs/drone-identity.md, plus the #217 /
#169 lesson that isinstance(..., T) is not enough because spec=T mocks spoof
__class__:

- Every unittest.mock flavour used as the *value* is rejected, including
  spec=str masquerades. A mock must not be adopted as the id, must not mint a
  fresh id as if the key were missing, and must not leak TypeError from
  re.match / str.strip.
- JSON-reachable non-strings (int, float, true, array, object) raise
  IdentityError.
- Malformed strings that _normalize_device_id rejects (spaces, NULs, too
  short/long, embedded newline) raise IdentityError.
- A real str that passes _normalize_device_id still resumes, including a str
  subclass and a 3-character token at the pattern floor.
- Rejection does not persist a mock-named file or rewrite an existing id.
- The error is IdentityError, not a leaked TypeError from the normalizer.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity


CONFIGURED_ID = "AA:BB:CC:DD:EE:FF"
RESUMED_ID = "02:aa:bb:cc:dd:ee"
MAC_SHAPED = r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$"
INVALID_JOURNALED_ID = "the journaled device id is not a valid Drone id"


class _SpecSubclass(mock.MagicMock):
    """User-defined MagicMock subclass; its type lives in this test module."""


class _JournalDict(dict):
    """Legitimate dict subclass; production json.loads is dict, but subclasses must still reject bad values."""


class _StrId(str):
    """A real str subclass; it is a string, not a mock."""


MOCK_VALUE_FLAVOURS = {
    "MagicMock()": lambda: mock.MagicMock(),
    "Mock()": lambda: mock.Mock(),
    "NonCallableMock()": lambda: mock.NonCallableMock(),
    "NonCallableMagicMock()": lambda: mock.NonCallableMagicMock(),
    "AsyncMock()": lambda: mock.AsyncMock(),
    "MagicMock(spec=str)": lambda: mock.MagicMock(spec=str),
    "Mock(spec=str)": lambda: mock.Mock(spec=str),
    "NonCallableMock(spec=str)": lambda: mock.NonCallableMock(spec=str),
    "NonCallableMagicMock(spec=str)": lambda: mock.NonCallableMagicMock(spec=str),
    "AsyncMock(spec=str)": lambda: mock.AsyncMock(spec=str),
    "MagicMock(spec_set=str)": lambda: mock.MagicMock(spec_set=str),
    "create_autospec(str, instance=True)": lambda: mock.create_autospec(str, instance=True),
    "create_autospec(str)": lambda: mock.create_autospec(str),
    "MagicMock subclass with spec=str": lambda: _SpecSubclass(spec=str),
}


def _spec_str_wired_as_mac() -> mock.MagicMock:
    """A spec=str mock whose strip() looks like a previously minted id."""
    value = mock.MagicMock(spec=str)
    value.__len__.return_value = len(RESUMED_ID)
    value.strip.return_value = RESUMED_ID
    return value


NON_STRING_VALUES = {
    "int": 12345,
    "float": 1.5,
    "bool-true": True,
    "list": [RESUMED_ID],
    "tuple": (RESUMED_ID,),
    "dict": {"id": RESUMED_ID},
    "bytes": RESUMED_ID.encode("ascii"),
    "bytearray": bytearray(RESUMED_ID.encode("ascii")),
    "object": object(),
}

MALFORMED_STRINGS = {
    "spaces": "bad id with spaces!",
    "too-short": "ab",
    "too-long": "x" * 129,
    "embedded-newline": "aa:bb:cc:dd:ee:ff\nsecond-line",
    "nul": "aa:bb:cc:dd:ee:ff\x00",
    "zwsp": "aa:bb:cc:dd:ee:ff\u200b",
    "mock-repr": "<MagicMock id='1'>",
    "bang": "id!",
}


class _ChooseDeviceIdSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()
        self._previous_cwd = os.getcwd()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmpdir.name).resolve()
        os.chdir(self.cwd)
        self.userdata = self.cwd / "userdata"
        (self.userdata / "system" / "drone-app").mkdir(parents=True)
        self.persisted = self.userdata / "system" / "drone-app" / "device-id"
        self.settings = SimpleNamespace(userdata_root=self.userdata, device_id=CONFIGURED_ID)

    def tearDown(self) -> None:
        os.chdir(self._previous_cwd)
        self._tmpdir.cleanup()
        self._env.stop()

    def _choose(self, journal):
        return identity._choose_device_id(self.settings, journal)

    def _assert_rejected_value(self, value) -> None:
        try:
            chosen = self._choose({"new_device_id": value})
        except identity.IdentityError as error:
            self.assertRegex(str(error), INVALID_JOURNALED_ID)
            self.assertFalse(self.persisted.exists(), msg="rejection must not persist a device-id")
            return
        except Exception as error:
            self.fail(
                f"{type(error).__name__} leaked instead of IdentityError: {error}"
            )
        self.fail(
            f"returned {chosen!r}; a mock or non-string resumed id must raise IdentityError"
        )


class MockValueFlavourRejectionTests(_ChooseDeviceIdSandbox):
    def test_every_mock_flavour_as_value_raises_identity_error(self) -> None:
        for label, factory in MOCK_VALUE_FLAVOURS.items():
            with self.subTest(value=label):
                if self.persisted.exists():
                    self.persisted.unlink()
                self._assert_rejected_value(factory())

    def test_spec_str_mock_wired_as_a_mac_is_still_rejected(self) -> None:
        self._assert_rejected_value(_spec_str_wired_as_mac())

    def test_mock_flavours_do_not_return_a_stringified_mock_when_error_is_swallowed(self) -> None:
        for label, factory in MOCK_VALUE_FLAVOURS.items():
            with self.subTest(value=label):
                if self.persisted.exists():
                    self.persisted.unlink()
                chosen = None
                try:
                    chosen = self._choose({"new_device_id": factory()})
                except identity.IdentityError:
                    chosen = None
                except Exception as error:
                    self.fail(
                        f"{label} leaked {type(error).__name__} instead of IdentityError: {error}"
                    )
                if chosen is not None:
                    self.assertNotIn("MagicMock", str(chosen))
                    self.assertNotIn("<Mock", str(chosen))
                    self.assertNotEqual(chosen, CONFIGURED_ID)
                self.assertIsNone(
                    chosen,
                    msg=f"{label} returned {chosen!r}; a mock value must not stringify or mint into a device id",
                )

    def test_rejection_leaves_an_existing_persisted_id_alone(self) -> None:
        self.persisted.write_text(CONFIGURED_ID + "\n", encoding="utf-8")
        for label, factory in (
            ("MagicMock", mock.MagicMock),
            ("MagicMock(spec=str)", lambda: mock.MagicMock(spec=str)),
            ("Mock(spec=str)", lambda: mock.Mock(spec=str)),
        ):
            with self.subTest(value=label):
                try:
                    chosen = self._choose({"new_device_id": factory()})
                except identity.IdentityError:
                    chosen = None
                except Exception as error:
                    self.fail(f"{label} leaked {type(error).__name__} instead of IdentityError: {error}")
                else:
                    self.fail(f"{label} returned {chosen!r}; a mock value must not mint or resume")
                self.assertEqual(self.persisted.read_text(encoding="utf-8"), CONFIGURED_ID + "\n")


class NonStringAndMalformedValueTests(_ChooseDeviceIdSandbox):
    def test_non_string_values_raise_identity_error_not_typeerror(self) -> None:
        for label, value in NON_STRING_VALUES.items():
            with self.subTest(value=label):
                if self.persisted.exists():
                    self.persisted.unlink()
                self._assert_rejected_value(value)

    def test_malformed_strings_raise_identity_error(self) -> None:
        for label, value in MALFORMED_STRINGS.items():
            with self.subTest(value=label):
                if self.persisted.exists():
                    self.persisted.unlink()
                self._assert_rejected_value(value)

    def test_dict_subclass_with_mock_value_is_still_rejected(self) -> None:
        journal = _JournalDict(new_device_id=mock.MagicMock())
        with self.assertRaisesRegex(identity.IdentityError, INVALID_JOURNALED_ID):
            self._choose(journal)
        self.assertFalse(self.persisted.exists())

    def test_bytes_that_decode_to_a_mac_are_not_stringified_into_that_mac(self) -> None:
        chosen = None
        try:
            chosen = self._choose({"new_device_id": RESUMED_ID.encode("ascii")})
        except identity.IdentityError:
            chosen = None
        self.assertIsNone(chosen)
        self.assertFalse(self.persisted.exists())


class WellFormedStringStillResumesTests(_ChooseDeviceIdSandbox):
    def test_str_subclass_with_a_mac_resumes(self) -> None:
        chosen = self._choose({"new_device_id": _StrId(RESUMED_ID)})
        self.assertEqual(chosen, RESUMED_ID)
        self.assertFalse(self.persisted.exists())

    def test_pattern_floor_token_resumes(self) -> None:
        chosen = self._choose({"new_device_id": "abc"})
        self.assertEqual(chosen, "abc")

    def test_cabinet_style_id_resumes(self) -> None:
        chosen = self._choose({"new_device_id": "cabinet-1"})
        self.assertEqual(chosen, "cabinet-1")

    def test_dict_subclass_with_a_mac_resumes(self) -> None:
        chosen = self._choose(_JournalDict(new_device_id=RESUMED_ID))
        self.assertEqual(chosen, RESUMED_ID)
        self.assertFalse(self.persisted.exists())

    def test_empty_dict_still_mints(self) -> None:
        chosen = self._choose({})
        self.assertRegex(chosen, MAC_SHAPED)
        self.assertNotEqual(chosen, CONFIGURED_ID)
        self.assertEqual(self.persisted.read_text(encoding="utf-8").strip(), chosen)


if __name__ == "__main__":
    unittest.main()
