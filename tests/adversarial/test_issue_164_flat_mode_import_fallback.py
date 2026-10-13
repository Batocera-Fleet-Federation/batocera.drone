"""Issue #164: flat-mode swarm_membership fallbacks must resolve under PYTHONPATH=app.

The device launches as ``python3 -m app.main`` (package mode), where
``from ..transfer import swarm_membership`` works. The documented
``python app/main.py`` entrypoint puts ``app/`` on ``sys.path``, so ``web``
is a top-level package and the relative import raises ImportError. The
except-branch must then import the nested module as
``from transfer import swarm_membership``. A bare ``import swarm_membership``
cannot find ``app/transfer/swarm_membership.py``.

That fallback runs *after* the pairing handler has already pinned the
peer certificate and saved the paired-peer row, so a ModuleNotFoundError
there is an HTTP 500 on a half-completed pairing.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "app"
HANDLERS_PEER = APP_DIR / "web" / "handlers_peer.py"
HANDLERS_NETWORK = APP_DIR / "web" / "handlers_network.py"

FLAT_HANDLER_PROGRAM = r"""
import json
import os
import sys
from pathlib import Path
from unittest import mock

userdata = Path(sys.argv[1])
scenario = sys.argv[2]

for folder in ("roms", "bios", "saves", "movies"):
    (userdata / folder).mkdir(parents=True, exist_ok=True)

path = sys.argv[3] or os.environ.get("PATH", "")
kept = {key: os.environ[key] for key in ("TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "USER") if key in os.environ}
os.environ.clear()
os.environ.update(kept)
os.environ.update({
    "PATH": path,
    "HOME": str(userdata),
    "USERDATA_ROOT": str(userdata),
    "ROMS_ROOT": str(userdata / "roms"),
    "BIOS_ROOT": str(userdata / "bios"),
    "SAVES_ROOT": str(userdata / "saves"),
    "MOVIES_ROOT": str(userdata / "movies"),
    "DRONE_DEVICE_ID": "cabinet-host",
    "HTTPS_PORT": "8443",
    "DRONE_PEER_MTLS_PORT": "8543",
    "HTTP_ONLY": "1",
    "HTTP_REDIRECT_PORT": "0",
    "DRONE_COMPAT_HTTPS_PORTS": "",
    "ROM_METADATA_POLL_SECONDS": "0",
    "DRONE_AUTO_UPDATE_POLL_SECONDS": "0",
    "DRONE_CAST_ENABLED": "0",
    "USE_FAKE_DATA": "0",
    "LOG_DIR": str(userdata / "logs"),
    "TLS_SELF_SIGNED_DIR": str(userdata / "system" / "certs"),
})

from common.settings import Settings  # noqa: E402
from transfer.drone_tls import DroneCertificateManager  # noqa: E402
from transfer import local_network  # noqa: E402
from transfer import swarm_membership  # noqa: E402
from web import handlers_peer  # noqa: E402
from web import handlers_network  # noqa: E402

settings = Settings.from_env()
own = DroneCertificateManager(settings).ensure_certificate()

peer_root = userdata / "peer-device"
for folder in ("roms", "bios", "saves"):
    (peer_root / folder).mkdir(parents=True, exist_ok=True)
os.environ["USERDATA_ROOT"] = str(peer_root)
os.environ["ROMS_ROOT"] = str(peer_root / "roms")
os.environ["BIOS_ROOT"] = str(peer_root / "bios")
os.environ["SAVES_ROOT"] = str(peer_root / "saves")
os.environ["DRONE_DEVICE_ID"] = "cabinet-peer"
os.environ["TLS_SELF_SIGNED_DIR"] = str(peer_root / "system" / "certs")
os.environ["LOG_DIR"] = str(peer_root / "logs")
peer_settings = Settings.from_env()
peer_cert = DroneCertificateManager(peer_settings).ensure_certificate()
os.environ["USERDATA_ROOT"] = str(userdata)
os.environ["DRONE_DEVICE_ID"] = "cabinet-host"

captured = {}

class PairHandler(handlers_peer.HandlersPeerMixin):
    pass

class ForgetHandler(handlers_network.HandlersNetworkMixin):
    pass

def send_json(status, payload, **_kwargs):
    captured["status"] = status
    captured["payload"] = payload

pair = PairHandler()
pair.settings = settings
pair.client_address = ("192.168.1.50", 12345)
pair.server = mock.MagicMock()
pair._send_json = send_json
pair._peer_requester_device_id = lambda: ""
pair._presented_client_fingerprint = lambda: ""

code = str(local_network.pairing_code(settings)["code"])
pem = str(peer_cert.get("public_certificate") or "")
fingerprint = str(peer_cert.get("fingerprint") or "")

if scenario == "invalid-code":
    pair._handle_peer_pair({
        "pairing_code": "00000000",
        "drone_id": "cabinet-peer",
        "name": "Peer",
        "certificate_pem": pem,
        "certificate_fingerprint": fingerprint,
    })
    assert captured["status"] == 403, captured
    assert local_network.get_paired_peer(settings, "cabinet-peer") is None
    membership = swarm_membership.membership_view(settings)
    assert membership["records"] == []
elif scenario == "own-id":
    try:
        pair._handle_peer_pair({
            "pairing_code": code,
            "drone_id": settings.device_id,
            "name": "self",
            "certificate_pem": pem,
        })
    except ValueError as error:
        assert "invalid peer id" in str(error), error
    else:
        raise SystemExit("expected ValueError for own device id")
    assert local_network.get_paired_peer(settings, settings.device_id) is None
elif scenario == "fingerprint-mismatch":
    try:
        pair._handle_peer_pair({
            "pairing_code": code,
            "drone_id": "cabinet-peer",
            "name": "Peer",
            "certificate_pem": pem,
            "certificate_fingerprint": "0" * 64,
        })
    except ValueError as error:
        assert "fingerprint mismatch" in str(error)
    else:
        raise SystemExit("expected fingerprint mismatch")
    assert local_network.get_paired_peer(settings, "cabinet-peer") is None
    cert_path = userdata / "system" / "drone-app" / "local-peer-certs" / "cabinet-peer.crt"
    assert not cert_path.exists(), "mismatch must unlink the half-written pin"
elif scenario == "pair":
    with mock.patch.object(handlers_peer, "load_peer_cert_everywhere"):
        pair._handle_peer_pair({
            "pairing_code": code,
            "drone_id": "cabinet-peer",
            "name": "Peer Cabinet",
            "hostname": "peer.local",
            "scheme": "http",
            "api_port": 8444,
            "peer_mtls_port": 8544,
            "certificate_pem": pem,
            "certificate_fingerprint": fingerprint,
        })
    assert captured["status"] == 200, captured
    assert captured["payload"]["status"] == "paired"
    stored = local_network.get_paired_peer(settings, "cabinet-peer")
    assert stored is not None, "pairing must persist the peer before returning"
    assert stored["certificate_fingerprint"] == fingerprint
    records = swarm_membership.membership_view(settings)["records"]
    assert any(row.get("peer_id") == "cabinet-peer" and row.get("op") == "add" for row in records), records
    rotated = str(local_network.pairing_code(settings)["code"])
    assert rotated != code, "successful pairing must rotate the pairing code"
elif scenario == "membership-unpaired":
    pair._handle_peer_membership({"records": [], "introducer_id": "stranger"})
    assert captured["status"] == 403, captured
    assert "paired" in str(captured["payload"].get("error") or "").lower()
elif scenario == "membership-removed-fingerprint":
    swarm_membership.record_local_removal(
        settings,
        "departed-peer",
        {"drone_id": "departed-peer", "certificate_fingerprint": "ab" * 32, "name": "gone"},
    )
    pair._presented_client_fingerprint = lambda: "ab" * 32
    caller = pair._membership_caller_id({})
    assert caller == "departed-peer", caller
elif scenario == "forget-missing":
    forget = ForgetHandler()
    forget.settings = settings
    forget._send_json = send_json
    with mock.patch.object(handlers_network._nfs_exports, "revoke_peer", return_value={"status": "not_found"}):
        forget._handle_admin_local_peer_forget("nobody")
    assert captured["status"] == 200, captured
    assert captured["payload"]["status"] == "not_found"
elif scenario == "forget-paired":
    with mock.patch.object(handlers_peer, "load_peer_cert_everywhere"):
        pair._handle_peer_pair({
            "pairing_code": code,
            "drone_id": "cabinet-peer",
            "name": "Peer Cabinet",
            "certificate_pem": pem,
            "certificate_fingerprint": fingerprint,
        })
    assert captured["status"] == 200, captured
    forget = ForgetHandler()
    forget.settings = settings
    forget._send_json = send_json
    with mock.patch.object(handlers_network._nfs_exports, "revoke_peer", return_value={"status": "not_found"}):
        forget._handle_admin_local_peer_forget("cabinet-peer")
    assert captured["status"] == 200, captured
    assert captured["payload"]["status"] == "forgotten"
    assert local_network.get_paired_peer(settings, "cabinet-peer") is None
else:
    raise SystemExit("unknown scenario")

print(json.dumps({"ok": True, "scenario": scenario, "captured": captured}))
"""

PACKAGE_IMPORT_PROGRAM = r"""
from app.web import handlers_peer
from app.web import handlers_network
from app.transfer import swarm_membership
assert hasattr(handlers_peer.HandlersPeerMixin, "_handle_peer_pair")
assert hasattr(handlers_network.HandlersNetworkMixin, "_handle_admin_local_peer_forget")
assert hasattr(swarm_membership, "record_approved_member")
print("ok")
"""


def _bare_swarm_membership_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "swarm_membership" or alias.name.startswith("swarm_membership."):
                    hits.append(f"{path.name}:{node.lineno}: import {alias.name}")
    return hits


def _run(program: str, *, env: dict, cwd: Path, extra_args=None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", program, *(extra_args or [])],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class FlatModeSwarmMembershipImportTests(unittest.TestCase):
    def test_handler_sources_do_not_bare_import_swarm_membership(self) -> None:
        """The broken except-ImportError fallback was `import swarm_membership`."""
        failures = []
        for path in (HANDLERS_PEER, HANDLERS_NETWORK):
            failures.extend(_bare_swarm_membership_imports(path))
        self.assertEqual(failures, [], "flat-mode fallback still uses a bare module import:\n" + "\n".join(failures))

    def test_flat_mode_fallback_import_resolves_nested_module(self) -> None:
        """The except-ImportError branch must succeed with app/ on sys.path."""
        program = (
            "try:\n"
            "    from ..transfer import swarm_membership as _swarm_membership\n"
            "except ImportError:\n"
            "    from transfer import swarm_membership as _swarm_membership\n"
            "assert hasattr(_swarm_membership, 'record_approved_member')\n"
            "assert hasattr(_swarm_membership, 'handle_membership_request')\n"
            "assert hasattr(_swarm_membership, 'publish_forget')\n"
            "assert hasattr(_swarm_membership, 'peer_id_for_removed_fingerprint')\n"
            "print(_swarm_membership.__name__)\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(APP_DIR)
        result = _run(program, env=env, cwd=APP_DIR)
        self.assertEqual(
            result.returncode,
            0,
            f"flat-mode swarm_membership fallback failed:\nSTDOUT:{result.stdout}\nSTDERR:{result.stderr}",
        )
        self.assertIn("swarm_membership", result.stdout)

    def test_bare_import_swarm_membership_is_the_failure_mode(self) -> None:
        """Guard the domain invariant: a nested file is not a top-level module."""
        program = (
            "import swarm_membership\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(APP_DIR)
        result = _run(program, env=env, cwd=APP_DIR)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No module named 'swarm_membership'", result.stderr)

    def test_package_mode_relative_import_still_resolves(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        result = _run(PACKAGE_IMPORT_PROGRAM, env=env, cwd=REPO_ROOT)
        self.assertEqual(
            result.returncode,
            0,
            f"package-mode handler import failed:\nSTDOUT:{result.stdout}\nSTDERR:{result.stderr}",
        )


@unittest.skipUnless(shutil.which("openssl"), "openssl is required to mint Drone certificates")
class FlatModeHandlerCallSiteTests(unittest.TestCase):
    def _run_scenario(self, scenario: str) -> dict:
        with tempfile.TemporaryDirectory(prefix="issue-164-flat-") as tmp:
            env = dict(os.environ)
            env["PYTHONPATH"] = str(APP_DIR)
            env["PATH"] = os.environ.get("PATH", "")
            result = _run(
                FLAT_HANDLER_PROGRAM,
                env=env,
                cwd=APP_DIR,
                extra_args=[tmp, scenario, os.environ.get("PATH", "")],
                timeout=120,
            )
        if result.returncode != 0:
            self.fail(
                f"flat-mode scenario {scenario!r} failed ({result.returncode}):\n"
                f"STDOUT:{result.stdout}\nSTDERR:{result.stderr}"
            )
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, result.stdout)
        return json.loads(lines[-1])

    def test_pair_records_membership_instead_of_raising_module_not_found(self) -> None:
        payload = self._run_scenario("pair")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["captured"]["status"], 200)

    def test_invalid_pairing_code_is_forbidden_and_does_not_half_pair(self) -> None:
        payload = self._run_scenario("invalid-code")
        self.assertEqual(payload["captured"]["status"], 403)

    def test_fingerprint_mismatch_unlinks_the_pin(self) -> None:
        payload = self._run_scenario("fingerprint-mismatch")
        self.assertTrue(payload["ok"])

    def test_own_device_id_is_rejected_without_saving_a_peer(self) -> None:
        payload = self._run_scenario("own-id")
        self.assertTrue(payload["ok"])

    def test_membership_from_unpaired_caller_is_forbidden_not_500(self) -> None:
        payload = self._run_scenario("membership-unpaired")
        self.assertEqual(payload["captured"]["status"], 403)

    def test_removed_member_fingerprint_resolves_caller_id(self) -> None:
        payload = self._run_scenario("membership-removed-fingerprint")
        self.assertTrue(payload["ok"])

    def test_forget_missing_peer_is_not_found_not_500(self) -> None:
        payload = self._run_scenario("forget-missing")
        self.assertEqual(payload["captured"]["payload"]["status"], "not_found")

    def test_forget_paired_peer_publishes_removal(self) -> None:
        payload = self._run_scenario("forget-paired")
        self.assertEqual(payload["captured"]["payload"]["status"], "forgotten")
