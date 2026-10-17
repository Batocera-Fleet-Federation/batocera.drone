"""UAT for issue #342: LAN state writes belong to their injected Settings snapshot.

The state-database environment variable selects a database while ``Settings`` is
created.  A LAN worker can outlive the test harness that created it, so a later
harness is allowed to replace that process environment without redirecting the
older worker's peer-discovery writes.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.storage import state_store
from app.transfer import local_network


class LanWorkerStateDatabaseInjectionUAT(unittest.TestCase):
    def test_discovery_write_keeps_settings_database_after_env_changes(self) -> None:
        """A later harness's environment must not receive this worker's peer row."""
        with tempfile.TemporaryDirectory(prefix="issue-342-") as temporary:
            root = Path(temporary)
            worker_database = root / "worker-state.sqlite3"
            later_harness_database = root / "later-harness.sqlite3"
            environment = {
                "USERDATA_ROOT": str(root / "userdata"),
                "DRONE_DEVICE_ID": "issue-342-worker",
                "DRONE_STATE_DATABASE_FILE": str(worker_database),
            }
            with mock.patch.dict(os.environ, environment, clear=True):
                settings = Settings.from_env()

            # The configuration is the worker's injected ownership boundary,
            # rather than a deferred lookup of mutable process state.
            self.assertEqual(settings.state_database_file, worker_database.resolve())

            with mock.patch.dict(
                os.environ,
                {"DRONE_STATE_DATABASE_FILE": str(later_harness_database)},
                clear=True,
            ):
                recorded = local_network.record_discovered_peer(
                    settings,
                    {
                        "service": local_network.DISCOVERY_SERVICE,
                        "drone_id": "issue-342-peer",
                        "name": "Issue 342 peer",
                        "scheme": "https",
                        "api_port": 8543,
                    },
                    source_ip="198.51.100.42",
                )

            self.assertIsNotNone(recorded)
            peers = state_store.load_payload(worker_database, "local_discovered_peers", {})
            self.assertIn("issue-342-peer", peers)
            self.assertEqual(peers["issue-342-peer"]["source_ip"], "198.51.100.42")
            self.assertTrue(worker_database.is_file())
            self.assertFalse(later_harness_database.exists())


if __name__ == "__main__":
    unittest.main()
