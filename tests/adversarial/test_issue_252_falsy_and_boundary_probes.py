"""Issue #252 adversarial probes: falsy/spoofed mock values and resumed-id boundaries.

The #252 issue asks that ``_choose_device_id`` never turn a non-string resumed value
into the logical Drone id. Expected behaviour, derived from the issue and from
``_valid_journal`` (the ensure_identity resume-path validator) before reading the
fix:

- A mock is rejected whether it is truthy or falsy (``__bool__`` returning False),
  whether it masquerades as ``str``/``dict`` via ``spec=``, and whether its
  ``__str__``/``__repr__`` raise. Rejection is always ``IdentityError``, never a mock
  repr, a partial write, or an unrelated exception type.
- A resumed id is accepted exactly when the shared normalizer accepts it, so the helper
  and ``_valid_journal`` agree on the 3..128 character Drone-id contract.
- Whitespace-only text is a present but invalid value, not a missing field.
- A rejected value leaves the whole userdata tree byte-for-byte unchanged.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity
from app.common.device_identity import _normalize_device_id


CONFIGURED_ID = "AA:BB:CC:DD:EE:FF"
RESUMED_ID = "02:11:22:33:44:55"
INVALID_JOURNALED_ID = "the journaled device id is not a valid Drone id"


def _tree_snapshot(root: Path) -> dict[str, bytes | None]:
    snapshot: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        key = str(path.relative_to(root))
        snapshot[key] = path.read_bytes() if path.is_file() else None
    return snapshot


class _Sandbox(unittest.TestCase):
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


class FalsyAndSpoofedMockTests(_Sandbox):
    def test_falsy_mock_value_raises_instead_of_minting_a_fresh_id(self) -> None:
        # A falsy mock must not fall through to the "missing field" mint path.
        value = mock.MagicMock()
        value.__bool__.return_value = False
        before = _tree_snapshot(self.userdata)
        with self.assertRaisesRegex(identity.IdentityError, INVALID_JOURNALED_ID):
            self._choose({"new_device_id": value})
        self.assertEqual(_tree_snapshot(self.userdata), before)

    def test_mock_spoofing_str_via_spec_raises(self) -> None:
        # spec=str makes isinstance(value, str) true; the mock guard must still catch it.
        value = mock.MagicMock(spec=str)
        self.assertTrue(isinstance(value, str))
        before = _tree_snapshot(self.userdata)
        with self.assertRaisesRegex(identity.IdentityError, INVALID_JOURNALED_ID):
            self._choose({"new_device_id": value})
        self.assertEqual(_tree_snapshot(self.userdata), before)

    def test_mock_whose_str_and_repr_raise_still_yields_identity_error(self) -> None:
        value = mock.MagicMock()
        value.__str__ = mock.Mock(side_effect=RuntimeError("no str"))
        value.__repr__ = mock.Mock(side_effect=RuntimeError("no repr"))
        with self.assertRaisesRegex(identity.IdentityError, INVALID_JOURNALED_ID):
            self._choose({"new_device_id": value})
        self.assertFalse(self.persisted.exists())

    def test_mock_journal_spoofing_dict_is_rejected_before_any_write(self) -> None:
        journal = mock.MagicMock(spec=dict)
        before = _tree_snapshot(self.userdata)
        with self.assertRaises(identity.IdentityError):
            self._choose(journal)
        self.assertEqual(_tree_snapshot(self.userdata), before)

    def test_dict_subclass_journal_holding_a_mock_is_rejected(self) -> None:
        class Journal(dict):
            pass

        with self.assertRaisesRegex(identity.IdentityError, INVALID_JOURNALED_ID):
            self._choose(Journal(new_device_id=mock.MagicMock()))
        self.assertFalse(self.persisted.exists())


class ResumedIdBoundaryTests(_Sandbox):
    def test_length_boundaries_agree_with_the_shared_normalizer(self) -> None:
        # The helper and _valid_journal both rely on _normalize_device_id; the helper must
        # accept exactly the ids the normalizer accepts, at both ends of the length range.
        cases = {
            "ab": None,
            "abc": "abc",
            "a" * 128: "a" * 128,
            "a" * 129: None,
        }
        for candidate, expected in cases.items():
            with self.subTest(length=len(candidate)):
                self.assertEqual(_normalize_device_id(candidate), expected)
                if expected is None:
                    with self.assertRaisesRegex(identity.IdentityError, INVALID_JOURNALED_ID):
                        self._choose({"new_device_id": candidate})
                else:
                    self.assertEqual(self._choose({"new_device_id": candidate}), expected)
                self.assertFalse(self.persisted.exists())

    def test_whitespace_only_value_is_present_and_invalid_not_missing(self) -> None:
        # Truthy but normalizes to nothing: must raise, not mint like a missing field.
        for raw in ("   ", "\t\n", " \r "):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(identity.IdentityError, INVALID_JOURNALED_ID):
                    self._choose({"new_device_id": raw})
                self.assertFalse(self.persisted.exists())

    def test_embedded_newline_inside_an_id_is_rejected(self) -> None:
        # A newline is not allowed inside an id even though a trailing one is stripped.
        with self.assertRaisesRegex(identity.IdentityError, INVALID_JOURNALED_ID):
            self._choose({"new_device_id": "02:11\n:22:33:44:55"})
        self.assertFalse(self.persisted.exists())

    def test_valid_resume_leaves_userdata_tree_unchanged(self) -> None:
        before = _tree_snapshot(self.userdata)
        self.assertEqual(self._choose({"new_device_id": RESUMED_ID}), RESUMED_ID)
        self.assertEqual(_tree_snapshot(self.userdata), before)


if __name__ == "__main__":
    unittest.main()
