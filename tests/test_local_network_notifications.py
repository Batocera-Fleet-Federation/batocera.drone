"""Regression tests for the swarm_peer_connected notification hook in
save_paired_peer() -- kept in a small, focused file rather than folded into
a full local_network test suite (none exists yet) since this only needs to
verify the is_new detection, not the rest of local_network.py's behavior.
"""

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
        "DRONE_DEVICE_ID": "local-network-test",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class SavePairedPeerNotificationTests(unittest.TestCase):
    def test_fires_once_for_a_genuinely_new_peer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch.object(local_network, "_notifications") as fake_notifications:
                local_network.save_paired_peer(settings, {"drone_id": "peer-1", "name": "Peer One"})
            fake_notifications.record_event.assert_called_once()
            self.assertEqual(fake_notifications.record_event.call_args[0][1], "swarm_peer_connected")

    def test_does_not_fire_again_on_a_routine_refresh_of_the_same_peer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            local_network.save_paired_peer(settings, {"drone_id": "peer-1", "name": "Peer One"})
            with mock.patch.object(local_network, "_notifications") as fake_notifications:
                # Same peer_id saved again -- e.g. a LAN-discovery beacon
                # refreshing last_seen/address on an already-paired peer.
                local_network.save_paired_peer(settings, {"drone_id": "peer-1", "name": "Peer One", "local_ip": "192.168.1.50"})
            fake_notifications.record_event.assert_not_called()

    def test_a_second_distinct_peer_fires_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch.object(local_network, "_notifications") as fake_notifications:
                local_network.save_paired_peer(settings, {"drone_id": "peer-1", "name": "Peer One"})
                local_network.save_paired_peer(settings, {"drone_id": "peer-2", "name": "Peer Two"})
            self.assertEqual(fake_notifications.record_event.call_count, 2)


if __name__ == "__main__":
    unittest.main()
