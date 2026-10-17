"""Issue #333 UAT: invalid signed inventories never start the asset download.

The receiver has two distinct HTTP requests: one for the nonce-bound signed
inventory and a later one for ROM bytes.  A signed response can still be stale
or structurally invalid, so the latter request is authorized only after the
former is verified against the persisted pairing pin and all schema bindings.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlparse

from app.common.settings import Settings
from app.transfer import local_network, peer_connectivity, peer_download
from app.transfer import signed_inventory as inventory


class _Response(io.BytesIO):
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class PinnedInventoryReceiverUAT(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.keys = tempfile.TemporaryDirectory(prefix="issue-333-keys-")
        root = Path(cls.keys.name)
        cls.key = root / "source.key"
        cls.cert = root / "source.crt"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(cls.key), "-out", str(cls.cert), "-days", "1",
             "-subj", "/CN=issue-333-source"],
            check=True, capture_output=True,
        )
        cls.certificate = cls.cert.read_bytes()
        cls.pin = hashlib.sha256(
            ssl.PEM_cert_to_DER_cert(cls.certificate.decode("ascii"))
        ).hexdigest()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.keys.cleanup()

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="issue-333-receiver-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = {
            "USERDATA_ROOT": str(self.root / "userdata"),
            "ROMS_ROOT": str(self.root / "roms"),
            "BIOS_ROOT": str(self.root / "bios"),
            "SAVES_ROOT": str(self.root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(self.root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "issue-333-receiver",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            self.settings = Settings.from_env()
        self.peer = local_network.save_paired_peer(self.settings, {
            "drone_id": "issue-333-source",
            "reachable_url": "http://source.invalid:8080",
            "certificate_fingerprint": self.pin,
        })
        peer_connectivity._save_local_peer_certificate(
            self.settings, "issue-333-source", self.certificate.decode("ascii")
        )
        self.source = SimpleNamespace(
            device_id="issue-333-source", drone_key_file=self.key
        )
        self.asset = self.root / "source-game.bin"
        self.asset.write_bytes(b"source-authorized-bytes")
        self.target = self.settings.roms_root / "nes" / "game.bin"
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"preexisting-local-bytes")

    def _envelope_for_request(self, request, *, fault: str) -> _Response:
        query = parse_qs(urlparse(request.full_url).query, keep_blank_values=True)
        nonce = query["nonce"][0]
        if fault == "replayed-nonce":
            nonce = "00" * 32
        envelope = inventory.sign_inventory(
            self.source, "roms", query["system"][0], query["relative_path"][0], nonce,
            self.asset,
        )
        if fault == "noncanonical-hash":
            forged = copy.deepcopy(envelope["inventory"])
            forged["files"][0]["sha256"] = forged["files"][0]["sha256"].upper()
            envelope = {
                "inventory": forged,
                "signature": inventory.base64.b64encode(inventory.openssl(
                    ["dgst", "-sha256", "-sign", str(self.key)], inventory.canonical(forged)
                )).decode("ascii"),
            }
        return _Response(json.dumps(envelope).encode("ascii"))

    def test_replayed_or_noncanonical_signed_inventory_never_fetches_rom_bytes(self) -> None:
        """Even a real source signature may not bypass freshness/schema checks."""
        for fault in ("replayed-nonce", "noncanonical-hash"):
            with self.subTest(fault=fault), \
                    mock.patch.object(inventory, "urlopen", side_effect=lambda request, **_kw: self._envelope_for_request(request, fault=fault)), \
                    mock.patch.object(peer_download, "urlopen", side_effect=AssertionError("asset bytes requested")) as asset_get:
                with self.assertRaises(ValueError):
                    peer_download._download_rom_from_peer(
                        self.settings, {}, self.peer, "nes", "game.bin", overwrite=True
                    )
                asset_get.assert_not_called()
                self.assertEqual(self.target.read_bytes(), b"preexisting-local-bytes")
                self.assertFalse(self.target.with_name("game.bin.part").exists())

    def test_pin_mismatch_stops_before_even_the_inventory_request(self) -> None:
        """The paired-record pin is checked locally, before any peer request."""
        with mock.patch.object(
            inventory.local_network, "get_paired_peer",
            return_value={"certificate_fingerprint": "0" * 64},
        ), mock.patch.object(inventory, "urlopen", side_effect=AssertionError("inventory dialed")) as inventory_get, \
                mock.patch.object(peer_download, "urlopen", side_effect=AssertionError("asset dialed")) as asset_get:
            with self.assertRaises(ValueError):
                peer_download._download_rom_from_peer(
                    self.settings, {}, self.peer, "nes", "game.bin", overwrite=True
                )
            inventory_get.assert_not_called()
            asset_get.assert_not_called()
            self.assertEqual(self.target.read_bytes(), b"preexisting-local-bytes")


if __name__ == "__main__":
    unittest.main()
