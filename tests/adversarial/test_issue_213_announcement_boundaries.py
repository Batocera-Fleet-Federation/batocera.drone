"""Issue #213: LAN claims never acquire authority over previously paired routes.

Legacy/sparse paired rows are valid: the peer client supports missing dedicated
ports. Absence of a trusted field is not permission for a LAN sender to set it.
Compare resolved destinations as well as DB rows because the admin pair action
uses discovered rows and resolves ports independently of reachable_url.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer import local_network, peer_connectivity


class AnnouncementBoundaries(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="issue-213-discovery-")
        self.addCleanup(temp.cleanup)
        with mock.patch.dict("os.environ", {
            "USERDATA_ROOT": temp.name, "DRONE_DEVICE_ID": "client-213",
            "ROMS_ROOT": str(Path(temp.name) / "roms"),
            "BIOS_ROOT": str(Path(temp.name) / "bios"),
            "SAVES_ROOT": str(Path(temp.name) / "saves"),
        }, clear=True):
            self.settings = Settings.from_env()

    def announce(self, **updates):
        return local_network.record_discovered_peer(self.settings, {
            "service": local_network.DISCOVERY_SERVICE, "drone_id": "target-213",
            "name": "Untrusted name", "hostname": "attacker.local",
            "scheme": "http", "api_port": 8080, "peer_mtls_port": 9090,
            "reachable_url": "http://attacker.local:8080",
            "certificate_fingerprint": "bb" * 32,
            "tailnet_ip": "100.64.0.66", **updates,
        }, "192.0.2.66")

    def test_complete_paired_record_only_refreshes_liveness(self):
        before = local_network.save_paired_peer(self.settings, {
            "drone_id": "target-213", "reachable_url": "https://192.0.2.20:7443",
            "advertised_reachable_url": "https://target.local:7443",
            "scheme": "https", "api_port": 7443, "peer_mtls_port": 8443,
            "tailnet_ip": "100.64.0.20", "certificate_fingerprint": "aa" * 32,
            "source_ip": "192.0.2.20", "name": "Target", "hostname": "target.local",
        })
        with mock.patch.object(local_network, "_now_iso", return_value="2030-01-01T00:00:00+00:00"):
            discovered = self.announce()
        after = local_network.get_paired_peer(self.settings, "target-213")
        self.assertEqual(after, {**before, "last_seen": "2030-01-01T00:00:00+00:00"})
        self.assertEqual(peer_connectivity._peer_address(discovered), peer_connectivity._peer_address(before))
        self.assertEqual(discovered["tailnet_ip"], before["tailnet_ip"])
        self.assertEqual(discovered["certificate_fingerprint"], before["certificate_fingerprint"])

    def test_legacy_paired_record_does_not_accept_attacker_ports(self):
        before = local_network.save_paired_peer(self.settings, {
            "drone_id": "target-213", "reachable_url": "https://192.0.2.20",
            "certificate_fingerprint": "aa" * 32,
        })
        discovered = self.announce()
        self.assertEqual(peer_connectivity._peer_address(discovered), peer_connectivity._peer_address(before),
                         "discovery supplied absent ports/scheme and changed the admin pairing destination")

    def test_pre_pair_discovery_tailnet_route_never_becomes_paired_authority(self):
        self.announce(tailnet_ip="100.64.0.66")
        local_network.save_paired_peer(self.settings, {
            "drone_id": "target-213", "reachable_url": "https://192.0.2.20",
            "scheme": "https", "api_port": 443, "peer_mtls_port": 443,
            "certificate_fingerprint": "aa" * 32, "tailnet_ip": "",
        })
        discovered = self.announce(tailnet_ip="")
        self.assertFalse(discovered.get("tailnet_ip"),
                         "pre-pair LAN claim was retained as a paired peer's authenticated mesh route")
        self.assertFalse(local_network.get_paired_peer(self.settings, "target-213").get("tailnet_ip"))

    def test_forgotten_peer_announcement_does_not_restore_pairing(self):
        local_network.save_paired_peer(self.settings, {
            "drone_id": "target-213", "reachable_url": "https://192.0.2.20",
            "certificate_fingerprint": "aa" * 32,
        })
        local_network.forget_peer(self.settings, "target-213", publish_membership=False)
        discovered = self.announce()
        self.assertFalse(discovered["paired"])
        self.assertIsNone(local_network.get_paired_peer(self.settings, "target-213"))


if __name__ == "__main__":
    unittest.main()
