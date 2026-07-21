"""Guard tests for the ``app/`` package decomposition.

``drone_api.py`` was split into ~87 focused modules, each wired with a dual
``try: from .pkg.mod ... except ImportError: from pkg.mod`` re-export/import shim so BOTH
the device's package-mode launch (``python3 -m app.main``) and the flat
(``PYTHONPATH=app``) convention keep working. These tests lock that in:

1. every module imports cleanly in **package mode** (the real device path);
2. the ``drone_api`` shim still re-exports its whole public surface (a forgotten
   re-export after a future extraction fails here, not at runtime on a device);
3. the ``drone_api`` shim imports in **flat mode** — which exercises the
   ``except ImportError`` branch of every re-export shim that package mode never hits
   (this is the branch a generic shim-generator once got wrong: ``from handlers_x import``
   instead of ``from web.handlers_x import`` — invisible in package mode).
"""
import importlib
import os
import subprocess
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"

# The shim's public surface: the entrypoint/bootstrap + the two base classes + a
# representative symbol from each extracted layer. A future extraction that forgets to
# re-export one of these breaks here instead of silently on a device.
PUBLIC_SURFACE = (
    "main", "create_server", "RomRepository", "RomRequestHandler",
    "Settings", "ARTWORK_FIELDS", "ARTWORK_DUPLICATE_FILTER",
    "_collect_system_info_payload", "_resolve_tls_material",
    "DownloadManager", "_download_rom_from_peer",
    "_poll_rom_metadata_cache", "_hash_rom_metadata_batches", "DroneCertificateManager",
)


def _package_module_names():
    names = []
    for path in sorted(APP_DIR.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        rel = path.relative_to(APP_DIR).with_suffix("")
        names.append("app." + ".".join(rel.parts))
    return names


class ModuleImportModeTests(unittest.TestCase):
    def test_every_module_imports_in_package_mode(self):
        """`python3 -m app.main` (the device launch) resolves every module."""
        failures = []
        for name in _package_module_names():
            try:
                importlib.import_module(name)
            except Exception as error:  # noqa: BLE001 - report all, don't stop at the first
                failures.append(f"{name}: {error!r}")
        self.assertEqual(failures, [], "package-mode import failures:\n" + "\n".join(failures))

    def test_drone_api_shim_reexports_public_surface(self):
        drone_api = importlib.import_module("app.drone_api")
        missing = [name for name in PUBLIC_SURFACE if not hasattr(drone_api, name)]
        self.assertEqual(missing, [], f"drone_api no longer re-exports: {missing}")

    def test_drone_api_imports_in_flat_mode(self):
        """`PYTHONPATH=app; import drone_api` exercises every shim's flat except-branch."""
        checks = " and ".join(f"hasattr(d, {name!r})" for name in PUBLIC_SURFACE)
        program = f"import drone_api as d\nassert {checks}, 'flat-mode public surface incomplete'\n"
        env = dict(os.environ)
        env["PYTHONPATH"] = str(APP_DIR)
        result = subprocess.run(
            [sys.executable, "-c", program],
            cwd=str(APP_DIR), env=env, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(
            result.returncode, 0,
            f"flat-mode `import drone_api` failed:\nSTDOUT:{result.stdout}\nSTDERR:{result.stderr}",
        )

    def test_runtime_state_singletons_are_shared_not_copied(self):
        """The coordination Event/Lock objects must be the *same* object everywhere.

        The whole poll/sync pipeline coordinates through these; if any module ended up
        with a copy (e.g. a stray local re-definition instead of importing from
        ``common.runtime_state``), threads would silently fail to coordinate. This asserts
        the shared-object invariant across the shim and representative consumers.
        """
        runtime_state = importlib.import_module("app.common.runtime_state")
        drone_api = importlib.import_module("app.drone_api")
        for attr in ("_ROM_METADATA_ACTIVE", "_ROM_METADATA_WAKE",
                     "_ROM_METADATA_LOCK", "_GAMELIST_WRITE_LOCK"):
            canonical = getattr(runtime_state, attr)
            self.assertIs(getattr(drone_api, attr), canonical, f"drone_api.{attr} is a copy")

    def test_key_reexports_are_identity_not_redefinitions(self):
        """drone_api must *re-export* moved symbols (same object), not redefine them.

        Tests patch/read `app.drone_api.X` for symbols whose home is now elsewhere; if the
        shim redefined X instead of importing it, `patch("app.drone_api.X")` would silently
        stop affecting the real code. Assert identity with the home module.
        """
        drone_api = importlib.import_module("app.drone_api")
        homes = {
            "_collect_system_info_payload": "app.device.system_info",
            "_poll_rom_metadata_cache": "app.roms.rom_scanner",
            "_download_rom_from_peer": "app.transfer.peer_download",
            "DownloadManager": "app.transfer.download_manager",
            "DroneCertificateManager": "app.transfer.drone_tls",
            "ARTWORK_FIELDS": "app.roms.gamelist",
            "_resolve_tls_material": "app.web.server_tls",
        }
        mismatched = []
        for symbol, home in homes.items():
            module = importlib.import_module(home)
            if getattr(drone_api, symbol) is not getattr(module, symbol):
                mismatched.append(f"{symbol}: drone_api copy != {home}.{symbol}")
        self.assertEqual(mismatched, [], "shim redefinitions (should be re-exports):\n" + "\n".join(mismatched))

    def test_god_classes_compose_all_their_mixins(self):
        """RomRepository / RomRequestHandler must include every extracted mixin in their MRO."""
        drone_api = importlib.import_module("app.drone_api")
        repo_mixins = {
            "RomArtworkApplyMixin", "RomArtworkGamelistMixin", "RomScanMixin",
            "RomSystemsSearchMixin", "RomAssetBiosMixin",
        }
        handler_mixins = {
            "HandlersPeerMixin", "HandlersContentMixin", "HandlersArtworkMixin",
            "HandlersNetworkMixin", "HandlersConfigMixin",
            "HandlersDiagnosticsMixin", "HandlersDownloadsMixin", "HandlersSystemMixin",
        }
        repo_mro = {c.__name__ for c in drone_api.RomRepository.__mro__}
        handler_mro = {c.__name__ for c in drone_api.RomRequestHandler.__mro__}
        self.assertEqual(repo_mixins - repo_mro, set(), "RomRepository missing mixins")
        self.assertEqual(handler_mixins - handler_mro, set(), "RomRequestHandler missing mixins")
        # the split query methods still resolve on the composed class
        for method in ("apply_launchbox_artwork", "list_gamelist_rom_metadata", "search_roms",
                       "list_bios_entries", "build_fingerprint"):
            self.assertTrue(hasattr(drone_api.RomRepository, method), f"RomRepository lost {method}")


if __name__ == "__main__":
    unittest.main()
