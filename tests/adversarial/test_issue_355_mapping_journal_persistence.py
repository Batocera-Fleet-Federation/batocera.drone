"""Acceptance tests for #355: journal writers must accept real Mapping objects.

Provisioning journals are JSON objects on disk.  Callers that construct or adapt a
journal may provide any ordinary ``collections.abc.Mapping`` implementation, so
recording it must produce the same JSON object as recording an equivalent dict.
"""

from __future__ import annotations

import collections
import json
import tempfile
import types
import unittest
from pathlib import Path
from types import MappingProxyType

from app.common import identity_provisioning as identity


class MappingJournalPersistenceUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="issue-355-journal-")
        self.addCleanup(self.tmp.cleanup)
        self.settings = types.SimpleNamespace(userdata_root=self.tmp.name)
        self.expected = {
            "state": "provisioning",
            "reason": "clone",
            "new_device_id": "02:11:22:33:44:55",
        }

    def _assert_persisted_as_json_object(self, journal) -> None:
        path = identity._journal_path(self.settings)
        identity._record_journal(self.settings, journal)

        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), self.expected)
        self.assertFalse(
            [candidate for candidate in path.parent.iterdir() if candidate.name.endswith(".tmp")],
            "successful atomic persistence must not leave a temporary journal behind",
        )

    def test_userdict_is_persisted_as_its_mapping_contents(self) -> None:
        self._assert_persisted_as_json_object(collections.UserDict(self.expected))

    def test_chainmap_is_persisted_with_normal_mapping_lookup_precedence(self) -> None:
        journal = collections.ChainMap(
            {"state": "provisioning", "reason": "clone"},
            {"new_device_id": "02:11:22:33:44:55"},
        )
        self._assert_persisted_as_json_object(journal)

    def test_mappingproxy_is_persisted_as_its_mapping_contents(self) -> None:
        self._assert_persisted_as_json_object(MappingProxyType(self.expected))

    def test_mapping_failure_does_not_replace_existing_journal(self) -> None:
        path = identity._journal_path(self.settings)
        identity._record_journal(self.settings, self.expected)
        original = path.read_bytes()

        # The root is a valid mapping, but its value cannot be represented in JSON.
        # Failed writes must preserve the prior durable recovery record.
        with self.assertRaises(TypeError):
            identity._record_journal(self.settings, {"state": object()})

        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(
            [candidate for candidate in path.parent.iterdir() if candidate.name.endswith(".tmp")],
            "a rejected journal must not leave a temporary file behind",
        )


if __name__ == "__main__":
    unittest.main()
