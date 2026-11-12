"""Issue #696: a renamed peer must be relabelled from its own authenticated health reply."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.transfer.local_network as local_network
from app.common.settings import Settings


def _build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "rename-test-local",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class PeerNameRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = _build_settings(Path(self._tmp.name))
        with mock.patch.object(local_network, "_notifications"):
            local_network.save_paired_peer(
                self.settings,
                {
                    "drone_id": "peer-1",
                    "name": "batocera-backup1",
                    "hostname": "batocera-backup1",
                    "reachable_url": "https://192.168.1.20:8443",
                    "certificate_fingerprint": "ab" * 32,
                    "last_seen": "2026-01-01T00:00:00+00:00",
                },
            )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _stored(self) -> dict:
        return local_network.get_paired_peer(self.settings, "peer-1") or {}

    def test_renamed_peer_is_relabelled_and_pin_is_untouched(self) -> None:
        changed = local_network.refresh_paired_peer_name(self.settings, "peer-1", "property-cabinet-r5")
        self.assertTrue(changed)
        stored = self._stored()
        self.assertEqual(stored["name"], "property-cabinet-r5")
        self.assertEqual(stored["hostname"], "property-cabinet-r5")
        self.assertEqual(stored["reachable_url"], "https://192.168.1.20:8443")
        self.assertEqual(stored["certificate_fingerprint"], "ab" * 32)
        self.assertEqual(stored["last_seen"], "2026-01-01T00:00:00+00:00")

    def test_unchanged_or_unknown_names_are_not_written(self) -> None:
        self.assertFalse(local_network.refresh_paired_peer_name(self.settings, "peer-1", "batocera-backup1"))
        self.assertFalse(local_network.refresh_paired_peer_name(self.settings, "peer-1", ""))
        self.assertFalse(local_network.refresh_paired_peer_name(self.settings, "peer-1", None))
        self.assertFalse(local_network.refresh_paired_peer_name(self.settings, "not-paired", "anything"))
        self.assertEqual(self._stored()["name"], "batocera-backup1")
        self.assertIsNone(local_network.get_paired_peer(self.settings, "not-paired"))

    def test_hostile_names_are_rejected(self) -> None:
        hostile = [
            "<script>alert(1)</script>",
            "name\nwith-newline",
            "x" * 300,
            "../../etc/passwd",
            "-leading-dash",
            "has space",
            {"nested": "object"},
        ]
        for value in hostile:
            self.assertFalse(local_network.refresh_paired_peer_name(self.settings, "peer-1", value), value)
        self.assertEqual(self._stored()["name"], "batocera-backup1")

    def test_clean_peer_display_name_accepts_hostname_shapes(self) -> None:
        self.assertEqual(local_network.clean_peer_display_name("  BATOCERA-R5  "), "BATOCERA-R5")
        self.assertEqual(local_network.clean_peer_display_name("box.local"), "box.local")
        self.assertEqual(local_network.clean_peer_display_name(""), "")


if __name__ == "__main__":
    unittest.main()
