"""Out-of-scope #254 evidence: _read_json still requires a dict, not a Mapping.

Issue #254 accepts a real Mapping in _choose_device_id. The on-disk loader
(_read_json) still treats anything that is not isinstance(..., dict) as
corrupt, so a json.loads object_hook that yields UserDict never reaches the
id check. Production json.loads of a JSON object is always a dict, which is
why no production path hits the original UserDict probe.

This is outside #254 (the filed defect is the type-check vs 'must be a mapping'
message on _choose_device_id). Owner follow-up: should the loader accept any
Mapping, or is dict-only decoding the durable journal contract?

These assertions document the current load gate so the evidence is retained.
They are not a #254 acceptance gate.
"""

from __future__ import annotations

import collections
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity


RESUMED_ID = "02:11:22:33:44:55"
PAYLOAD = {"new_device_id": RESUMED_ID, "reason": "clone", "state": "provisioning"}


class ReadJsonDictGateOutOfScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.userdata = Path(self._tmpdir.name).resolve() / "userdata"
        self.userdata.mkdir()
        self.settings = SimpleNamespace(userdata_root=self.userdata, device_id="AA:BB:CC:DD:EE:FF")
        identity._record_journal(self.settings, dict(PAYLOAD))
        self.path = identity._journal_path(self.settings)

    def test_plain_loads_is_a_dict_and_not_corrupt(self) -> None:
        loaded = identity._read_json(self.path)
        self.assertIsInstance(loaded, dict)
        self.assertIsNot(loaded, identity._CORRUPT)
        self.assertEqual(loaded["new_device_id"], RESUMED_ID)

    def test_userdict_decoded_object_is_treated_as_corrupt(self) -> None:
        # Simulate json.loads(..., object_hook=UserDict) without wrapping the real decoder:
        # _read_json still requires isinstance(..., dict), which UserDict is not.
        decoded = collections.UserDict(PAYLOAD)
        self.assertFalse(isinstance(decoded, dict))
        with mock.patch.object(identity.json, "loads", return_value=decoded):
            loaded = identity._read_json(self.path)
        self.assertIs(loaded, identity._CORRUPT)

    def test_json_array_file_is_corrupt_even_though_loads_succeeds(self) -> None:
        self.path.write_text("[1, 2]", encoding="utf-8")
        self.assertIs(identity._read_json(self.path), identity._CORRUPT)


if __name__ == "__main__":
    unittest.main()
