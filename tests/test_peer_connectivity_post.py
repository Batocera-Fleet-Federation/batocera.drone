"""Authenticated peer POST address failover used by NFS negotiation."""

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
