"""Issue #160: a clone cannot become ready while copied swarm secrets remain usable."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import identity_provisioning as identity
from app.common.hardware_identity import HardwareIdentityProvider
from app.common.settings import Settings
from app.device import tailnet_service
from app.storage.state_store import database_path, load_payload, save_payload


def _settings(root):
    with mock.patch.dict(os.environ, {
        "USERDATA_ROOT": str(root),
        "TLS_SELF_SIGNED_DIR": str(root / "system" / "certs"),
    }, clear=True):
        return Settings.from_env()


def _hardware(root, uuid, serial):
    for name, value in (("product_uuid", uuid), ("board_serial", serial)):
        path = root / "sys/class/dmi/id" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n", encoding="utf-8")
    return HardwareIdentityProvider(root, container_probe=lambda: False, machine="x86_64")


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class CloneCleanupSafetyUAT(unittest.TestCase):
    def setUp(self):
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        temporary = tempfile.TemporaryDirectory(prefix="issue-160-cleanup-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source_hardware = _hardware(
            self.root / "hardware-source", "8e53b2f0-e3a4-43fd-b4c2-63d978ab1260", "SOURCE-BOARD-8765"
        )
        self.clone_hardware = _hardware(
            self.root / "hardware-clone", "50b6437a-d7c3-45e2-8d26-bbb7069c1234", "CLONE-BOARD-4321"
        )
        self.source = _settings(self.root / "source")
        self.assertEqual(identity.ensure_identity(self.source, self.source_hardware).state, identity.State.READY)

    def _clone(self):
        target = self.root / "clone"
        shutil.copytree(self.source.userdata_root, target)
        return _settings(target)

    def test_failed_inherited_tailnet_key_revocation_blocks_ready_and_retries(self):
        """The copied enrollment key must be cleared before swarm networking resumes."""
        save_payload(database_path(self.source.userdata_root), "tailnet_sharing.json", {
            "auth_key": "tskey-copied-from-source", "sharing_enabled": True,
            "source_peer_id": "peer-from-old-swarm",
        })
        clone = self._clone()
        with mock.patch.object(tailnet_service, "_revoke_local_sharing", side_effect=OSError("read-only state")):
            failed = identity.ensure_identity(clone, self.clone_hardware)

        self.assertEqual(failed.state, identity.State.RECOVERY_REQUIRED)
        self.assertFalse(identity.swarm_networking_allowed())
        self.assertTrue(identity._journal_path(clone).exists())
        self.assertEqual(load_payload(database_path(clone.userdata_root), "tailnet_sharing.json", {})["auth_key"],
                         "tskey-copied-from-source")

        identity.reset_runtime_state_for_tests()
        retried = identity.ensure_identity(_settings(clone.userdata_root), self.clone_hardware)
        self.assertEqual(retried.state, identity.State.READY)
        self.assertEqual(load_payload(database_path(clone.userdata_root), "tailnet_sharing.json", {})["auth_key"], "")

    def test_unstoppable_tailnet_daemon_without_successful_down_blocks_clone(self):
        """A failed `tailscale down` cannot be reported as a safe offline hold."""
        clone = self._clone()
        fake_cli = self.root / "tailscale"
        fake_cli.write_text("fixture only", encoding="utf-8")
        with mock.patch.object(tailnet_service, "TAILSCALE_CLI", fake_cli), \
                mock.patch.object(tailnet_service, "_stop_tailscaled", return_value=False), \
                mock.patch.object(tailnet_service, "hold_local_node", return_value=False) as hold:
            failed = identity.ensure_identity(clone, self.clone_hardware)

        self.assertTrue(hold.called)
        self.assertEqual(failed.state, identity.State.RECOVERY_REQUIRED)
        self.assertFalse(identity.swarm_networking_allowed())
        self.assertTrue(identity._journal_path(clone).exists())


if __name__ == "__main__":
    unittest.main()
