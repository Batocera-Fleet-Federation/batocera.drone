"""Issue #246 boundaries: untrusted announce fields never become dial targets.

Invariants from the issue, not from the delivery tests:

- An unauthenticated announce may record announced_* evidence and last_seen.
  It must not change a paired peer's scheme, URLs, ports, hostname, fingerprint,
  or TLS-pairing flag, and it must not inject local_ip / public_reachable_url
  that later become http:// candidates.
- Candidate construction must ignore announced_reachable_url, attacker hostname,
  and source_ip even when those fields sit on the discovered row.
- Malformed schemes (javascript:, ftp, whitespace, mixed-type) must not produce
  a plaintext candidate for a TLS-paired peer.
- An announce must not *set* paired_over_tls on an http_only pin (DoS the
  legitimate plaintext fleet) or clear it on a TLS pin.
- Wrong service / empty drone_id must leave the pin untouched.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer import local_network, peer_connectivity

FINGERPRINT = "aa" * 32
PEER_ID = "victim-246"


def _settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "DRONE_DEVICE_ID": "local-246",
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "HTTP_ONLY": "0",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class Issue246AnnouncementFieldBoundaries(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="issue-246-fields-")
        self.addCleanup(temp.cleanup)
        self.settings = _settings(Path(temp.name))
        local_network.set_mode(self.settings, local_network.MODE_LOCAL_NETWORK)

    def _pin(self, **updates) -> dict:
        peer = {
            "drone_id": PEER_ID,
            "name": "Victim",
            "hostname": "victim.local",
            "reachable_url": "https://192.0.2.20:7443",
            "advertised_reachable_url": "https://victim.local:7443",
            "scheme": "https",
            "api_port": 7443,
            "peer_mtls_port": 8443,
            "tailnet_ip": "100.64.0.20",
            "certificate_fingerprint": FINGERPRINT,
            "source_ip": "192.0.2.20",
            "paired_over_tls": True,
            "pairing_source": "local_network",
        }
        peer.update(updates)
        return local_network.save_paired_peer(self.settings, peer)

    def _announce(self, source_ip: str = "10.0.0.66", **updates):
        payload = {
            "service": local_network.DISCOVERY_SERVICE,
            "drone_id": PEER_ID,
            "name": "Attacker cabinet",
            "hostname": "attacker.local",
            "scheme": "http",
            "api_port": 8080,
            "peer_mtls_port": 8080,
            "reachable_url": "http://attacker.local:8080",
            "certificate_fingerprint": "bb" * 32,
            "tailnet_ip": "100.64.0.66",
            "paired_over_tls": False,
            "local_ip": "10.0.0.66",
            "public_ip": "198.51.100.66",
            "public_reachable_url": "http://10.0.0.66:8080",
            "private_ip": "10.0.0.66",
        }
        payload.update(updates)
        return local_network.record_discovered_peer(self.settings, payload, source_ip)

    def _candidates(self, peer: dict) -> list[str]:
        return peer_connectivity._peer_address_candidates(
            peer, settings=self.settings, peer_id=str(peer.get("drone_id") or PEER_ID)
        )

    def test_announced_url_hostname_and_source_ip_are_not_candidates(self) -> None:
        self._pin()
        discovered = self._announce()
        stored = local_network.get_paired_peer(self.settings, PEER_ID)
        self.assertEqual(discovered.get("announced_reachable_url"), "http://10.0.0.66:8080")
        self.assertEqual(discovered.get("hostname"), "attacker.local")
        self.assertEqual(discovered.get("source_ip"), "10.0.0.66")
        for peer in (stored, discovered):
            candidates = self._candidates(peer)
            blob = " ".join(candidates).lower()
            self.assertNotIn("10.0.0.66", blob, candidates)
            self.assertNotIn("attacker.local", blob, candidates)
            self.assertNotIn("http://", blob, candidates)
            self.assertNotIn(str(discovered.get("announced_reachable_url") or ""), candidates)

    def test_announce_does_not_inject_local_ip_or_public_reachable_url_onto_pin(self) -> None:
        before = self._pin()
        self._announce()
        after = local_network.get_paired_peer(self.settings, PEER_ID)
        self.assertEqual(after.get("local_ip"), before.get("local_ip"))
        self.assertEqual(after.get("public_reachable_url"), before.get("public_reachable_url"))
        self.assertEqual(after.get("private_ip"), before.get("private_ip"))
        self.assertEqual(after.get("public_ip"), before.get("public_ip"))
        self.assertEqual(after["hostname"], "victim.local")
        self.assertEqual(after["name"], "Victim")

    def test_wrong_service_or_empty_id_leaves_the_pin_untouched(self) -> None:
        before = self._pin()
        self.assertIsNone(
            local_network.record_discovered_peer(
                self.settings,
                {"service": "not-drone", "drone_id": PEER_ID, "scheme": "http", "api_port": 8080},
                "10.0.0.66",
            )
        )
        self.assertIsNone(
            local_network.record_discovered_peer(
                self.settings,
                {"service": local_network.DISCOVERY_SERVICE, "drone_id": "", "scheme": "http"},
                "10.0.0.66",
            )
        )
        after = local_network.get_paired_peer(self.settings, PEER_ID)
        self.assertEqual(after["reachable_url"], before["reachable_url"])
        self.assertEqual(after["scheme"], "https")
        self.assertTrue(after.get("paired_over_tls"))

    def test_announce_cannot_force_tls_flag_onto_http_only_pin(self) -> None:
        local_network.save_paired_peer(
            self.settings,
            {
                "drone_id": PEER_ID,
                "reachable_url": "http://192.0.2.20:8080",
                "scheme": "http",
                "api_port": 8080,
                "certificate_fingerprint": FINGERPRINT,
                "paired_over_tls": False,
            },
        )
        self._announce(scheme="https", api_port=443, paired_over_tls=True, reachable_url="https://10.0.0.66")
        stored = local_network.get_paired_peer(self.settings, PEER_ID)
        self.assertEqual(stored["reachable_url"], "http://192.0.2.20:8080")
        self.assertEqual(stored.get("scheme"), "http")
        self.assertFalse(bool(stored.get("paired_over_tls")))
        self.assertIn("http://192.0.2.20:8080", self._candidates(stored))

    def test_malformed_schemes_are_not_plaintext_candidates_for_tls_peer(self) -> None:
        self._pin()
        for scheme in ("javascript:alert(1)", "ftp", "ws", "http:https", " HTTP", "http\n", ["/http"], None, ""):
            with self.subTest(scheme=scheme):
                discovered = self._announce(scheme=scheme) if scheme is not None else self._announce()
                stored = local_network.get_paired_peer(self.settings, PEER_ID)
                self.assertEqual(stored["scheme"], "https")
                for peer in (stored, discovered):
                    candidates = self._candidates(peer)
                    self.assertTrue(candidates, scheme)
                    self.assertTrue(
                        all(value.lower().startswith("https://") for value in candidates),
                        (scheme, candidates),
                    )

    def test_ipv6_source_does_not_rewrite_the_pin(self) -> None:
        before = self._pin()
        discovered = self._announce(source_ip="2001:db8::66")
        after = local_network.get_paired_peer(self.settings, PEER_ID)
        self.assertEqual(after["reachable_url"], before["reachable_url"])
        blob = " ".join(self._candidates(after) + self._candidates(discovered)).lower()
        self.assertNotIn("2001:db8::66", blob)
        self.assertNotIn("http://", blob)


if __name__ == "__main__":
    unittest.main()
