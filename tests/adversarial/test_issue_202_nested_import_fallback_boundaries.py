"""Issue #202 boundaries: source contract for named ImportError fallbacks.

Runtime of the except branch lives in the companion runtime suite. This file
locks the four named files to relative-then-package-qualified dual imports,
rejects a rewritten fallback via ``importlib.import_module('settings')`` (same
bug), and scans every ``app/`` ImportError-class handler — including tuple
handlers, ``ModuleNotFoundError``, and nested package directories such as
``media_keys`` — so a latent bare sibling import cannot hide behind the
narrower ``except ImportError`` / nested-file-stem heuristic.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "app"

NAMED_SITES = (
    (APP_DIR / "common" / "self_update.py", ".settings", "common.settings", "Settings"),
    (APP_DIR / "music" / "coverart_client.py", ".musicbrainz_client", "music.musicbrainz_client", "MUSICBRAINZ_USER_AGENT"),
    (APP_DIR / "music" / "wikimedia_client.py", ".musicbrainz_client", "music.musicbrainz_client", "MUSICBRAINZ_USER_AGENT"),
    (APP_DIR / "roms" / "scrapers.py", ".gamelist", "roms.gamelist", "_first_metadata_value"),
)

BARE_FORBIDDEN = {"settings", "musicbrainz_client", "gamelist"}
IMPORT_EXCEPTIONS = {"ImportError", "ModuleNotFoundError"}


def _exception_names(handler: ast.ExceptHandler) -> list[str]:
    node = handler.type
    if node is None:
        return ["bare"]
    parts = node.elts if isinstance(node, ast.Tuple) else [node]
    names = []
    for part in parts:
        if isinstance(part, ast.Name):
            names.append(part.id)
        elif isinstance(part, ast.Attribute):
            names.append(part.attr)
    return names


def _is_import_error_handler(handler: ast.ExceptHandler) -> bool:
    names = _exception_names(handler)
    return bool(IMPORT_EXCEPTIONS.intersection(names)) or "Exception" in names or "bare" in names


def _import_strings_in(handler: ast.ExceptHandler) -> list[tuple[int, str, str]]:
    """Return (lineno, kind, module) for fallback imports inside a handler."""
    found = []
    for node in ast.walk(handler):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.lineno, "from", node.module))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, "import", alias.name))
        elif isinstance(node, ast.Call):
            func = node.func
            is_import_module = (
                (isinstance(func, ast.Attribute) and func.attr == "import_module")
                or (isinstance(func, ast.Name) and func.id == "import_module")
            )
            if not is_import_module or not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.append((node.lineno, "import_module", arg.value))
            elif (
                isinstance(arg, ast.Call)
                and isinstance(arg.func, ast.Name)
                and arg.func.id == "__import__"
                and arg.args
                and isinstance(arg.args[0], ast.Constant)
                and isinstance(arg.args[0].value, str)
            ):
                found.append((node.lineno, "__import__", arg.args[0].value))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "__import__":
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                found.append((node.lineno, "__import__", node.args[0].value))
    return found


def _relative_modules_in_try(node: ast.Try) -> set[str]:
    names = set()
    for child in node.body:
        for inner in ast.walk(child):
            if isinstance(inner, ast.ImportFrom) and inner.level > 0:
                if inner.module:
                    names.add(inner.module)
                for alias in inner.names:
                    names.add(alias.name)
    return names


def _layout() -> tuple[set[str], set[str], set[str], set[str]]:
    top_level = {path.stem for path in APP_DIR.glob("*.py") if path.name != "__init__.py"}
    packages = {
        path.name
        for path in APP_DIR.iterdir()
        if path.is_dir() and (path / "__init__.py").exists()
    }
    nested_stems = set()
    nested_pkg_dirs = set()
    for package in packages:
        root = APP_DIR / package
        for path in root.rglob("*"):
            if path.is_dir() and (path / "__init__.py").exists():
                nested_pkg_dirs.add(path.name)
            if path.suffix == ".py" and path.name != "__init__.py":
                nested_stems.add(path.stem)
    nested_stems -= top_level | packages
    nested_pkg_dirs -= packages | top_level
    return top_level, packages, nested_stems, nested_pkg_dirs


class NamedSiteSourceTests(unittest.TestCase):
    def test_named_files_still_exist(self) -> None:
        missing = [str(path) for path, *_ in NAMED_SITES if not path.is_file()]
        self.assertEqual(missing, [])

    def test_named_sites_keep_relative_then_package_qualified_fallback(self) -> None:
        failures = []
        for path, relative_mod, qualified_mod, symbol in NAMED_SITES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            sibling = relative_mod.lstrip(".")
            matched = False
            for node in ast.walk(tree):
                if not isinstance(node, ast.Try):
                    continue
                relative_ok = False
                for child in node.body:
                    for inner in ast.walk(child):
                        if not isinstance(inner, ast.ImportFrom) or inner.level == 0:
                            continue
                        module = inner.module or ""
                        names = {alias.name for alias in inner.names}
                        if module == sibling and symbol in names:
                            relative_ok = True
                if not relative_ok:
                    continue
                for handler in node.handlers:
                    if not _is_import_error_handler(handler):
                        continue
                    bound = False
                    for _lineno, _kind, module in _import_strings_in(handler):
                        if module == qualified_mod or module.startswith(qualified_mod + "."):
                            bound = True
                    if bound:
                        matched = True
            if not matched:
                failures.append(
                    f"{path.relative_to(APP_DIR)}: no try from {relative_mod} import {symbol} / "
                    f"except from {qualified_mod} import {symbol}"
                )
        self.assertEqual(failures, [], "\n".join(failures))

    def test_named_sites_do_not_bare_import_the_issue_modules(self) -> None:
        offenders = []
        for path, *_rest in NAMED_SITES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Try):
                    continue
                for handler in node.handlers:
                    if not _is_import_error_handler(handler):
                        continue
                    for lineno, kind, module in _import_strings_in(handler):
                        first = module.split(".")[0]
                        if first in BARE_FORBIDDEN:
                            offenders.append(
                                f"{path.relative_to(APP_DIR)}:{lineno} {kind} {module}"
                            )
        self.assertEqual(offenders, [], "bare issue-module fallback still present:\n" + "\n".join(offenders))

    def test_named_sites_do_not_importlib_the_bare_module(self) -> None:
        offenders = []
        for path, *_rest in NAMED_SITES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (
                    (isinstance(func, ast.Attribute) and func.attr == "import_module")
                    or (isinstance(func, ast.Name) and func.id in {"import_module", "__import__"})
                ):
                    continue
                if not node.args:
                    continue
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and arg.value in BARE_FORBIDDEN:
                    offenders.append(f"{path.name}:{node.lineno}: {arg.value}")
        self.assertEqual(offenders, [])


class RepoWideFallbackScanTests(unittest.TestCase):
    def test_import_error_fallbacks_never_name_nested_modules_or_packages_bare(self) -> None:
        """Bare nested file *or nested package dir* in an ImportError-class handler is the bug.

        ``from media_keys.paths import X`` is the same failure mode as
        ``from settings import Settings``: PYTHONPATH=app does not put
        ``integrations/`` children on ``sys.path``. Top-level packages
        (``common``, ``music``, ``roms``, ...) and top-level ``app/*.py``
        modules are the documented flat-mode names.
        """
        top_level, packages, nested_stems, nested_pkg_dirs = _layout()
        forbidden = nested_stems | nested_pkg_dirs
        offenders = []
        for path in sorted(APP_DIR.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Try):
                    continue
                relative = _relative_modules_in_try(node)
                for handler in node.handlers:
                    names = _exception_names(handler)
                    watches_import = bool(IMPORT_EXCEPTIONS.intersection(names))
                    if not watches_import and not (relative and ("Exception" in names or "bare" in names)):
                        continue
                    for lineno, kind, module in _import_strings_in(handler):
                        first = module.split(".")[0]
                        if first in forbidden:
                            offenders.append(
                                f"{path.relative_to(APP_DIR)}:{lineno} except {names} {kind} {module}"
                            )
        self.assertEqual(
            offenders,
            [],
            "bare nested-module/package ImportError fallbacks:\n" + "\n".join(offenders),
        )

    def test_tuple_and_modulenotfounderror_handlers_are_scanned(self) -> None:
        """The delivery AST check only matches ``except ImportError`` as ast.Name.

        A rewritten fallback of ``except (ImportError, ModuleNotFoundError):``
        or ``except ModuleNotFoundError:`` must still be rejected. This test
        compiles synthetic handlers through the same classifier used above.
        """
        source = (
            "try:\n"
            "    from .settings import Settings\n"
            "except (ImportError, ModuleNotFoundError):\n"
            "    from settings import Settings\n"
            "try:\n"
            "    from .gamelist import x\n"
            "except ModuleNotFoundError:\n"
            "    import gamelist\n"
        )
        tree = ast.parse(source)
        classified = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            for handler in node.handlers:
                classified.append((_exception_names(handler), _is_import_error_handler(handler), _import_strings_in(handler)))
        self.assertEqual(len(classified), 2)
        self.assertTrue(classified[0][1])
        self.assertTrue(classified[1][1])
        self.assertEqual({item[2] for item in classified[0][2]}, {"settings"})
        self.assertEqual({item[2] for item in classified[1][2]}, {"gamelist"})

        top_level, packages, nested_stems, nested_pkg_dirs = _layout()
        self.assertIn("settings", nested_stems)
        self.assertIn("gamelist", nested_stems)
        self.assertIn("musicbrainz_client", nested_stems)
        self.assertIn("media_keys", nested_pkg_dirs)
        self.assertIn("streamdeck", nested_pkg_dirs)
        self.assertNotIn("common", nested_stems)
        self.assertNotIn("music", nested_stems)
        self.assertNotIn("roms", nested_stems)


if __name__ == "__main__":
    unittest.main()
