"""Issue #358: provisioning must validate an accepted Mapping journal.

The identity journal is durable JSON, so files read by _read_json are plain dicts.
However, _choose_device_id explicitly defines real Mapping implementations as valid
journals. When _provision is given such a journal, serialization must not short-circuit
the resumed-id validation with a raw json TypeError. Malformed resumed ids must fail
closed through IdentityError before identity replacement proceeds.
"""

from __future__ import annotations

import collections
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity


class MappingJournalProvisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.settings = SimpleNamespace(
            userdata_root=self.root,
            device_id="AA:BB:CC:DD:EE:FF",
        )

    def test_non_dict_mappings_reach_resumed_id_validation_before_generating_a_new_identity(self) -> None:
        def payload():
            return {
                "state": "PROVISIONING",
                "reason": "clone",
                "steps": {},
                # Hyphens are valid in Drone ids. An empty present value is not: it must
                # reach _choose_device_id's resumed-id validation rather than fail at
                # the preceding JSON journal write.
                "new_device_id": "",
            }

        journal_factories = {
            "UserDict": lambda: collections.UserDict(payload()),
            "ChainMap": lambda: collections.ChainMap(payload()),
            "MappingProxyType": lambda: MappingProxyType(payload()),
        }
        for label, make_journal in journal_factories.items():
            with self.subTest(journal=label):
                with mock.patch.object(identity, "_revoke_inherited_access"), \
                     mock.patch.object(identity, "_retire_old_identity"), \
                     mock.patch.object(identity, "_swarm_reset_module") as reset_module, \
                     mock.patch.object(identity, "_record_journal", wraps=identity._record_journal) as record_journal, \
                     mock.patch.object(identity, "_reset_tailnet") as reset_tailnet, \
                     mock.patch.object(identity, "_certificate_manager") as certificate_manager, \
                     mock.patch.object(identity, "_choose_device_id", wraps=identity._choose_device_id) as choose_device_id:
                    reset_module.return_value.reset_swarm_identity_state.return_value = {"incomplete": []}
                    with self.assertRaisesRegex(identity.IdentityError, "journaled device id") as caught:
                        identity._provision(self.settings, None, "clone", None, make_journal())

                self.assertNotIsInstance(caught.exception, TypeError)
                choose_device_id.assert_called_once()
                self.assertTrue(record_journal.called)
                self.assertTrue(all(isinstance(call.args[1], dict) for call in record_journal.call_args_list))
                reset_tailnet.assert_not_called()
                certificate_manager.assert_not_called()


if __name__ == "__main__":
    unittest.main()
