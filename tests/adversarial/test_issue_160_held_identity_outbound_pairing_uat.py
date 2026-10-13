"""Issue #160: an unresolved identity must not be presented to any peer, including at pairing time.

Requirements (sections 2B/3/11): "An ambiguous result must never allow an unverified
inherited identity to participate in swarm networking" and "Never expose inherited swarm
credentials before the decision is resolved". While swarm networking is held
(RECOVERY_REQUIRED) the administrator can still click Pair in the admin UI; that outbound
request carries this Drone's identity certificate and device id in its body, so it must be
refused before anything leaves the machine, and nothing may be recorded as paired.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import identity_provisioning as identity
from app.common.hardware_identity import HardwareIdentityProvider
from app.common.settings import Settings
from app.storage.state_store import database_path, load_payload
from app.transfer import peer_connectivity
from app.transfer.drone_tls import DroneCertificateManager

PEER = {"drone_id": "aa:bb:cc:dd:ee:01", "name": "other", "reachable_url": "https://10.0.0.5", "api_port": 443}


def provider(root: Path) -> HardwareIdentityProvider:
    return HardwareIdentityProvider(root, container_probe=lambda: False, machine="x86_64")


def hardware(root: Path, uuid: str, serial: str) -> HardwareIdentityProvider:
    directory = root / "sys" / "class" / "dmi" / "id"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "product_uuid").write_text(uuid + "\n", encoding="utf-8")
    (directory / "board_serial").write_text(serial + "\n", encoding="utf-8")
    return provider(root)


def settings_for(root: Path) -> Settings:
    env = {"USERDATA_ROOT": str(root), "TLS_SELF_SIGNED_DIR": str(root / "system" / "certs")}
    with mock.patch.dict(os.environ, env, clear=True):
        return Settings.from_env()


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class HeldIdentityOutboundPairing(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        temp = tempfile.TemporaryDirectory(prefix="issue-160-held-pair-")
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.hw_a = hardware(self.tmp / "hw-a", "2c9e7a41-5d03-48b6-a1f7-e0b3946c8d25", "HELDORIG-0001")
        self.hw_b = hardware(self.tmp / "hw-b", "e84b1d70-96a2-4c5f-83e1-5a07c2f9d614", "HELDCLONE-0002")
        self.original = settings_for(self.tmp / "original")
        identity.ensure_identity(self.original, self.hw_a)

    def pair_while_held(self, settings: Settings):
        requests = []

        def capture(request, timeout=None, context=None):
            requests.append(json.loads(request.data.decode("utf-8")) if request.data else None)
            raise OSError("peer unreachable in test")

        with mock.patch.object(peer_connectivity, "urlopen", capture), \
                mock.patch("app.transfer.local_network._notifications.record_event"):
            try:
                peer_connectivity._local_pair_peer(settings, dict(PEER), "12345678")
            except Exception:  # noqa: BLE001 - refusal or unreachable are both acceptable; leaking is not
                pass
        return requests

    def test_pairing_is_refused_before_sending_the_identity_when_hardware_evidence_is_missing(self) -> None:
        blind = self.tmp / "hw-blind"
        blind.mkdir()
        held = identity.ensure_identity(self.original, provider(blind))
        self.assertEqual(held.state, identity.State.RECOVERY_REQUIRED)
        self.assertFalse(identity.swarm_networking_allowed())
        requests = self.pair_while_held(self.original)
        self.assertEqual(len(requests), 0, msg="the held Drone sent its identity certificate and device id to a peer (%d request)" % len(requests))
        self.assertEqual(load_payload(database_path(self.original.userdata_root), "local_paired_peers", {}), {})

    def test_pairing_is_refused_while_a_clone_is_half_provisioned_and_creates_no_identity(self) -> None:
        target = self.tmp / "clone"
        shutil.copytree(self.original.userdata_root, target)
        clone = settings_for(target)
        with mock.patch.object(DroneCertificateManager, "_generate_local_certificate", side_effect=RuntimeError("disk full")):
            held = identity.ensure_identity(clone, self.hw_b)
        self.assertEqual(held.reason, "provisioning_failed")
        self.assertFalse(clone.drone_cert_file.exists())
        requests = self.pair_while_held(clone)
        self.assertEqual(len(requests), 0, msg="a half-provisioned clone minted and advertised an unjournaled identity to a peer")
        # Pairing must not mint an identity outside the journaled provisioning flow.
        self.assertFalse(clone.drone_cert_file.exists())
        self.assertEqual(load_payload(database_path(clone.userdata_root), "local_paired_peers", {}), {})

    def test_pairing_works_again_once_the_identity_is_resolved(self) -> None:
        blind = self.tmp / "hw-blind"
        blind.mkdir()
        identity.ensure_identity(self.original, provider(blind))
        identity.resolve_recovery(self.original, "keep", provider(blind), actor="admin")
        self.assertTrue(identity.swarm_networking_allowed())
        requests = self.pair_while_held(self.original)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["drone_id"], self.original.device_id)


if __name__ == "__main__":
    unittest.main()
