"""Issue #254 adversarial: a unittest mock that also inherits Mapping must not resume an id.

Issue #254 replaced the ``dict in type(journal).__mro__`` check with
``issubclass(type(journal), collections.abc.Mapping)`` so that real Mappings
(UserDict, MappingProxyType) are accepted. ``issubclass`` looks at the class
hierarchy, not at what the object claims to be, so a class that inherits both a
unittest mock and ``collections.abc.Mapping`` passes the gate. The #217 rule is
that a mock never supplies a resumed device id, so this is a regression
introduced by the #254 change, not a pre-existing gap.

Expected, derived from the #217 invariant and docs/drone-identity.md before
inspecting the patch:

- Any unittest mock journal raises IdentityError and never returns the id its
  get/__getitem__ returns, even when the class also inherits Mapping.
- A rejected mock journal leaves the userdata tree byte-identical.
- Control: the same key/value wiring on a genuine Mapping (not a mock) resumes,
  so the failure is about the mock, not about the values.
- Control: mock.MagicMock(spec=Mapping) (not a Mapping subclass) stays rejected.
"""

from __future__ import annotations

import collections.abc
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity


CONFIGURED_ID = "AA:BB:CC:DD:EE:FF"
SUPPLIED_ID = "02:11:22:33:44:55"


class _MappingStubs:
    """Abstract Mapping hooks. Mock classes override these per instance."""

    def __getitem__(self, key):
        raise KeyError(key)

    def __iter__(self):
        return iter(())

    def __len__(self) -> int:
        return 0


class _MagicMockMapping(_MappingStubs, mock.MagicMock, collections.abc.Mapping):
    """A MagicMock that is also a real Mapping subclass."""


class _NonCallableMagicMockMapping(_MappingStubs, mock.NonCallableMagicMock, collections.abc.Mapping):
    """A NonCallableMagicMock that is also a real Mapping subclass."""


class _PlainMockMapping(_MappingStubs, mock.Mock, collections.abc.Mapping):
    """A plain Mock that is also a real Mapping subclass."""


class _RealMapping(collections.abc.Mapping):
    """Genuine Mapping with the same wiring the mocks get, for the positive control."""

    def __init__(self, data):
        self._data = dict(data)

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


MOCK_MAPPING_CLASSES = {
    "MagicMock + Mapping": _MagicMockMapping,
    "NonCallableMagicMock + Mapping": _NonCallableMagicMockMapping,
    "Mock + Mapping": _PlainMockMapping,
}


def _wired(journal):
    """Make get/[]/in all report SUPPLIED_ID, like a hostile or buggy journal would."""
    journal.get = mock.Mock(return_value=SUPPLIED_ID)
    journal.__getitem__ = mock.Mock(return_value=SUPPLIED_ID)
    journal.__contains__ = mock.Mock(return_value=True)
    return journal


def _snapshot(root: Path) -> dict:
    entries = {}
    for path in sorted(root.rglob("*")):
        key = path.relative_to(root).as_posix()
        entries[key] = path.read_bytes() if path.is_file() else None
    return entries


class MockMappingMasqueradeTests(unittest.TestCase):
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

    def test_mock_classes_that_inherit_mapping_are_not_journals(self) -> None:
        for label, cls in MOCK_MAPPING_CLASSES.items():
            with self.subTest(journal=label):
                before = _snapshot(self.userdata)
                journal = _wired(cls())
                with self.assertRaises(identity.IdentityError) as ctx:
                    chosen = self._choose(journal)
                    self.fail(f"{label} returned {chosen!r}; a unittest mock must not supply a resumed id")
                self.assertNotIn(SUPPLIED_ID, str(ctx.exception))
                self.assertEqual(_snapshot(self.userdata), before)
                self.assertFalse(self.persisted.exists())

    def test_mock_mapping_with_a_persisted_id_does_not_overwrite_it(self) -> None:
        self.persisted.write_text(CONFIGURED_ID + "\n", encoding="utf-8")
        for label, cls in MOCK_MAPPING_CLASSES.items():
            with self.subTest(journal=label):
                with self.assertRaises(identity.IdentityError):
                    self._choose(_wired(cls()))
                self.assertEqual(self.persisted.read_text(encoding="utf-8"), CONFIGURED_ID + "\n")

    def test_control_real_mapping_with_the_same_wiring_resumes(self) -> None:
        # Positive control: the failure above must come from the mock, not the values.
        chosen = self._choose(_RealMapping({"new_device_id": SUPPLIED_ID}))
        self.assertEqual(chosen, SUPPLIED_ID)
        self.assertFalse(self.persisted.exists())

    def test_control_spec_mapping_mock_stays_rejected(self) -> None:
        # Not a Mapping subclass, so this was already rejected; it must stay that way.
        journal = _wired(mock.MagicMock(spec=collections.abc.Mapping))
        with self.assertRaises(identity.IdentityError):
            self._choose(journal)
        self.assertFalse(self.persisted.exists())


if __name__ == "__main__":
    unittest.main()
