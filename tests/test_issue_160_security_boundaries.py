"""Identity replacement must revoke live access before dropping ownership."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity
from app.common.auth import SessionStore
from app.device import nfs_export_manager
from app.storage.state_store import create_session, database_path, load_payload, save_payload
from app.transfer import swarm_reset
from tests.test_identity_provisioning import provider_for, settings_for
from tests.test_hardware_identity import UUID_B, x86_tree


class LiveResetBoundaries(unittest.TestCase):
    def test_absent_kernel_export_allows_imaged_nfs_record_to_clear(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = SimpleNamespace(userdata_root=Path(temporary))
            db = database_path(settings.userdata_root)
            save_payload(db, "nfs_export_manager.json", {"peers": {"peer": {"addresses": ["192.168.1.8"]}}})
            with mock.patch.object(nfs_export_manager, "_unexport_client", return_value="not exported"), \
                    mock.patch.object(nfs_export_manager, "_find_command", return_value="/usr/sbin/exportfs"), \
                    mock.patch.object(nfs_export_manager, "_run", return_value=SimpleNamespace(returncode=0, stdout="")), \
                    mock.patch.object(nfs_export_manager, "_cleanup_bind_mounts"), \
                    mock.patch.object(swarm_reset, "_revoke_shared_credentials", return_value=[]):
                swarm_reset.reset_swarm_identity_state(settings)
            self.assertIsNone(load_payload(db, "nfs_export_manager.json", None))

    def test_failed_nfs_revocation_keeps_records_and_blocks_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = SimpleNamespace(userdata_root=Path(temporary))
            db = database_path(settings.userdata_root)
            save_payload(db, "nfs_export_manager.json", {"peers": {"peer": {"addresses": ["192.168.1.8"]}}})
            token = "old-session"
            create_session(db, token, "admin", (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
            with mock.patch("app.device.nfs_export_manager._unexport_client", return_value="denied"), \
                    mock.patch("app.device.nfs_export_manager._cleanup_bind_mounts"), \
                    mock.patch.object(swarm_reset, "_revoke_shared_credentials", return_value=[]):
                with self.assertRaisesRegex(RuntimeError, "could not revoke inherited NFS export"):
                    swarm_reset.reset_swarm_identity_state(settings)
            self.assertIn("peer", load_payload(db, "nfs_export_manager.json", {})["peers"])
            self.assertEqual(SessionStore(db).validate(token), "admin")
            with mock.patch("app.device.nfs_export_manager._unexport_client", return_value=None), \
                    mock.patch("app.device.nfs_export_manager._cleanup_bind_mounts"), \
                    mock.patch.object(swarm_reset, "_revoke_shared_credentials", return_value=[]):
                swarm_reset.reset_swarm_identity_state(settings)
            self.assertIsNone(load_payload(db, "nfs_export_manager.json", None))
            self.assertIsNone(SessionStore(db).validate(token))

    def test_tailnet_preflight_requires_a_verified_ready_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            disk = root / "disk"
            disk.mkdir()
            hw_a = x86_tree(root / "hw-a")
            hw_b = x86_tree(root / "hw-b", uuid=UUID_B, board="BOARDSERIAL02")
            settings = settings_for(disk)
            identity.reset_runtime_state_for_tests()
            self.addCleanup(identity.reset_runtime_state_for_tests)
            self.assertFalse(identity.tailnet_start_permitted(settings, provider_for(hw_a)))
            identity.ensure_identity(settings, provider_for(hw_a))
            self.assertTrue(identity.tailnet_start_permitted(settings, provider_for(hw_a)))
            self.assertFalse(identity.tailnet_start_permitted(settings, provider_for(hw_b)))
            identity._atomic_write_json(identity._journal_path(settings), {"state": "PROVISIONING"})
            self.assertFalse(identity.tailnet_start_permitted(settings, provider_for(hw_a)))
            identity._journal_path(settings).unlink()
            identity._atomic_write_json(identity._recovery_path(settings), {"reason": "insufficient_evidence"})
            self.assertFalse(identity.tailnet_start_permitted(settings, provider_for(hw_a)))

    def test_failed_tailnet_reset_retry_stays_held(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            disk = root / "disk"
            disk.mkdir()
            hardware = x86_tree(root / "hw")
            settings = settings_for(disk)
            identity.reset_runtime_state_for_tests()
            self.addCleanup(identity.reset_runtime_state_for_tests)
            identity.ensure_identity(settings, provider_for(hardware))
            binding_path = identity._binding_path(settings)
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
            binding["tailnet_reset_pending"] = True
            identity._atomic_write_json(binding_path, binding)
            self.assertFalse(identity.tailnet_start_permitted(settings, provider_for(hardware)))
            with mock.patch.object(identity, "_reset_tailnet", return_value={"status": "error"}), \
                    mock.patch.object(identity, "_tailnet_module"):
                result = identity.ensure_identity(settings, provider_for(hardware))
            self.assertEqual(result.state, identity.State.RECOVERY_REQUIRED)
            self.assertFalse(identity.swarm_networking_allowed())
            self.assertTrue(json.loads(binding_path.read_text(encoding="utf-8"))["tailnet_reset_pending"])


if __name__ == "__main__":
    unittest.main()
