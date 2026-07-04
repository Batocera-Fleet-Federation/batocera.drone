import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.drone_api as drone_api
from app.drone_api import Settings
from app.overmind import saves_sync


class SavesSyncTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.userdata = Path(self._tmp.name)
        (self.userdata / "saves" / "snes").mkdir(parents=True)
        self._env = mock.patch.dict(
            "os.environ",
            {
                "USERDATA_ROOT": str(self.userdata),
                "SAVES_ROOT": str(self.userdata / "saves"),
                "OVERMIND_DEVICE_ID": "drone-test",
            },
            clear=True,
        )
        self._env.start()
        self.settings = Settings.from_env()
        drone_api._SAVES_PUSH_REQUESTED.clear()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _write_save(self, rel, data=b"save"):
        path = self.userdata / "saves" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def test_sync_scans_saves_locally_without_uploading(self):
        self._write_save("snes/Chrono Trigger.srm")
        result = drone_api._sync_saves_to_overmind(self.settings, "https://overmind.example", "tok")
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["reason"], "overmind_saves_disabled")
        self.assertTrue(result["thumbprint"])
        self.assertEqual(drone_api._local_saves_thumbprint(self.settings), "")

    def test_heartbeat_saves_thumbprint_never_queues_push(self):
        self._write_save("snes/A.srm")
        drone_api._sync_saves_to_overmind(self.settings, "https://o", "tok")
        for response in ({}, {"saves_files_thumbprint": ""}, {"saves_files_thumbprint": None}, {"saves_files_thumbprint": "different"}):
            drone_api._SAVES_PUSH_REQUESTED.set()
            drone_api._maybe_request_saves_push_from_heartbeat(self.settings, response)
            self.assertFalse(drone_api._SAVES_PUSH_REQUESTED.is_set(), f"saves echo should not queue: {response}")


if __name__ == "__main__":
    unittest.main()
