"""Authenticated peer POST address failover used by NFS negotiation."""

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import URLError

from app.common.settings import Settings
from app.transfer import peer_connectivity


def _settings(root: Path) -> Settings:
    with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True):
        return Settings.from_env()


class PeerPostConnectivityTests(unittest.TestCase):
    def test_post_falls_back_between_trusted_routes_and_caches_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            peer = {
                "drone_id": "peer-1",
                "tailnet_ip": "100.91.173.37",
                "reachable_url": "https://192.168.0.180",
                "scheme": "https",
                "api_port": 443,
            }
            with mock.patch.object(
                peer_connectivity,
                "_peer_post_json",
                side_effect=[URLError("timed out"), {"available": True}],
            ) as post_json, mock.patch.object(peer_connectivity, "_remember_successful_peer_route") as remember:
                payload, address = peer_connectivity._peer_post_json_for_peer(
                    peer,
                    "/v1/api/peer/network-share/nfs/authorize",
                    {"protocol_version": 1},
                    settings,
                    peer_id="peer-1",
                    timeout=20,
                )

        self.assertEqual(payload, {"available": True})
        self.assertEqual(address, "https://192.168.0.180")
        self.assertEqual(post_json.call_count, 2)
        self.assertEqual(post_json.call_args_list[0].kwargs["timeout"], 3)
        self.assertEqual(post_json.call_args_list[1].kwargs["timeout"], 20)
        self.assertEqual(post_json.call_args_list[1].args[1], {"protocol_version": 1})
        remember.assert_called_once_with(settings, "peer-1", "https://192.168.0.180")


class PeerFileDownloadConnectivityTests(unittest.TestCase):
    def test_download_uses_trusted_route_and_caches_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _settings(root / "userdata")
            destination = root / "attachment.tar.gz"
            peer = {"drone_id": "peer-1"}
            with mock.patch.object(
                peer_connectivity, "_peer_address_candidates", return_value=["https://peer.local"]
            ), mock.patch.object(
                peer_connectivity, "_peer_trust_cafile", return_value=root / "peer.pem"
            ), mock.patch.object(
                peer_connectivity, "_drone_client_ssl_context", return_value=mock.sentinel.context
            ), mock.patch.object(
                peer_connectivity, "urlopen", return_value=io.BytesIO(b"backup-data")
            ) as opened, mock.patch.object(
                peer_connectivity, "_remember_successful_peer_route"
            ) as remember:
                size, address = peer_connectivity._peer_download_file_for_peer(
                    peer,
                    "/v1/api/peer/config-backups/weekly.tar.gz",
                    destination,
                    settings,
                    peer_id="peer-1",
                )
            self.assertEqual(size, 11)
            self.assertEqual(address, "https://peer.local")
            self.assertEqual(destination.read_bytes(), b"backup-data")
            self.assertIn("/v1/api/peer/config-backups/weekly.tar.gz", opened.call_args.args[0].full_url)
            remember.assert_called_once_with(settings, "peer-1", "https://peer.local")

    def test_oversized_download_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _settings(root / "userdata")
            destination = root / "attachment.tar.gz"
            with mock.patch.object(
                peer_connectivity, "_peer_address_candidates", return_value=["http://peer.local"]
            ), mock.patch.object(
                peer_connectivity, "_peer_trust_cafile", return_value=None
            ), mock.patch.object(
                peer_connectivity, "_drone_client_ssl_context", return_value=mock.sentinel.context
            ), mock.patch.object(
                peer_connectivity, "urlopen", return_value=io.BytesIO(b"too-large")
            ):
                with self.assertRaises(ValueError):
                    peer_connectivity._peer_download_file_for_peer(
                        {}, "/attachment", destination, settings, max_bytes=3
                    )
            self.assertFalse(destination.exists())

    def test_post_overall_deadline_stops_before_an_extra_route_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp) / "userdata")
            peer = {
                "tailnet_ip": "100.64.0.5",
                "advertised_reachable_url": "https://drone-b.local",
                "reachable_url": "https://192.168.1.50",
            }
            with mock.patch.object(
                peer_connectivity,
                "_peer_post_json",
                side_effect=URLError("timed out"),
            ) as post_json, mock.patch.object(
                peer_connectivity.time,
                "monotonic",
                side_effect=[1.0, 8.0, 11.0],
            ):
                with self.assertRaises(URLError):
                    peer_connectivity._peer_post_json_for_peer(
                        peer,
                        "/v1/api/peer/network-share/nfs/authorize",
                        {},
                        settings,
                        peer_id="peer-1",
                        timeout=4,
                        overall_deadline=10.0,
                    )

        self.assertEqual(post_json.call_count, 2)


if __name__ == "__main__":
    unittest.main()
