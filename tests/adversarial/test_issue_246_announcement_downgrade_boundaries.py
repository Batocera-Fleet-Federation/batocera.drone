"""Issue #246: unauthenticated LAN announces cannot force plaintext peer traffic.

Invariants taken from the issue, not from the delivery tests:

- A multicast/broadcast announce is unauthenticated. For an already-paired
  drone_id it must not copy scheme, reachable_url, ports, hostname-derived
  routes, or a TLS-pairing flag into the pinned record.
- A peer that was paired over TLS must never be offered an ``http://``
  candidate (health, inventory, download, re-pair). Fail closed if that
  leaves no route.
- A cached successful route may reorder authorized endpoints; it must not
  reintroduce a cleartext URL the pin rejected.
- ``http_only`` fleets that never paired over TLS still keep http routes.
- Legacy https pins that predate ``paired_over_tls`` still must not be
  rewritten by an announce (the original attack).
"""

from __future__ import annotations

import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.storage.state_store import database_path, save_peer_route
from app.transfer import local_network, peer_connectivity, peer_download
from app.transport.lan import LanDirectTransport
from app.web import handlers_network

FINGERPRINT = "aa" * 32
PEER_ID = "victim-246"


def _settings(root: Path, device_id: str = "local-246") -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "DRONE_DEVICE_ID": device_id,
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "HTTP_ONLY": "0",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


def _announce(**updates) -> dict:
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
        "public_reachable_url": "http://10.0.0.66:8080",
    }
    payload.update(updates)
    return payload


class Issue246AnnouncementDowngradeBoundaries(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="issue-246-bounds-")
        self.addCleanup(temp.cleanup)
        self.settings = _settings(Path(temp.name))
        local_network.set_mode(self.settings, local_network.MODE_LOCAL_NETWORK)

    def _pin_tls(self, **updates) -> dict:
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

    def _spoof(self, source_ip: str = "10.0.0.66", **updates) -> dict:
        return local_network.record_discovered_peer(self.settings, _announce(**updates), source_ip)

    def test_spoofed_http_announce_does_not_rewrite_tls_pin(self) -> None:
        before = self._pin_tls()
        discovered = self._spoof()
        after = local_network.get_paired_peer(self.settings, PEER_ID)
        self.assertEqual(after["reachable_url"], before["reachable_url"])
        self.assertEqual(after["advertised_reachable_url"], before["advertised_reachable_url"])
        self.assertEqual(after["scheme"], "https")
        self.assertEqual(after["api_port"], 7443)
        self.assertEqual(after["peer_mtls_port"], 8443)
        self.assertEqual(after["tailnet_ip"], "100.64.0.20")
        self.assertEqual(after["certificate_fingerprint"], FINGERPRINT)
        self.assertTrue(after.get("paired_over_tls"))
        self.assertEqual(after["hostname"], "victim.local")
        self.assertNotEqual(after["reachable_url"], discovered.get("announced_reachable_url"))

    def test_announce_cannot_clear_paired_over_tls(self) -> None:
        self._pin_tls()
        self._spoof(paired_over_tls=False, scheme="http")
        self._spoof(paired_over_tls=0, scheme="HTTP")
        stored = local_network.get_paired_peer(self.settings, PEER_ID)
        self.assertTrue(stored.get("paired_over_tls"))
        self.assertEqual(stored["scheme"], "https")

    def test_repeated_spoof_keeps_https_candidates_only(self) -> None:
        self._pin_tls()
        for _ in range(5):
            self._spoof()
        stored = local_network.get_paired_peer(self.settings, PEER_ID)
        discovered = local_network.discovered_peers(self.settings, include_stale=True)
        row = next(item for item in discovered if item["drone_id"] == PEER_ID)
        for peer in (stored, row):
            candidates = peer_connectivity._peer_address_candidates(
                peer, settings=self.settings, peer_id=PEER_ID
            )
            self.assertTrue(candidates, "TLS pin lost every route after spoofed announces")
            self.assertTrue(
                all(value.lower().startswith("https://") for value in candidates),
                candidates,
            )

    def test_legacy_https_pin_without_flag_is_not_downgraded_by_announce(self) -> None:
        """Pre-#246 rows have a cert pin and https URL but no paired_over_tls."""
        local_network.save_paired_peer(
            self.settings,
            {
                "drone_id": PEER_ID,
                "reachable_url": "https://192.0.2.20",
                "scheme": "https",
                "certificate_fingerprint": FINGERPRINT,
            },
        )
        self._spoof()
        stored = local_network.get_paired_peer(self.settings, PEER_ID)
        self.assertEqual(stored["reachable_url"], "https://192.0.2.20")
        self.assertEqual(stored.get("scheme"), "https")
        self.assertNotIn("http://", str(stored.get("reachable_url") or ""))
        candidates = peer_connectivity._peer_address_candidates(
            stored, settings=self.settings, peer_id=PEER_ID
        )
        self.assertTrue(candidates)
        self.assertTrue(all(value.lower().startswith("https://") for value in candidates), candidates)

    def test_malformed_announce_fields_do_not_land_on_the_pin(self) -> None:
        before = self._pin_tls()
        self._spoof(
            scheme=["http"],
            api_port="not-a-port",
            peer_mtls_port=-1,
            reachable_url={"url": "http://10.0.0.66"},
            certificate_fingerprint={"fp": "cc" * 32},
        )
        after = local_network.get_paired_peer(self.settings, PEER_ID)
        for key in (
            "reachable_url",
            "scheme",
            "api_port",
            "peer_mtls_port",
            "certificate_fingerprint",
            "paired_over_tls",
        ):
            self.assertEqual(after[key], before[key], key)

    def test_poisoned_http_fields_are_dropped_for_tls_paired_peer(self) -> None:
        peer = {
            "drone_id": PEER_ID,
            "scheme": "http",
            "protocol": "http",
            "api_port": 8080,
            "peer_mtls_port": 8080,
            "reachable_url": "http://10.0.0.66:8080",
            "advertised_reachable_url": "http://attacker.local:8080",
            "public_reachable_url": "http://10.0.0.66:8080",
            "tailnet_ip": "100.64.0.9",
            "local_ip": "10.0.0.66",
            "certificate_fingerprint": FINGERPRINT,
            "paired_over_tls": True,
        }
        candidates = peer_connectivity._peer_address_candidates(
            peer, settings=self.settings, peer_id=PEER_ID
        )
        self.assertEqual(candidates, [])
        self.assertIsNone(peer_connectivity._peer_address(peer))
        self.assertIsNone(
            peer_connectivity._preferred_peer_address(peer, settings=self.settings, peer_id=PEER_ID)
        )

    def test_mixed_http_and_https_candidates_keep_only_https(self) -> None:
        peer = {
            "drone_id": PEER_ID,
            "scheme": "http",
            "api_port": 7443,
            "peer_mtls_port": 7443,
            "reachable_url": "https://192.0.2.20:7443",
            "advertised_reachable_url": "http://10.0.0.66:8080",
            "tailnet_ip": "100.64.0.20",
            "certificate_fingerprint": FINGERPRINT,
            "paired_over_tls": True,
        }
        candidates = peer_connectivity._peer_address_candidates(
            peer, settings=self.settings, peer_id=PEER_ID
        )
        self.assertTrue(candidates)
        self.assertTrue(all(value.lower().startswith("https://") for value in candidates), candidates)
        self.assertTrue(any(value.startswith("https://192.0.2.20") for value in candidates), candidates)

    def test_cached_http_route_cannot_be_promoted_for_tls_peer(self) -> None:
        self._pin_tls()
        save_peer_route(
            database_path(self.settings.userdata_root),
            PEER_ID,
            "http://10.0.0.66:8080",
            "ip",
        )
        stored = local_network.get_paired_peer(self.settings, PEER_ID)
        candidates = peer_connectivity._peer_address_candidates(
            stored, settings=self.settings, peer_id=PEER_ID
        )
        self.assertNotIn("http://10.0.0.66:8080", candidates)
        self.assertTrue(all(value.lower().startswith("https://") for value in candidates), candidates)
        self.assertEqual(
            peer_connectivity._preferred_peer_address(stored, settings=self.settings, peer_id=PEER_ID),
            candidates[0],
        )

    def test_uppercase_http_candidate_is_refused(self) -> None:
        peer = {
            "drone_id": PEER_ID,
            "scheme": "HTTP",
            "reachable_url": "HTTP://10.0.0.66:8080",
            "paired_over_tls": True,
            "certificate_fingerprint": FINGERPRINT,
        }
        self.assertEqual(
            peer_connectivity._peer_address_candidates(peer, settings=self.settings, peer_id=PEER_ID),
            [],
        )
        self.assertIsNone(peer_connectivity._peer_address(peer))

    def test_http_only_peer_without_tls_pairing_keeps_http_routes(self) -> None:
        peer = {
            "drone_id": "plain-246",
            "scheme": "http",
            "api_port": 8080,
            "reachable_url": "http://10.0.0.5:8080",
            "certificate_fingerprint": FINGERPRINT,
        }
        self.assertEqual(
            peer_connectivity._peer_address_candidates(
                peer, settings=self.settings, peer_id="plain-246"
            ),
            ["http://10.0.0.5:8080"],
        )

    def test_lan_transport_rewrite_cannot_offer_plaintext_for_tls_peer(self) -> None:
        peer = {
            "drone_id": PEER_ID,
            "scheme": "http",
            "api_port": 8080,
            "peer_mtls_port": 8080,
            "reachable_url": "https://192.0.2.20:7443",
            "local_ip": "10.0.0.66",
            "public_ip": "203.0.113.8",
            "certificate_fingerprint": FINGERPRINT,
            "paired_over_tls": True,
        }
        transport = LanDirectTransport(
            fetch_fn=lambda request, context: {},
            local_network=lambda: {"public_ip": "203.0.113.8", "ipv4": ["192.0.2.1"]},
        )
        lan_url = transport.lan_url(peer)
        lan_peer = {**peer, "public_reachable_url": lan_url, "reachable_url": lan_url}
        candidates = peer_connectivity._peer_address_candidates(
            lan_peer, settings=self.settings, peer_id=PEER_ID
        )
        self.assertFalse(
            any(str(value).lower().startswith("http://") for value in candidates),
            candidates,
        )

    def test_pair_refuses_plaintext_before_sending_the_code(self) -> None:
        self._pin_tls()
        discovered = {
            "drone_id": PEER_ID,
            "reachable_url": "http://10.0.0.66:8080",
            "scheme": "http",
        }
        with mock.patch.object(peer_connectivity, "urlopen") as opener, mock.patch.object(
            peer_connectivity, "_identity_swarm_networking_allowed", return_value=True
        ), mock.patch.object(peer_connectivity, "DroneCertificateManager") as manager:
            manager.return_value.ensure_certificate.return_value = {
                "public_certificate": "pem",
                "fingerprint": "cc" * 32,
            }
            with self.assertRaises(peer_connectivity.PeerPairingRejected):
                peer_connectivity._local_pair_peer(self.settings, discovered, "246801")
        opener.assert_not_called()
        stored = local_network.get_paired_peer(self.settings, PEER_ID)
        self.assertEqual(stored["reachable_url"], "https://192.0.2.20:7443")
        self.assertTrue(stored.get("paired_over_tls"))

    def test_admin_discovered_pair_uses_pinned_https_after_spoof(self) -> None:
        self._pin_tls()
        self._spoof()
        handler = handlers_network.HandlersNetworkMixin()
        handler.settings = self.settings
        handler.server = mock.Mock()
        handler._send_json = mock.Mock()
        captured = {}

        def fake_pair(peer, code, fingerprint):
            captured["address"] = peer_connectivity._peer_address(peer)
            captured["peer"] = dict(peer)
            return None

        with mock.patch.object(handler, "_pair_peer_or_reply", side_effect=fake_pair):
            handler._handle_admin_local_peer_pair(PEER_ID, {"pairing_code": "246801"})
        self.assertIn("address", captured)
        address = str(captured["address"] or "")
        self.assertTrue(address.lower().startswith("https://"), address)
        self.assertFalse(address.lower().startswith("http://"))

    def test_inventory_helper_raises_instead_of_dialing_http(self) -> None:
        peer = {
            "drone_id": PEER_ID,
            "scheme": "http",
            "reachable_url": "http://10.0.0.66:8080",
            "paired_over_tls": True,
            "certificate_fingerprint": FINGERPRINT,
        }
        with mock.patch.object(peer_connectivity, "urlopen") as opener, mock.patch.object(
            peer_connectivity, "_identity_swarm_networking_allowed", return_value=True
        ):
            with self.assertRaises(ValueError):
                peer_connectivity._peer_get_json_for_peer(
                    peer, "/v1/api/peer/roms", self.settings, peer_id=PEER_ID
                )
        opener.assert_not_called()

    def test_download_preferred_address_is_https_after_spoof(self) -> None:
        self._pin_tls()
        self._spoof()
        stored = local_network.get_paired_peer(self.settings, PEER_ID)
        address = peer_download._preferred_peer_address(
            stored, settings=self.settings, peer_id=PEER_ID
        )
        self.assertIsNotNone(address)
        self.assertTrue(str(address).lower().startswith("https://"), address)


if __name__ == "__main__":
    unittest.main()
