import contextlib
import io
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

    def test_sync_uploads_changed_saves_then_skips_when_unchanged(self):
        self._write_save("snes/Chrono Trigger.srm")
        calls = []

        def fake_post(url, payload, token=None, settings=None, timeout_seconds=None):
            calls.append((url, payload))
            return 200, {"saves_count": len(payload.get("saves") or [])}

        with mock.patch.object(saves_sync, "_overmind_post_json_with_status", fake_post):
            first = drone_api._sync_saves_to_overmind(self.settings, "https://overmind.example", "tok")
            self.assertEqual(first["status"], "ok")
            self.assertEqual(first["upserts"], 1)
            self.assertEqual(len(calls), 1)
            url, payload = calls[0]
            self.assertTrue(url.endswith("/api/devices/drone-test/rom-metadata"))
            self.assertEqual(payload["update_mode"], "inventory_delta")
            self.assertEqual(payload["saves"][0]["file_path"], "snes/Chrono Trigger.srm")
            self.assertTrue(payload["saves_files_thumbprint"])

            # Second call with no changes uploads nothing.
            calls.clear()
            second = drone_api._sync_saves_to_overmind(self.settings, "https://overmind.example", "tok")
            self.assertEqual(second["status"], "unchanged")
            self.assertEqual(calls, [])

    def test_deleted_save_is_sent_in_delta(self):
        self._write_save("snes/A.srm")
        with mock.patch.object(saves_sync, "_overmind_post_json_with_status", lambda *a, **k: (200, {})):
            drone_api._sync_saves_to_overmind(self.settings, "https://o", "tok")
        (self.userdata / "saves" / "snes" / "A.srm").unlink()
        captured = {}

        def fake_post(url, payload, token=None, settings=None, timeout_seconds=None):
            captured["payload"] = payload
            return 200, {}

        with mock.patch.object(saves_sync, "_overmind_post_json_with_status", fake_post):
            result = drone_api._sync_saves_to_overmind(self.settings, "https://o", "tok")
        self.assertEqual(result["deletes"], 1)
        self.assertEqual(captured["payload"]["deleted"]["saves"][0]["file_path"], "snes/A.srm")

    def test_heartbeat_thumbprint_helper_reflects_synced_state(self):
        self._write_save("snes/A.srm")
        with mock.patch.object(saves_sync, "_overmind_post_json_with_status", lambda *a, **k: (200, {})):
            drone_api._sync_saves_to_overmind(self.settings, "https://o", "tok")
        self.assertTrue(drone_api._local_saves_thumbprint(self.settings))

    def test_heartbeat_mismatch_queues_saves_push(self):
        self._write_save("snes/A.srm")
        with mock.patch.object(saves_sync, "_overmind_post_json_with_status", lambda *a, **k: (200, {})):
            drone_api._sync_saves_to_overmind(self.settings, "https://o", "tok")
        drone_api._SAVES_PUSH_REQUESTED.clear()
        # Overmind echoes a different saves thumbprint -> a resync push is queued.
        drone_api._maybe_request_saves_push_from_heartbeat(self.settings, {"saves_files_thumbprint": "different-thumb"})
        self.assertTrue(drone_api._SAVES_PUSH_REQUESTED.is_set())
        drone_api._SAVES_PUSH_REQUESTED.clear()
        # Matching thumbprint -> no push.
        local = drone_api._local_saves_thumbprint(self.settings)
        drone_api._maybe_request_saves_push_from_heartbeat(self.settings, {"saves_files_thumbprint": local})
        self.assertFalse(drone_api._SAVES_PUSH_REQUESTED.is_set())

    def test_sync_logs_trigger_reason(self):
        # The trigger log must explain WHY a saves sync fired (or was skipped).
        self._write_save("snes/A.srm")
        with mock.patch.object(saves_sync, "_overmind_post_json_with_status", lambda *a, **k: (200, {})):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                drone_api._sync_saves_to_overmind(self.settings, "https://o", "tok")
            first = buf.getvalue()
            self.assertIn("Saves sync trigger:", first)
            self.assertIn("will_upload=True", first)
            self.assertIn("changed_saves=", first)  # first scan has pending saves

            # Second run with nothing changed -> skip, reasons=none.
            drone_api._SAVES_PUSH_REQUESTED.clear()
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                result = drone_api._sync_saves_to_overmind(self.settings, "https://o", "tok")
            self.assertEqual(result["status"], "unchanged")
            self.assertIn("Saves sync trigger: will_upload=False reasons=none", buf2.getvalue())

    def test_empty_overmind_thumbprint_does_not_queue_push(self):
        # An Overmind that doesn't echo a saves thumbprint must NOT be treated as drift,
        # or the drone re-pushes the full saves set on every heartbeat (the resync loop).
        self._write_save("snes/A.srm")
        with mock.patch.object(saves_sync, "_overmind_post_json_with_status", lambda *a, **k: (200, {})):
            drone_api._sync_saves_to_overmind(self.settings, "https://o", "tok")
        drone_api._SAVES_PUSH_REQUESTED.clear()
        for response in ({}, {"saves_files_thumbprint": ""}, {"saves_files_thumbprint": None}):
            drone_api._maybe_request_saves_push_from_heartbeat(self.settings, response)
            self.assertFalse(drone_api._SAVES_PUSH_REQUESTED.is_set(), f"empty echo should not queue: {response}")


if __name__ == "__main__":
    unittest.main()
