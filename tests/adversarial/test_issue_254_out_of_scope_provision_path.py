"""Regression coverage for Mapping journals through the provisioning path.

#254 is about _choose_device_id refusing a real Mapping. The real provisioning entry
point historically persisted the journal (_record_journal -> json) before it called
_choose_device_id. A UserDict then failed with a raw TypeError at the state-update
write, so the accepted-Mapping resume path was unreachable from _provision.

Issue #358 requires the real path to reach resumed-id validation; issue #355
requires supported Mapping journals to persist as JSON objects before that check.
"""

from __future__ import annotations

import collections
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from contextlib import ExitStack, contextmanager
from unittest import mock

from app.common import identity_provisioning as identity


CONFIGURED_ID = "AA:BB:CC:DD:EE:FF"
RESUMED_ID = "02:11:22:33:44:55"


@contextmanager
def _enter_all(managers):
    """Enter every patch and hand back the started mocks, in the same order."""
    with ExitStack() as stack:
        yield [stack.enter_context(manager) for manager in managers]


class ProvisionPathUserDictOutOfScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.userdata = Path(self._tmpdir.name).resolve() / "userdata"
        self.userdata.mkdir()
        self.settings = SimpleNamespace(userdata_root=self.userdata, device_id=CONFIGURED_ID)

    def _patched(self, choose_kwargs):
        """Stub the steps that run before the id check; they need real hardware or swarm state."""
        swarm_reset = mock.Mock()
        swarm_reset.reset_swarm_identity_state.return_value = {"incomplete": []}
        return [
            mock.patch.object(identity, "_revoke_inherited_access"),
            mock.patch.object(identity, "_publish"),
            mock.patch.object(identity, "_log"),
            mock.patch.object(identity, "_retire_old_identity"),
            mock.patch.object(identity, "_swarm_reset_module", return_value=swarm_reset),
            mock.patch.object(identity, "_choose_device_id", **choose_kwargs),
        ]

    def _resumed_journal(self, container):
        return container({
            "state": identity.State.CLONE_DETECTED.value,
            "reason": "clone",
            "started_at": "2026-01-01T00:00:00+00:00",
            "previous_certificate_fingerprint": "",
            "previous_device_id": CONFIGURED_ID,
            "previous_installation_id": "",
            "hardware": {},
            "steps": {},
            "new_device_id": RESUMED_ID,
        })

    def test_userdict_journal_reaches_the_id_check(self) -> None:
        journal = self._resumed_journal(collections.UserDict)
        with _enter_all(self._patched({"side_effect": identity.IdentityError("reached id check")})) as patches:
            choose = patches[-1]
            with self.assertRaisesRegex(identity.IdentityError, "reached id check"):
                identity._provision(self.settings, None, "clone", None, journal)
        choose.assert_called_once()

    def test_userdict_journal_persists_and_reaches_the_id_check(self) -> None:
        journal = self._resumed_journal(collections.UserDict)
        with _enter_all(self._patched({"return_value": RESUMED_ID})) as patches:
            choose = patches[-1]
            try:
                identity._provision(self.settings, None, "clone", None, journal)
            except Exception:  # noqa: BLE001 - later certificate work is outside this assertion
                pass
        choose.assert_called_once()
        stored = identity._read_json(identity._journal_path(self.settings))
        self.assertIsInstance(stored, dict)
        self.assertEqual(stored["new_device_id"], RESUMED_ID)

    def test_plain_dict_journal_reaches_the_id_check_on_the_same_path(self) -> None:
        # Positive control: the same wiring with a dict gets past the write to the id check.
        journal = self._resumed_journal(dict)
        with _enter_all(self._patched({"return_value": RESUMED_ID})) as patches:
            choose = patches[-1]
            try:
                identity._provision(self.settings, None, "clone", None, journal)
            except Exception:  # noqa: BLE001 - later steps need real hardware; only the call matters
                pass
        choose.assert_called_once()


if __name__ == "__main__":
    unittest.main()
