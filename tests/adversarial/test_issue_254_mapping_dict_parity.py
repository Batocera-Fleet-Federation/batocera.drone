"""Issue #254 adversarial: a real Mapping journal must behave exactly like the same dict.

The #254 report is that a UserDict is refused while a dict with the same content is
accepted. The fix is correct only if every real Mapping gives the same outcome as a
dict for the same content, across well-formed, malformed, falsy, and mock-valued
resumed ids. This is a differential check: each journal class is run against the
same value corpus as a plain dict on an identical userdata tree.

Expected, derived from the issue and the #217/#252 resumed-value rules before
inspecting the patch:

- The outcome (resumed id, minted id, or IdentityError message) is the same for a
  dict and for each real Mapping with the same content.
- The persisted device-id file and the userdata tree end up the same for both.
- A minted id is a fresh MAC-shaped id, never the configured id.

random_device_id is pinned so that a mint is comparable between the two runs.
"""

from __future__ import annotations

import collections
import collections.abc
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity


CONFIGURED_ID = "AA:BB:CC:DD:EE:FF"
MINTED_ID = "02:00:00:00:00:01"
MAC_SHAPED = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


class _CustomMapping(collections.abc.Mapping):
    def __init__(self, data):
        self._data = dict(data)

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


class _UserDictSubclass(collections.UserDict):
    """A UserDict subclass is a real Mapping and must match the dict too."""


def _journal_values():
    """Values for new_device_id, each built fresh so mock values are not shared."""
    return {
        "well-formed": "02:11:22:33:44:55",
        "upper-case": "02:AA:BB:CC:DD:EE",
        "trailing-newline": "02:11:22:33:44:55\n",
        "surrounding-space": " 02:11:22:33:44:55 ",
        "whitespace-only": "   ",
        "too-short": "ab",
        "too-long": "x" * 129,
        "not-a-mac": "bad id with spaces!",
        "empty-string": "",
        "json-null": None,
        "zero": 0,
        "false": False,
        "true": True,
        "integer": 12345,
        "list": ["02:11:22:33:44:55"],
        "nested-dict": {"id": "02:11:22:33:44:55"},
        "magic-mock": mock.MagicMock(),
        "spec-dict-mock": mock.MagicMock(spec=dict),
    }


JOURNAL_FACTORIES = {
    "UserDict": lambda content: collections.UserDict(content),
    "UserDict subclass": lambda content: _UserDictSubclass(content),
    "MappingProxyType": lambda content: MappingProxyType(dict(content)),
    "custom Mapping": lambda content: _CustomMapping(content),
}


class _Tree(unittest.TestCase):
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
        pinned = mock.patch.object(identity, "random_device_id", return_value=MINTED_ID)
        pinned.start()
        self.addCleanup(pinned.stop)

    def _userdata_tree(self) -> dict:
        entries = {}
        for path in sorted(self.userdata.rglob("*")):
            key = path.relative_to(self.userdata).as_posix()
            entries[key] = path.read_bytes() if path.is_file() else None
        return entries

    def _run_choice(self, journal):
        """Run one choice and return (outcome, tree-after) so both sides compare fully."""
        if self.persisted.exists():
            self.persisted.unlink()
        self.persisted.write_text(CONFIGURED_ID + "\n", encoding="utf-8")
        try:
            chosen = identity._choose_device_id(self.settings, journal)
            outcome = ("returned", chosen)
        except identity.IdentityError as error:
            outcome = ("identity-error", str(error))
        except Exception as error:  # noqa: BLE001 - a leaked type is itself the finding
            outcome = ("leaked", type(error).__name__, str(error))
        return outcome, self._userdata_tree()


class MappingDictParityTests(_Tree):
    def test_every_real_mapping_matches_the_dict_outcome_for_each_value(self) -> None:
        for value_label in _journal_values():
            for factory_label, factory in JOURNAL_FACTORIES.items():
                with self.subTest(value=value_label, mapping=factory_label):
                    # Fresh values for each run so a mock's call state cannot leak.
                    dict_value = _journal_values()[value_label]
                    mapping_value = _journal_values()[value_label]
                    expected = self._run_choice({"new_device_id": dict_value})
                    actual = self._run_choice(factory({"new_device_id": mapping_value}))
                    self.assertNotEqual(expected[0][0], "leaked", msg="dict control leaked an exception")
                    self.assertEqual(actual[0], expected[0], msg=f"{factory_label} diverges from dict")
                    self.assertEqual(actual[1], expected[1], msg=f"{factory_label} changed the tree differently")

    def test_empty_real_mapping_matches_empty_dict_mint(self) -> None:
        expected = self._run_choice({})
        for factory_label, factory in JOURNAL_FACTORIES.items():
            with self.subTest(mapping=factory_label):
                actual = self._run_choice(factory({}))
                self.assertEqual(actual, expected)
                self.assertEqual(actual[0], ("returned", MINTED_ID))
                self.assertTrue(MAC_SHAPED.match(MINTED_ID))
                self.assertNotEqual(MINTED_ID, CONFIGURED_ID)

    def test_resumed_ids_never_stringify_a_mock_or_the_configured_id(self) -> None:
        for factory_label, factory in JOURNAL_FACTORIES.items():
            for value_label in ("magic-mock", "spec-dict-mock"):
                with self.subTest(mapping=factory_label, value=value_label):
                    outcome, _ = self._run_choice(factory({"new_device_id": _journal_values()[value_label]}))
                    self.assertNotEqual(outcome[0], "returned", msg=f"mock value resumed as {outcome!r}")


if __name__ == "__main__":
    unittest.main()
