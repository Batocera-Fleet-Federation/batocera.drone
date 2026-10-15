"""Issue #254 UAT: a journal.json object decoded as a real Mapping must resume.

The #254 report constructed UserDict by hand. The only stdlib path that can
actually produce a UserDict from identity/journal.json is json.loads with
object_pairs_hook (or object_hook). Production _read_json uses plain
json.loads and therefore yields a dict; wrapping that dict in UserDict /
MappingProxyType is the same type-check contract as the original probe.

Expected, derived from the issue and the owner decision that a real Mapping
is a journal, before relying on the patch:

- json.loads of a well-formed journal object is a dict and still resumes.
- The same bytes decoded with object_pairs_hook=UserDict (or object_hook)
  resume the same id and do not raise 'must be a mapping'.
- MappingProxyType around the loaded dict also resumes (read-only Mapping).
- Extra journal fields (reason/state/steps/hardware) do not change the type
  check: a UserDict carrying a full clone journal still resumes.
- A JSON array or JSON string is not a Mapping and still raises the mapping
  error; decoding those as UserDict is not a backdoor.
"""

from __future__ import annotations

import collections
import json
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

FULL_JOURNAL = {
    "state": "provisioning",
    "reason": "clone",
    "started_at": "2026-01-01T00:00:00+00:00",
    "previous_certificate_fingerprint": "a" * 64,
    "previous_device_id": CONFIGURED_ID,
    "previous_installation_id": "install-1",
    "hardware": {"version": 1, "components": {}},
    "steps": {"retired": True},
    "new_device_id": RESUMED_ID,
}


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


class JsonDecoderMappingUatTests(_Sandbox):
    def test_plain_json_loads_dict_from_journal_file_still_resumes(self) -> None:
        identity._record_journal(self.settings, dict(FULL_JOURNAL))
        loaded = identity._read_json(identity._journal_path(self.settings))
        self.assertIsInstance(loaded, dict)
        self.assertNotIsInstance(loaded, collections.UserDict)
        self.assertEqual(self._choose(loaded), RESUMED_ID)
        self.assertFalse(self.persisted.exists())

    def test_object_pairs_hook_userdict_from_the_same_bytes_resumes(self) -> None:
        text = json.dumps(FULL_JOURNAL)
        journal = json.loads(text, object_pairs_hook=collections.UserDict)
        self.assertIsInstance(journal, collections.UserDict)
        self.assertEqual(self._choose(journal), RESUMED_ID)
        self.assertFalse(self.persisted.exists())

    def test_object_hook_userdict_from_the_same_bytes_resumes(self) -> None:
        text = json.dumps(FULL_JOURNAL)
        journal = json.loads(text, object_hook=collections.UserDict)
        self.assertIsInstance(journal, collections.UserDict)
        chosen = self._choose(journal)
        self.assertEqual(chosen, RESUMED_ID)
        self.assertNotEqual(chosen, CONFIGURED_ID)

    def test_mappingproxy_around_the_loaded_dict_resumes(self) -> None:
        loaded = json.loads(json.dumps(FULL_JOURNAL))
        self.assertEqual(self._choose(MappingProxyType(loaded)), RESUMED_ID)
        self.assertFalse(self.persisted.exists())

    def test_userdict_wrap_of_read_json_does_not_raise_must_be_a_mapping(self) -> None:
        identity._record_journal(self.settings, dict(FULL_JOURNAL))
        loaded = identity._read_json(identity._journal_path(self.settings))
        try:
            chosen = self._choose(collections.UserDict(loaded))
        except identity.IdentityError as error:
            self.fail(
                f"UserDict wrap of a loaded journal raised IdentityError({error!s}); "
                "a real Mapping must resume"
            )
        self.assertEqual(chosen, RESUMED_ID)

    def test_json_array_payload_is_still_not_a_journal(self) -> None:
        for payload in (
            json.loads("[]"),
            json.loads('[{"new_device_id": "%s"}]' % RESUMED_ID),
            json.loads('"%s"' % RESUMED_ID),
        ):
            with self.subTest(payload=type(payload).__name__):
                with self.assertRaisesRegex(identity.IdentityError, JOURNAL_MUST_BE_MAPPING):
                    self._choose(payload)
                self.assertFalse(self.persisted.exists())

    def test_object_pairs_hook_cannot_launder_a_json_array_into_a_resume(self) -> None:
        # object_pairs_hook is only consulted for JSON objects. An array stays a list.
        payload = json.loads(
            '[{"new_device_id": "%s"}]' % RESUMED_ID,
            object_pairs_hook=collections.UserDict,
        )
        self.assertIsInstance(payload, list)
        with self.assertRaisesRegex(identity.IdentityError, JOURNAL_MUST_BE_MAPPING):
            self._choose(payload)


if __name__ == "__main__":
    unittest.main()
