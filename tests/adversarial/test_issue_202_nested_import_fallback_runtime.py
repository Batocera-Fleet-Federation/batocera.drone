"""Issue #202: named nested-module ImportError fallbacks must resolve with app/ on sys.path.

Same class as #164/#177: ``except ImportError`` after a relative sibling import
falls back to a *top-level* name of a file that now lives under a subpackage.
``python app/main.py`` loads those files as packages, so the relative import
succeeds and the except branch stays latent.

Named sites (issue evidence):

- ``common/self_update.py`` — ``from settings import Settings``
- ``music/coverart_client.py`` / ``music/wikimedia_client.py`` —
  ``from musicbrainz_client import MUSICBRAINZ_USER_AGENT``
- ``roms/scrapers.py`` — ``from gamelist import ...``

Domain invariant, from the issue reproduction, before looking at the patch:

- ``PYTHONPATH=app``: ``import settings`` / ``import musicbrainz_client`` /
  ``import gamelist`` raise ``ModuleNotFoundError``.
- ``from common.settings import Settings``,
  ``from music.musicbrainz_client import MUSICBRAINZ_USER_AGENT``,
  ``from roms.gamelist import _first_metadata_value`` succeed.
- Direct/non-package load of the named files (relative import cannot run)
  must take the except branch and bind those package-qualified modules.

Probes run in a fresh isolated interpreter with a temp cwd so the test
runner's import mode cannot hide a broken fallback.
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
import importlib.util
import json
import os
import sys
from pathlib import Path

app_dir = Path(sys.argv[1])
userdata = Path(sys.argv[2])
scenario = sys.argv[3]
sys.path.insert(0, str(app_dir))

BARE = ("settings", "musicbrainz_client", "gamelist")
QUALIFIED = (
    ("common.settings", "Settings"),
    ("music.musicbrainz_client", "MUSICBRAINZ_USER_AGENT"),
    ("roms.gamelist", "_first_metadata_value"),
    ("roms.gamelist", "_looks_like_placeholder_image"),
)
NAMED = (
    ("common/self_update.py", "Settings"),
    ("music/coverart_client.py", "_USER_AGENT"),
    ("music/wikimedia_client.py", "_USER_AGENT"),
    ("roms/scrapers.py", "_first_metadata_value"),
)


def _load_non_package(rel, attr):
    path = app_dir / rel
    modname = "issue202_" + rel.replace("/", "_").replace(".py", "")
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:
        raise SystemExit("no spec for " + rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    if mod.__package__:
        raise SystemExit(rel + " loaded as package " + repr(mod.__package__) + "; except branch would stay latent")
    if not hasattr(mod, attr):
        raise SystemExit(rel + " missing " + attr)
    return mod


if scenario == "bare-imports-fail":
    failures = []
    for name in BARE:
        try:
            __import__(name)
        except ModuleNotFoundError as error:
            if name not in str(error):
                failures.append(name + " raised ModuleNotFoundError without the name: " + str(error))
        else:
            failures.append(name + " imported as a top-level module; nested files must not")
    if failures:
        raise SystemExit("bare nested imports still resolve:\n" + "\n".join(failures))
    print(json.dumps({"ok": True, "scenario": scenario}))
    raise SystemExit(0)

if scenario == "qualified-imports-succeed":
    bound = {}
    for module_name, attr in QUALIFIED:
        mod = __import__(module_name, fromlist=[attr])
        value = getattr(mod, attr)
        bound[module_name + "." + attr] = type(value).__name__
    print(json.dumps({"ok": True, "scenario": scenario, "bound": bound}))
    raise SystemExit(0)

if scenario == "non-package-load-named-files":
    results = {}
    for rel, attr in NAMED:
        mod = _load_non_package(rel, attr)
        results[rel] = {"attr": attr, "package": mod.__package__, "name": mod.__name__}
    print(json.dumps({"ok": True, "scenario": scenario, "results": results}))
    raise SystemExit(0)

if scenario == "fallback-symbols-are-the-real-ones":
    from common.settings import Settings as CanonicalSettings
    from music.musicbrainz_client import MUSICBRAINZ_USER_AGENT as CanonicalUA
    from roms.gamelist import _first_metadata_value as canonical_first
    from roms.gamelist import _looks_like_placeholder_image as canonical_placeholder

    self_update = _load_non_package("common/self_update.py", "Settings")
    coverart = _load_non_package("music/coverart_client.py", "_USER_AGENT")
    wikimedia = _load_non_package("music/wikimedia_client.py", "_USER_AGENT")
    scrapers = _load_non_package("roms/scrapers.py", "_first_metadata_value")

    assert self_update.Settings is CanonicalSettings, (self_update.Settings, CanonicalSettings)
    assert coverart._USER_AGENT == CanonicalUA, coverart._USER_AGENT
    assert wikimedia._USER_AGENT == CanonicalUA, wikimedia._USER_AGENT
    assert scrapers._first_metadata_value is canonical_first
    assert scrapers._looks_like_placeholder_image is canonical_placeholder
    print(json.dumps({"ok": True, "scenario": scenario, "ua": CanonicalUA}))
    raise SystemExit(0)

if scenario == "fallback-helpers-survive-empty-input":
    scrapers = _load_non_package("roms/scrapers.py", "_first_metadata_value")
    first = scrapers._first_metadata_value
    placeholder = scrapers._looks_like_placeholder_image
    assert first() == ""
    assert first(None, "", "   ", {}, [], ()) == ""
    assert first(None, {"title": "  "}, ["", None], "Pong") == "Pong"
    assert placeholder(b"") is True
    assert placeholder(b"x" * 8) is True
    assert placeholder(b"\x00" * 200) is True
    print(json.dumps({"ok": True, "scenario": scenario}))
    raise SystemExit(0)

if scenario == "self-update-settings-from-env":
    for folder in ("roms", "bios", "saves", "movies"):
        (userdata / folder).mkdir(parents=True, exist_ok=True)
    kept = {key: os.environ[key] for key in ("PATH", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "USER") if key in os.environ}
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
    })
    self_update = _load_non_package("common/self_update.py", "Settings")
    settings = self_update.Settings.from_env()
    assert settings.device_id == "cabinet-host", settings.device_id
    print(json.dumps({"ok": True, "scenario": scenario, "device_id": settings.device_id}))
    raise SystemExit(0)

if scenario == "package-mode-named-modules":
    # Drop the flat app/ entry so these resolve only as app.* packages.
    sys.path[:] = [p for p in sys.path if Path(p).resolve() != app_dir.resolve()]
    repo = app_dir.parent
    sys.path.insert(0, str(repo))
    import app.common.self_update as self_update
    import app.music.coverart_client as coverart
    import app.music.wikimedia_client as wikimedia
    import app.roms.scrapers as scrapers
    from app.music.musicbrainz_client import MUSICBRAINZ_USER_AGENT
    assert self_update.Settings is not None
    assert coverart._USER_AGENT == MUSICBRAINZ_USER_AGENT
    assert wikimedia._USER_AGENT == MUSICBRAINZ_USER_AGENT
    assert callable(scrapers._first_metadata_value)
    print(json.dumps({
        "ok": True,
        "scenario": scenario,
        "packages": [
            self_update.__package__,
            coverart.__package__,
            wikimedia.__package__,
            scrapers.__package__,
        ],
    }))
    raise SystemExit(0)

raise SystemExit("unknown scenario: " + scenario)
"""


def _run(scenario: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="issue-202-") as tmp:
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


class NestedImportFallbackRuntimeTests(unittest.TestCase):
    def test_bare_nested_module_names_fail_with_app_on_path(self) -> None:
        payload = _run("bare-imports-fail")
        self.assertTrue(payload["ok"])

    def test_package_qualified_imports_succeed_with_app_on_path(self) -> None:
        payload = _run("qualified-imports-succeed")
        self.assertTrue(payload["ok"])
        self.assertIn("common.settings.Settings", payload["bound"])
        self.assertIn("music.musicbrainz_client.MUSICBRAINZ_USER_AGENT", payload["bound"])

    def test_named_files_load_when_relative_import_cannot_run(self) -> None:
        payload = _run("non-package-load-named-files")
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["results"]), 4)
        for rel, info in payload["results"].items():
            self.assertFalse(info["package"], rel)

    def test_fallback_binds_canonical_symbols(self) -> None:
        payload = _run("fallback-symbols-are-the-real-ones")
        self.assertTrue(payload["ok"])
        self.assertIn("BatoceraDroneMusicScraper", payload["ua"])

    def test_scraper_helpers_after_fallback_tolerate_empty_and_malformed_values(self) -> None:
        payload = _run("fallback-helpers-survive-empty-input")
        self.assertTrue(payload["ok"])

    def test_self_update_settings_from_env_after_fallback(self) -> None:
        payload = _run("self-update-settings-from-env")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["device_id"], "cabinet-host")

    def test_package_mode_named_modules_still_import(self) -> None:
        payload = _run("package-mode-named-modules")
        self.assertTrue(payload["ok"])
        self.assertEqual(
            payload["packages"],
            ["app.common", "app.music", "app.music", "app.roms"],
        )


if __name__ == "__main__":
    unittest.main()
