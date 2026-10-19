"""Issue #358: standard Mapping journals preserve a valid resumed device id.

The durable journal format is JSON, but the provisioning helper accepts genuine
``Mapping`` implementations.  A non-dict mapping with a valid resumed id must
be snapshotted for persistence and then resume that exact id; it must neither
raise the old JSON ``TypeError`` nor mint a replacement id.
"""

from __future__ import annotations

import collections
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from unittest import mock

from app.common import identity_provisioning as identity


@dataclass
class _Settings:
    userdata_root: Path
    device_id: str


class MappingJournalResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = _Settings(
            userdata_root=Path(self._tmp.name).resolve(),
            device_id="AA:BB:CC:DD:EE:FF",
        )

    def test_valid_mapping_journal_resumes_its_id_without_minting_a_replacement(self) -> None:
        resumed_id = "02:11:22:33:44:55"
        payload = {
            "state": identity.State.CLONE_DETECTED.value,
            "reason": "clone",
            "steps": {},
            "new_device_id": resumed_id,
        }
        factories = {
            "UserDict": lambda: collections.UserDict(payload),
            "ChainMap": lambda: collections.ChainMap(payload),
            "MappingProxyType": lambda: MappingProxyType(payload),
        }

        for label, make_journal in factories.items():
            with self.subTest(journal=label):
                reset = mock.Mock()
                reset.reset_swarm_identity_state.return_value = {"incomplete": []}
                with mock.patch.object(identity, "_revoke_inherited_access"), \
                     mock.patch.object(identity, "_retire_old_identity"), \
                     mock.patch.object(identity, "_swarm_reset_module", return_value=reset), \
                     mock.patch.object(identity, "_record_journal", wraps=identity._record_journal) as record, \
                     mock.patch.object(identity, "random_device_id") as random_id, \
                     mock.patch.object(
                         identity,
                         "_reset_tailnet",
                         side_effect=identity.IdentityError("stop after resumed-id validation"),
                     ):
                    with self.assertRaisesRegex(identity.IdentityError, "stop after resumed-id validation") as caught:
                        identity._provision(self.settings, None, "clone", None, make_journal())

                self.assertNotIsInstance(caught.exception, TypeError)
                random_id.assert_not_called()
                self.assertTrue(record.called)
                self.assertTrue(all(isinstance(call.args[1], dict) for call in record.call_args_list))
                persisted = identity._read_json(identity._journal_path(self.settings))
                self.assertEqual(persisted["new_device_id"], resumed_id)

    def test_malformed_mapping_id_reaches_validation_not_json_serialization(self) -> None:
        """A present but malformed id is an invalid attempted resume, not a new id."""
        journal = collections.UserDict(
            {
                "state": identity.State.CLONE_DETECTED.value,
                "reason": "clone",
                "steps": {},
                "new_device_id": "not a device id",
            }
        )
        reset = mock.Mock()
        reset.reset_swarm_identity_state.return_value = {"incomplete": []}
        with mock.patch.object(identity, "_revoke_inherited_access"), \
             mock.patch.object(identity, "_retire_old_identity"), \
             mock.patch.object(identity, "_swarm_reset_module", return_value=reset), \
             mock.patch.object(identity, "random_device_id") as random_id:
            with self.assertRaisesRegex(identity.IdentityError, "journaled device id") as caught:
                identity._provision(self.settings, None, "clone", None, journal)

        self.assertNotIsInstance(caught.exception, TypeError)
        random_id.assert_not_called()


if __name__ == "__main__":
    unittest.main()
