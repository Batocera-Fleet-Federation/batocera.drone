"""Issue #722: adversarial UATs that mentioned Reference ROMs must still collect.

Quoted requirement:
- "tests: `test_network_share_handlers.py`, `test_network_share_manager.py`, `test_nfs_export_manager.py`, plus references in `test_unit.py`, `test_integration_mock_server.py`, `test_openapi_contract.py`, `test_identity_provisioning.py`, `test_peer_connectivity_post.py` and the adversarial UATs that mention it."
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UAT_FILES = (
    "tests/adversarial/test_issue_615_create_server_shutdown_race_uat.py",
    "tests/adversarial/test_issue_615_main_finally_start_window_uat.py",
    "tests/adversarial/test_issue_615_poller_capture_uat.py",
)


class ReferenceRomAdversarialUatImports(unittest.TestCase):
    def test_adversarial_uats_that_mention_reference_roms_still_import(self) -> None:
        # "and the adversarial UATs that mention it."
        failures: list[str] = []
        for relative in UAT_FILES:
            path = ROOT / relative
            self.assertTrue(path.is_file(), relative)
            spec = importlib.util.spec_from_file_location(f"issue722_{path.stem}", path)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
            except Exception as error:
                failures.append(f"{relative}: {error.__class__.__name__}: {error}")
        self.assertEqual(failures, [], "adversarial UATs that mention Reference ROMs no longer import")


if __name__ == "__main__":
    unittest.main()
