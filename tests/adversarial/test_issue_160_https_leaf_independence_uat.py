"""Issue #160: HTTPS leaf renewal is independent of long-lived identity rotation.

Requirement 6: clone provisioning must issue a new identity certificate and
appropriate HTTPS certificates, and routine HTTPS leaf renewal must not rotate
the long-lived Drone identity. create_server reconciles certificates after
ensure_identity; a failed clone must not be un-held by that reconcile.
"""

import hashlib
import os
import shutil
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import identity_provisioning as identity
from app.common.hardware_identity import HardwareIdentityProvider
from app.common.settings import Settings
from app.transfer.drone_tls import DroneCertificateManager
from app.web.server_tls import _resolve_tls_material


def hardware(root: Path, uuid: str, serial: str) -> HardwareIdentityProvider:
    directory = root / "sys/class/dmi/id"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "product_uuid").write_text(uuid + "\n", encoding="utf-8")
    (directory / "board_serial").write_text(serial + "\n", encoding="utf-8")
    return HardwareIdentityProvider(root, container_probe=lambda: False, machine="x86_64")


def settings_for(root: Path) -> Settings:
    with mock.patch.dict(os.environ, {
        "USERDATA_ROOT": str(root),
        "TLS_SELF_SIGNED_DIR": str(root / "system" / "certs"),
    }, clear=True):
        return Settings.from_env()


def fingerprint(item: Settings) -> str:
    pem = item.drone_cert_file.read_text(encoding="utf-8")
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def leaf_names():
    return (
        mock.patch("app.transfer.drone_tls.socket.gethostname", return_value="batocera"),
        mock.patch("app.transfer.drone_tls.socket.getfqdn", return_value="batocera.local"),
        mock.patch(
            "app.transfer.drone_tls._get_local_certificate_ips",
            return_value=["127.0.0.1", "192.168.0.50"],
        ),
    )


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class HttpsLeafIndependenceUAT(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self.tmp = Path(tempfile.mkdtemp(prefix="issue-160-https-leaf-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.hw_a = hardware(self.tmp / "hw-a", "9a1c4e57-2b0d-4f38-8c61-71d5e3a0b9f2", "ORIGBOARD-0001")
        self.hw_b = hardware(self.tmp / "hw-b", "5d7e0c13-8a94-4b26-9f10-c3e8a2d64b75", "CLONEBOARD-0002")
        self.original = settings_for(self.tmp / "original")
        self.assertEqual(identity.ensure_identity(self.original, self.hw_a).state, identity.State.READY)

    def test_clone_wipes_inherited_https_leaf_and_new_leaf_is_signed_by_new_identity(self) -> None:
        host, fqdn, ips = leaf_names()
        with host, fqdn, ips:
            original_leaf, original_leaf_key = _resolve_tls_material(self.original)
        original_identity = fingerprint(self.original)
        original_leaf_bytes = original_leaf.read_bytes()
        original_leaf_key_bytes = original_leaf_key.read_bytes()

        target = self.tmp / "clone"
        shutil.copytree(self.original.userdata_root, target, symlinks=True)
        identity.reset_runtime_state_for_tests()
        clone = settings_for(target)
        result = identity.ensure_identity(clone, self.hw_b)
        self.assertEqual(result.status, identity.STATUS_CLONE)
        self.assertNotEqual(fingerprint(clone), original_identity)
        self.assertFalse((Path(clone.tls_self_signed_dir) / "server.crt").exists())
        self.assertFalse((Path(clone.tls_self_signed_dir) / "server.key").exists())

        host, fqdn, ips = leaf_names()
        with host, fqdn, ips:
            leaf, key = _resolve_tls_material(clone)
        self.assertNotEqual(leaf.read_bytes(), original_leaf_bytes)
        self.assertNotEqual(key.read_bytes(), original_leaf_key_bytes)
        self.assertEqual(fingerprint(clone), fingerprint(settings_for(target)))
        verify = subprocess.run(
            ["openssl", "verify", "-CAfile", str(clone.drone_cert_file), str(leaf)],
            capture_output=True,
        )
        self.assertEqual(verify.returncode, 0, msg=verify.stderr.decode("utf-8", "replace"))
        reject = subprocess.run(
            ["openssl", "verify", "-CAfile", str(self.original.drone_cert_file), str(leaf)],
            capture_output=True,
        )
        self.assertNotEqual(reject.returncode, 0)
        self.assertEqual(fingerprint(self.original), original_identity)
        self.assertEqual(original_leaf.read_bytes(), original_leaf_bytes)

    def test_https_leaf_repair_does_not_rotate_swarm_identity(self) -> None:
        host, fqdn, ips = leaf_names()
        with host, fqdn, ips:
            leaf, key = _resolve_tls_material(self.original)
            identity_bytes = self.original.drone_cert_file.read_bytes()
            identity_fp = fingerprint(self.original)
            key.write_text("not a private key\n", encoding="utf-8")
            _resolve_tls_material(self.original)
        self.assertEqual(self.original.drone_cert_file.read_bytes(), identity_bytes)
        self.assertEqual(fingerprint(self.original), identity_fp)
        identity.reset_runtime_state_for_tests()
        again = identity.ensure_identity(settings_for(self.original.userdata_root), self.hw_a)
        self.assertEqual(again.status, identity.STATUS_VERIFIED)
        self.assertEqual(fingerprint(self.original), identity_fp)

    def test_create_server_cert_reconcile_cannot_unhold_a_failed_clone(self) -> None:
        target = self.tmp / "clone"
        shutil.copytree(self.original.userdata_root, target, symlinks=True)
        clone = settings_for(target)
        with mock.patch.object(DroneCertificateManager, "_generate_local_certificate", side_effect=RuntimeError("disk full")):
            failed = identity.ensure_identity(clone, self.hw_b)
        self.assertEqual(failed.state, identity.State.RECOVERY_REQUIRED)
        self.assertFalse(identity.swarm_networking_allowed())
        self.assertFalse(clone.drone_key_file.exists())

        DroneCertificateManager(clone).ensure_certificate()
        self.assertFalse(identity.swarm_networking_allowed())
        self.assertNotEqual(fingerprint(clone), fingerprint(self.original))

        identity.reset_runtime_state_for_tests()
        retry = identity.ensure_identity(settings_for(target), self.hw_b)
        self.assertEqual(retry.state, identity.State.READY)
        self.assertEqual(retry.status, identity.STATUS_CLONE)
        self.assertNotEqual(fingerprint(settings_for(target)), fingerprint(self.original))
        self.assertTrue(identity.swarm_networking_allowed())


if __name__ == "__main__":
    unittest.main()
