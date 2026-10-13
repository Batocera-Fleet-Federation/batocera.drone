"""Issue #177: transfer/ swarm_membership fallbacks must resolve when relative import fails.

The three named sites lazy-import swarm_membership after they have already
mutated pairing state:

- ``peer_connectivity._local_pair_peer`` (certificate pinned, peer saved)
- ``local_network.forget_peer`` (peer already popped from the paired map)
- ``peer_workers`` health loop (checks saved, then gossip)

``from . import swarm_membership`` succeeds while those modules load as the
``transfer`` package, so ``python app/main.py`` never takes the except branch.
The issue's invariant is the except branch itself: with ``app/`` on sys.path a
bare ``import swarm_membership`` raises ModuleNotFoundError, and the fallback
must be ``from transfer import swarm_membership``.

These probes run in a fresh interpreter, force the relative import to fail, and
refuse a top-level ``swarm_membership`` module so the except branch is the only
path that can succeed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "app"

PROBE = r"""
import builtins
import json
import os
import sys
from pathlib import Path
from unittest import mock

app_dir = sys.argv[1]
userdata = Path(sys.argv[2])
scenario = sys.argv[3]
sys.path.insert(0, app_dir)

for folder in ("roms", "bios", "saves", "movies"):
    (userdata / folder).mkdir(parents=True, exist_ok=True)

kept = {key: os.environ[key] for key in ("TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "USER", "PATH") if key in os.environ}
os.environ.clear()
os.environ.update(kept)
os.environ.update({
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
    "DRONE_LOCAL_HEALTH_INTERVAL_SECONDS": "10",
})

from common.settings import Settings  # noqa: E402
from transfer import local_network  # noqa: E402
from transfer import swarm_membership  # noqa: E402
from transfer import peer_connectivity  # noqa: E402
from transfer import peer_workers  # noqa: E402

settings = Settings.from_env()
local_network.set_mode(settings, local_network.MODE_LOCAL_NETWORK)
swarm_membership.set_membership_poster(lambda *_args, **_kwargs: {"status": "ok", "records": []})

_real_import = builtins.__import__


def _forced_relative_failure(name, globals=None, locals=None, fromlist=(), level=0):
    fromlist = fromlist or ()
    if level > 0 and "swarm_membership" in fromlist:
        raise ImportError("forced relative import failure (issue 177)")
    if name == "swarm_membership" or str(name).startswith("swarm_membership."):
        raise ModuleNotFoundError("No module named 'swarm_membership'")
    return _real_import(name, globals, locals, fromlist, level)


def _install_hook():
    builtins.__import__ = _forced_relative_failure


def _records():
    return swarm_membership.membership_view(settings).get("records") or []


if scenario == "bare-import-fails":
    try:
        _real_import("swarm_membership")
    except ModuleNotFoundError as error:
        assert "swarm_membership" in str(error), error
        print(json.dumps({"ok": True, "scenario": scenario}))
        raise SystemExit(0)
    raise SystemExit("bare import swarm_membership must fail with PYTHONPATH=app")

if scenario == "fallback-resolves":
    _install_hook()
    try:
        from . import swarm_membership as _rel  # type: ignore
    except ImportError:
        from transfer import swarm_membership as _rel  # type: ignore
    else:
        raise SystemExit("relative import was supposed to fail under the hook")
    assert hasattr(_rel, "record_approved_member")
    assert hasattr(_rel, "record_local_removal")
    assert hasattr(_rel, "propagate_membership")
    assert _rel.__name__.endswith("swarm_membership")
    print(json.dumps({"ok": True, "scenario": scenario, "module": _rel.__name__}))
    raise SystemExit(0)

if scenario == "forget-peer":
    stored = local_network.save_paired_peer(
        settings,
        {"drone_id": "cabinet-peer", "name": "Peer Cabinet", "certificate_fingerprint": "ab" * 32},
    )
    assert stored["drone_id"] == "cabinet-peer"
    _install_hook()
    removed = local_network.forget_peer(settings, "cabinet-peer")
    assert removed is True, removed
    assert local_network.get_paired_peer(settings, "cabinet-peer") is None
    records = _records()
    assert any(row.get("peer_id") == "cabinet-peer" and row.get("op") == "remove" for row in records), records
    print(json.dumps({"ok": True, "scenario": scenario}))
    raise SystemExit(0)

if scenario == "local-pair-peer":
    pair_response = {
        "status": "paired",
        "drone_id": "cabinet-peer",
        "name": "Peer Cabinet",
        "scheme": "https",
        "api_port": 8444,
        "peer_mtls_port": 8544,
        "reachable_url": "https://cabinet-peer.local",
        "tailnet_ip": "100.64.0.9",
        "certificate_pem": "-----BEGIN CERTIFICATE-----fake",
        "certificate_fingerprint": "aa:bb",
    }
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(pair_response).encode("utf-8")
    fake_manager = mock.MagicMock()
    fake_manager.return_value.ensure_certificate.return_value = {
        "public_certificate": "-----BEGIN CERTIFICATE-----own",
        "fingerprint": "cc:dd",
    }
    cert_path = userdata / "peer-certs" / "cabinet-peer.pem"
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    _install_hook()
    with mock.patch.object(peer_connectivity, "urlopen", return_value=response), \
            mock.patch.object(peer_connectivity, "DroneCertificateManager", fake_manager), \
            mock.patch.object(peer_connectivity, "_save_local_peer_certificate", return_value=(cert_path, "aa:bb")), \
            mock.patch.object(peer_connectivity, "_identity_swarm_networking_allowed", return_value=True), \
            mock.patch.object(local_network, "get_tailnet_ip", return_value="100.64.0.2"):
        stored = peer_connectivity._local_pair_peer(
            settings,
            {"drone_id": "cabinet-peer", "reachable_url": "https://100.64.0.9", "tailnet_ip": "100.64.0.9"},
            "12345678",
            tailnet_auto_pair=True,
        )
    assert stored["drone_id"] == "cabinet-peer", stored
    assert local_network.get_paired_peer(settings, "cabinet-peer") is not None
    records = _records()
    assert any(row.get("peer_id") == "cabinet-peer" and row.get("op") == "add" for row in records), records
    print(json.dumps({"ok": True, "scenario": scenario}))
    raise SystemExit(0)

if scenario == "health-loop":
    class CapturingThread:
        registry = {}

        def __init__(self, group=None, target=None, name=None, args=(), kwargs=None, *, daemon=None):
            self.target = target
            self.name = name
            self.args = args
            self.kwargs = kwargs or {}
            CapturingThread.registry[name] = self

        def start(self):
            return None

    sleep_calls = {"n": 0}

    def fake_sleep(_seconds):
        sleep_calls["n"] += 1
        if sleep_calls["n"] > 1:
            raise RuntimeError("stop-health-loop")

    propagate_calls = {"n": 0}

    def counting_poster(*_args, **_kwargs):
        propagate_calls["n"] += 1
        return {"status": "ok", "records": []}

    swarm_membership.set_membership_poster(counting_poster)
    _install_hook()
    with mock.patch.object(peer_workers, "Thread", CapturingThread), \
            mock.patch.object(peer_workers, "ensure_tailnet_networking"), \
            mock.patch.object(peer_workers, "bootstrap_tailnet_from_swarm"), \
            mock.patch.object(peer_workers, "tailnet_status", return_value={"running": True, "installed": True, "enrolled": True}), \
            mock.patch.object(peer_workers, "_repair_tailnet_data_path"), \
            mock.patch.object(local_network, "start_discovery_worker"):
        peer_workers._start_local_network_workers(settings)
    health = CapturingThread.registry.get("drone-local-peer-health")
    assert health is not None, sorted(CapturingThread.registry)
    with mock.patch.object(peer_workers.time, "sleep", side_effect=fake_sleep):
        try:
            health.target(*health.args, **health.kwargs)
        except RuntimeError as error:
            assert str(error) == "stop-health-loop", error
        else:
            raise SystemExit("health loop did not iterate")
    assert sleep_calls["n"] == 2, sleep_calls
    print(json.dumps({"ok": True, "scenario": scenario, "sleep_calls": sleep_calls["n"]}))
    raise SystemExit(0)

if scenario == "health-loop-propagate-error":
    class CapturingThread:
        registry = {}

        def __init__(self, group=None, target=None, name=None, args=(), kwargs=None, *, daemon=None):
            self.target = target
            self.name = name
            self.args = args
            self.kwargs = kwargs or {}
            CapturingThread.registry[name] = self

        def start(self):
            return None

    sleep_calls = {"n": 0}

    def fake_sleep(_seconds):
        sleep_calls["n"] += 1
        if sleep_calls["n"] > 2:
            raise RuntimeError("stop-health-loop")

    def exploding_poster(*_args, **_kwargs):
        raise RuntimeError("gossip exploded")

    swarm_membership.set_membership_poster(exploding_poster)
    local_network.save_paired_peer(
        settings,
        {"drone_id": "cabinet-peer", "name": "Peer", "reachable_url": "https://100.64.0.9"},
    )
    _install_hook()
    with mock.patch.object(peer_workers, "Thread", CapturingThread), \
            mock.patch.object(peer_workers, "ensure_tailnet_networking"), \
            mock.patch.object(peer_workers, "bootstrap_tailnet_from_swarm"), \
            mock.patch.object(peer_workers, "tailnet_status", return_value={"running": True, "installed": True, "enrolled": True}), \
            mock.patch.object(peer_workers, "_repair_tailnet_data_path"), \
            mock.patch.object(peer_workers, "_peer_get_json_for_peer", side_effect=RuntimeError("peer down")), \
            mock.patch.object(peer_workers, "_preferred_peer_address", return_value="https://100.64.0.9"), \
            mock.patch.object(local_network, "start_discovery_worker"):
        peer_workers._start_local_network_workers(settings)
    health = CapturingThread.registry["drone-local-peer-health"]
    with mock.patch.object(peer_workers.time, "sleep", side_effect=fake_sleep):
        try:
            health.target(*health.args, **health.kwargs)
        except RuntimeError as error:
            assert str(error) == "stop-health-loop", error
        else:
            raise SystemExit("health loop died instead of swallowing propagate errors")
    assert sleep_calls["n"] == 3, sleep_calls
    print(json.dumps({"ok": True, "scenario": scenario}))
    raise SystemExit(0)

raise SystemExit("unknown scenario: " + scenario)
"""


def _run(scenario: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="issue-177-") as tmp:
        result = subprocess.run(
            [sys.executable, "-I", "-c", PROBE, str(APP_DIR), tmp, scenario],
            cwd=tmp,
            env={key: os.environ[key] for key in ("PATH", "TMPDIR", "TEMP", "TMP") if key in os.environ},
            capture_output=True,
            text=True,
            timeout=120,
        )
    if result.returncode != 0:
        raise AssertionError(
            f"scenario {scenario!r} failed ({result.returncode}):\n"
            f"STDOUT:{result.stdout}\nSTDERR:{result.stderr}"
        )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError(f"scenario {scenario!r} produced no output:\nSTDERR:{result.stderr}")
    return json.loads(lines[-1])


class TransferMembershipFallbackRuntimeTests(unittest.TestCase):
    def test_bare_import_swarm_membership_fails_with_app_on_path(self) -> None:
        payload = _run("bare-import-fails")
        self.assertTrue(payload["ok"])

    def test_except_branch_resolves_nested_module_when_relative_import_fails(self) -> None:
        payload = _run("fallback-resolves")
        self.assertTrue(payload["ok"])
        self.assertIn("swarm_membership", payload["module"])

    def test_forget_peer_records_removal_after_relative_import_fails(self) -> None:
        payload = _run("forget-peer")
        self.assertTrue(payload["ok"])

    def test_local_pair_peer_records_membership_after_relative_import_fails(self) -> None:
        payload = _run("local-pair-peer")
        self.assertTrue(payload["ok"])

    def test_health_loop_propagates_membership_after_relative_import_fails(self) -> None:
        payload = _run("health-loop")
        self.assertTrue(payload["ok"])

    def test_health_loop_swallows_propagate_errors_but_not_import_errors(self) -> None:
        payload = _run("health-loop-propagate-error")
        self.assertTrue(payload["ok"])


if __name__ == "__main__":
    unittest.main()
