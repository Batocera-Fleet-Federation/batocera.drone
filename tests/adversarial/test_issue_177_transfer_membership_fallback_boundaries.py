"""Issue #177 boundaries: source contract, malformed forget_peer, package-mode.

Runtime of the except branch lives in the companion runtime suite. This file
locks the three named transfer/ call sites to the documented dual import
(``from . import swarm_membership`` then ``from transfer import swarm_membership``),
rejects a bare sibling import of swarm_membership anywhere under transfer/,
and checks forget_peer error paths that must not raise ModuleNotFoundError.
"""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer import local_network
from app.transfer import swarm_membership


REPO_ROOT = Path(__file__).resolve().parents[2]
TRANSFER_DIR = REPO_ROOT / "app" / "transfer"
NAMED_FILES = (
    TRANSFER_DIR / "local_network.py",
    TRANSFER_DIR / "peer_connectivity.py",
    TRANSFER_DIR / "peer_workers.py",
)


def _settings(root: Path, device_id: str = "cabinet-host") -> Settings:
    for folder in ("roms", "bios", "saves", "movies"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    with mock.patch.dict(
        "os.environ",
        {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "MOVIES_ROOT": str(root / "movies"),
            "DRONE_DEVICE_ID": device_id,
            "HTTPS_PORT": "8443",
            "DRONE_PEER_MTLS_PORT": "8543",
            "HTTP_ONLY": "1",
            "HTTP_REDIRECT_PORT": "0",
            "DRONE_COMPAT_HTTPS_PORTS": "",
            "ROM_METADATA_POLL_SECONDS": "0",
            "DRONE_AUTO_UPDATE_POLL_SECONDS": "0",
            "DRONE_CAST_ENABLED": "0",
            "USE_FAKE_DATA": "0",
            "LOG_DIR": str(root / "logs"),
            "TLS_SELF_SIGNED_DIR": str(root / "system" / "certs"),
        },
        clear=True,
    ):
        return Settings.from_env()


def _bare_swarm_membership_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "swarm_membership" or alias.name.startswith("swarm_membership."):
                    hits.append(f"{path.name}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
            if module == "swarm_membership" or module.startswith("swarm_membership."):
                hits.append(f"{path.name}:{node.lineno}: from {module} import ...")
    return hits


def _lazy_swarm_membership_handlers(path: Path) -> list[ast.ExceptHandler]:
    """Except-handlers that catch ImportError around a relative swarm_membership import."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    handlers = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        tries_relative = False
        for child in node.body:
            for inner in ast.walk(child):
                if (
                    isinstance(inner, ast.ImportFrom)
                    and inner.level > 0
                    and any(alias.name == "swarm_membership" for alias in inner.names)
                ):
                    tries_relative = True
        if not tries_relative:
            continue
        for handler in node.handlers:
            names = []
            if isinstance(handler.type, ast.Name):
                names = [handler.type.id]
            elif isinstance(handler.type, ast.Tuple):
                names = [elt.id for elt in handler.type.elts if isinstance(elt, ast.Name)]
            if "ImportError" in names or "ModuleNotFoundError" in names:
                handlers.append(handler)
    return handlers


def _except_imports_from_transfer_package(handler: ast.ExceptHandler) -> bool:
    for node in ast.walk(handler):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "transfer":
            if any(alias.name == "swarm_membership" for alias in node.names):
                return True
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "transfer.swarm_membership":
            return True
    return False


class TransferMembershipFallbackSourceTests(unittest.TestCase):
    def test_named_files_still_exist(self) -> None:
        missing = [str(path) for path in NAMED_FILES if not path.is_file()]
        self.assertEqual(missing, [])

    def test_transfer_package_has_no_bare_swarm_membership_import(self) -> None:
        offenders = []
        for path in sorted(TRANSFER_DIR.glob("*.py")):
            offenders.extend(_bare_swarm_membership_imports(path))
        self.assertEqual(offenders, [], "bare swarm_membership import still in transfer/:\n" + "\n".join(offenders))

    def test_named_sites_keep_relative_then_flat_package_fallback(self) -> None:
        failures = []
        for path in NAMED_FILES:
            handlers = _lazy_swarm_membership_handlers(path)
            if not handlers:
                failures.append(f"{path.name}: no try/except around from . import swarm_membership")
                continue
            for handler in handlers:
                if not _except_imports_from_transfer_package(handler):
                    failures.append(
                        f"{path.name}:{handler.lineno}: ImportError fallback does not "
                        f"use from transfer import swarm_membership"
                    )
        self.assertEqual(failures, [], "\n".join(failures))

    def test_named_sites_do_not_importlib_the_bare_module(self) -> None:
        """A rewritten fallback via importlib.import_module('swarm_membership') is the same bug."""
        offenders = []
        for path in NAMED_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute) and func.attr == "import_module"):
                    continue
                if not node.args:
                    continue
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and arg.value == "swarm_membership":
                    offenders.append(f"{path.name}:{node.lineno}: import_module('swarm_membership')")
        self.assertEqual(offenders, [])


class ForgetPeerBoundaryTests(unittest.TestCase):
    def test_package_mode_forget_missing_peer_is_false_not_import_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="issue-177-miss-") as tmp:
            settings = _settings(Path(tmp))
            self.assertFalse(local_network.forget_peer(settings, "nobody"))
            self.assertFalse(local_network.forget_peer(settings, ""))
            self.assertFalse(local_network.forget_peer(settings, "   "))
            self.assertFalse(local_network.forget_peer(settings, None))  # type: ignore[arg-type]
            self.assertEqual(swarm_membership.membership_view(settings)["records"], [])

    def test_publish_membership_false_skips_tombstone_and_still_forgets(self) -> None:
        with tempfile.TemporaryDirectory(prefix="issue-177-nopub-") as tmp:
            settings = _settings(Path(tmp))
            local_network.save_paired_peer(settings, {"drone_id": "cabinet-peer", "name": "Peer"})
            removed = local_network.forget_peer(settings, "cabinet-peer", publish_membership=False)
            self.assertTrue(removed)
            self.assertIsNone(local_network.get_paired_peer(settings, "cabinet-peer"))
            self.assertEqual(swarm_membership.membership_view(settings)["records"], [])

    def test_record_local_removal_accepts_empty_previous_for_non_dict_guard(self) -> None:
        """forget_peer passes {} when the popped row is not a dict; that path must not raise."""
        with tempfile.TemporaryDirectory(prefix="issue-177-empty-prev-") as tmp:
            settings = _settings(Path(tmp))
            swarm_membership.record_local_removal(settings, "cabinet-peer", {})
            swarm_membership.record_local_removal(settings, "cabinet-peer", None)
            records = swarm_membership.membership_view(settings)["records"]
            self.assertTrue(
                any(row.get("peer_id") == "cabinet-peer" and row.get("op") == "remove" for row in records),
                records,
            )


class PackageModeCallSiteTests(unittest.TestCase):
    def test_package_mode_forget_paired_peer_writes_tombstone(self) -> None:
        with tempfile.TemporaryDirectory(prefix="issue-177-pkg-") as tmp:
            settings = _settings(Path(tmp))
            local_network.save_paired_peer(
                settings,
                {"drone_id": "cabinet-peer", "name": "Peer Cabinet", "certificate_fingerprint": "cd" * 32},
            )
            self.assertTrue(local_network.forget_peer(settings, "cabinet-peer"))
            records = swarm_membership.membership_view(settings)["records"]
            self.assertTrue(
                any(row.get("peer_id") == "cabinet-peer" and row.get("op") == "remove" for row in records),
                records,
            )


if __name__ == "__main__":
    unittest.main()
