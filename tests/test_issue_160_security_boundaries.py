"""Identity replacement must revoke live access before dropping ownership."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity
from tests.test_identity_provisioning import provider_for, settings_for
from tests.test_hardware_identity import UUID_B, x86_tree


class LiveResetBoundaries(unittest.TestCase):
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
