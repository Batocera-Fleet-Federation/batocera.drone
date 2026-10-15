"""Issue #254 adversarial: the 'must be a mapping' check and its message must agree.

The reported defect is a gap between the check (``dict in type(journal).__mro__``)
and the message ('the identity journal must be a mapping'). The owner decision
recorded for #254 is that a real Mapping is a journal. Expected behaviour, derived
from that decision and the #217 mock rule before reading the patch:

- A journal is refused with 'must be a mapping' exactly when it is not a real
  Mapping, or when it is a unittest mock. Every real Mapping resumes a well-formed id.
- A unittest mock whose spec is UserDict (spec, spec_set, autospec) is still a mock:
  it must not resume an id even when every lookup is wired to one.
- A class registered virtually with collections.abc.Mapping.register is a Mapping by
  the ABC's own definition, so it resumes like any other real Mapping.
- A real Mapping with an invalid resumed value raises the journaled-id error, never a
  raw TypeError/KeyError/AttributeError and never the 'must be a mapping' message.
"""

from __future__ import annotations

import collections
import collections.abc
import os
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity


CONFIGURED_ID = "AA:BB:CC:DD:EE:FF"
RESUMED_ID = "02:11:22:33:44:55"
JOURNAL_MUST_BE_MAPPING = "the identity journal must be a mapping"
INVALID_JOURNALED_ID = "the journaled device id is not a valid Drone id"


class _VirtualMapping:
    """Not a Mapping subclass; registered with the ABC below, so it IS a Mapping."""

    def __init__(self, data):
        self._data = dict(data)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getitem__(self, key):
        return self._data[key]

    def __contains__(self, key) -> bool:
        return key in self._data


collections.abc.Mapping.register(_VirtualMapping)


class _Unregistered:
    """Has the same methods as _VirtualMapping but no ABC registration: not a Mapping."""

    def __init__(self, data):
        self._data = dict(data)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getitem__(self, key):
        return self._data[key]

    def __contains__(self, key) -> bool:
        return key in self._data


def _wire_mock(journal):
    """Make get/[]/in all report RESUMED_ID, the way a hostile or buggy journal would."""
    journal.get.return_value = RESUMED_ID
    journal.__getitem__.return_value = RESUMED_ID
    journal.__contains__.return_value = True
    return journal


def _snapshot(root: Path) -> dict:
    entries = {}
    for path in sorted(root.rglob("*")):
        key = path.relative_to(root).as_posix()
        entries[key] = path.read_bytes() if path.is_file() else None
    return entries


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)
        self._previous_cwd = os.getcwd()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.cwd = Path(self._tmpdir.name).resolve()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, self._previous_cwd)
        self.userdata = self.cwd / "userdata"
        (self.userdata / "system" / "drone-app").mkdir(parents=True)
        self.persisted = self.userdata / "system" / "drone-app" / "device-id"
        self.settings = SimpleNamespace(userdata_root=self.userdata, device_id=CONFIGURED_ID)

    def _choose(self, journal):
        return identity._choose_device_id(self.settings, journal)


class CheckAndMessageAgreementTests(_Sandbox):
    # (label, factory, accepted). accepted=True means a real Mapping that must resume.
    CANDIDATES = (
        ("UserDict", lambda: collections.UserDict({"new_device_id": RESUMED_ID}), True),
        ("ChainMap", lambda: collections.ChainMap({"new_device_id": RESUMED_ID}), True),
        ("MappingProxyType", lambda: MappingProxyType({"new_device_id": RESUMED_ID}), True),
        ("registered virtual Mapping", lambda: _VirtualMapping({"new_device_id": RESUMED_ID}), True),
        ("dict", lambda: {"new_device_id": RESUMED_ID}, True),
        ("unregistered duck-typed", lambda: _Unregistered({"new_device_id": RESUMED_ID}), False),
        ("mock spec=UserDict", lambda: _wire_mock(mock.MagicMock(spec=collections.UserDict)), False),
        ("list of pairs", lambda: [("new_device_id", RESUMED_ID)], False),
        ("str", lambda: RESUMED_ID, False),
        ("None", lambda: None, False),
    )

    def test_refusal_message_matches_whether_the_value_is_a_real_mapping(self) -> None:
        for label, factory, accepted in self.CANDIDATES:
            with self.subTest(journal=label):
                self.persisted.unlink(missing_ok=True)
                if accepted:
                    self.assertEqual(self._choose(factory()), RESUMED_ID)
                    self.assertFalse(self.persisted.exists())
                else:
                    with self.assertRaises(identity.IdentityError) as ctx:
                        self._choose(factory())
                    self.assertEqual(str(ctx.exception), JOURNAL_MUST_BE_MAPPING)
                    self.assertFalse(self.persisted.exists())

    def test_every_real_mapping_is_an_instance_the_check_accepts(self) -> None:
        # For real objects the check and isinstance(..., Mapping) must give the same answer.
        # Mocks are excluded: a spec'd mock passes isinstance by design and is still refused.
        for label, factory, accepted in self.CANDIDATES:
            if "mock" in label:
                continue
            with self.subTest(journal=label):
                self.assertEqual(isinstance(factory(), collections.abc.Mapping), accepted)


class SpecUserDictMockTests(_Sandbox):
    MOCK_FACTORIES = {
        "MagicMock(spec=UserDict)": lambda: mock.MagicMock(spec=collections.UserDict),
        "NonCallableMagicMock(spec=UserDict)": lambda: mock.NonCallableMagicMock(
            spec=collections.UserDict
        ),
        "Mock(spec_set=UserDict)": lambda: mock.Mock(spec_set=collections.UserDict),
        "create_autospec(UserDict, instance=True)": lambda: mock.create_autospec(
            collections.UserDict, instance=True
        ),
    }

    def test_userdict_spec_mocks_never_resume_a_wired_id(self) -> None:
        for label, factory in self.MOCK_FACTORIES.items():
            with self.subTest(journal=label):
                journal = factory()
                try:
                    _wire_mock(journal)
                except AttributeError:
                    # Some mock variants refuse magic-method wiring; the rejection
                    # below must still hold for the bare mock.
                    pass
                before = _snapshot(self.userdata)
                with self.assertRaises(identity.IdentityError) as ctx:
                    chosen = self._choose(journal)
                    self.fail(f"{label} returned {chosen!r}; a unittest mock must not resume")
                self.assertEqual(str(ctx.exception), JOURNAL_MUST_BE_MAPPING)
                self.assertEqual(_snapshot(self.userdata), before)

    def test_userdict_spec_mock_resumed_value_is_not_stringified(self) -> None:
        # The mock's own repr must never become a device id, even when the journal is refused.
        journal = mock.MagicMock(spec=collections.UserDict)
        with self.assertRaises(identity.IdentityError):
            self._choose(journal)
        self.assertFalse(self.persisted.exists())


class RealUserDictInvalidValueTests(_Sandbox):
    def test_truthy_non_string_values_on_userdict_raise_the_journaled_id_error(self) -> None:
        values = (
            float("nan"),
            1.5,
            1 + 0j,
            b"02:11:22:33:44:55",
            bytearray(b"02:11:22:33:44:55"),
            ["02:11:22:33:44:55"],
            ("02:11:22:33:44:55",),
            frozenset({"02:11:22:33:44:55"}),
            {"id": RESUMED_ID},
        )
        for value in values:
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(identity.IdentityError) as ctx:
                    self._choose(collections.UserDict({"new_device_id": value}))
                self.assertEqual(str(ctx.exception), INVALID_JOURNALED_ID)
                self.assertFalse(self.persisted.exists())


if __name__ == "__main__":
    unittest.main()
