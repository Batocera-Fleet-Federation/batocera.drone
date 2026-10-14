"""Game identity, cross-peer matching and per-game multiplayer assessment.

Reuses the Drone's existing ROM cache and gamelist metadata (no second library)
and the *already computed* sampled fingerprints (``sample-fp-v1``): nothing is
hashed here, so listing or matching never triggers library-wide hashing.
A game without a cached fingerprint is matched by name and size and marked
unverified rather than hashed on demand.
"""

import os
import hashlib
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import models as m
from .adapters.base import CONTENT_EXACT, CONTENT_HOST_ONLY, CONTENT_TITLE, parse_players
from .compat_db import CompatibilityDatabase
from .persistence import Document

LEVEL_RANK = {"none": 0, "title": 1, "same_file": 2, "exact": 3}
INDEX_TTL = 30.0
MAX_MATCH_QUERIES = 40


def match_level(wanted: dict, local: dict) -> str:
    """How closely ``local`` is the same content as ``wanted`` (both sampled-fingerprint dicts)."""
    wanted_fp, local_fp = wanted.get("fingerprint") or "", local.get("fingerprint") or ""
    if wanted_fp and local_fp:
        if wanted_fp == local_fp:
            return "exact"
        return "title" if wanted.get("title_key") and wanted.get("title_key") == local.get("title_key") else "none"
    same_name = str(wanted.get("rom_name", "")).lower() == str(local.get("rom_name", "")).lower() and wanted.get("rom_name")
    same_size = not wanted.get("size") or not local.get("size") or int(wanted["size"]) == int(local["size"])
    if same_name and same_size:
        return "same_file"
    if wanted.get("title_key") and wanted.get("title_key") == local.get("title_key"):
        return "title"
    return "none"


def level_satisfies(level: str, requirement: str) -> bool:
    if requirement == CONTENT_HOST_ONLY:
        return True
    if requirement == CONTENT_TITLE:
        return LEVEL_RANK.get(level, 0) >= LEVEL_RANK["title"]
    return level in ("exact", "same_file")


def level_note(level: str, requirement: str) -> str:
    if requirement == CONTENT_HOST_ONLY:
        return "Only the host needs this game."
    if level == "exact":
        return "Same content (fingerprints match)."
    if level == "same_file":
        return "Same file name and size, but not fingerprint-verified yet."
    if level == "title":
        return "Same title, different file." + (" This mechanism needs identical content." if requirement == CONTENT_EXACT else "")
    return "This machine does not have the game."


class GameLibrary:
    """Normalised, short-lived view of the local ROM cache for multiplayer."""

    def __init__(self, settings: Any, repository: Any) -> None:
        self.settings = settings
        self.repository = repository
        self._cache: Dict[str, tuple] = {}
        self._lock = threading.Lock()
        self._hashes = Document(settings, "content_hashes", dict)
        self._hash_view: Optional[tuple] = None

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    def systems(self) -> List[str]:
        try:
            return [str(row.get("name")).lower() for row in self.repository.list_systems() if row.get("name")]
        except Exception:  # noqa: BLE001 - a broken system directory must not break the page
            return []

    def _normalize(self, system: str, item: dict) -> Optional[dict]:
        rom_path = str(item.get("rom_path") or item.get("relative_path") or "")
        rom_name = Path(rom_path).name or str(item.get("name") or "")
        if not rom_name:
            return None
        details = item.get("gamelist") if isinstance(item.get("gamelist"), dict) else {}
        existing = item.get("existing") if isinstance(item.get("existing"), dict) else {}
        image = str(existing.get("image") or "")
        try:
            size = int(item.get("byte_count") or 0)
        except (TypeError, ValueError):
            size = 0
        return {
            "id": str(item.get("unique_id") or ""),
            "system": system,
            "title": str(item.get("title") or item.get("name") or Path(rom_name).stem),
            "rom_name": rom_name,
            "rom_path": rom_path,
            "title_key": m.normalize_title(rom_name),
            "fingerprint": str(item.get("fingerprint") or "").lower(),
            "size": size,
            "players": parse_players(details.get("players")),
            "image": Path(image).name if image else "",
            "entry_type": str(item.get("entry_type") or "file"),
        }

    def games(self, system: str) -> List[dict]:
        system = m.clean_system(system)
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(system)
            if cached and now - cached[0] < INDEX_TTL:
                return cached[1]
        try:
            _dir, items = self.repository.list_assets(system, "roms", include_fingerprint=True)
        except (FileNotFoundError, ValueError, OSError):
            items = []
        games = [game for game in (self._normalize(system, item) for item in items) if game and game["id"]]
        with self._lock:
            self._cache[system] = (now, games)
        return games

    def get(self, system: str, game_id: str) -> Optional[dict]:
        game_id = m.clean_unique_id(game_id)
        return next((game for game in self.games(system) if game["id"] == game_id), None)

    def absolute_path(self, game: dict) -> Optional[Path]:
        """The ROM's location under ``<roms>/<system>``; refused unless its symlink-resolved target stays inside that system."""
        root = Path(self.repository.roms_root) / game["system"]
        try:
            resolved_root = root.resolve(strict=True)
            (root / game["rom_path"]).resolve(strict=True).relative_to(resolved_root)
        except (OSError, ValueError, RuntimeError):
            return None
        # Hand back the path as EmulationStation lists it (symlinks intact): ES only launches paths it knows.
        return Path(os.path.abspath(str(root / game["rom_path"])))

    def content_hash(self, game: dict) -> str:
        """Full SHA-256 of the selected ROM only, cached by path/inode/size/mtime.

        Browser requests never call this. A file that changes during hashing is
        refused; directories/multi-file sets need an emulator-aware manifest.
        """
        path = self.absolute_path(game)
        if path is None or not path.is_file():
            raise m.MultiplayerError("content_unverifiable", "This game's content cannot be verified as a single ROM file.", 409)
        info = path.stat()
        stamp = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        cache = self._hashes.load().get(str(path))
        if cache and cache.get("stamp") == stamp:
            return cache["sha256"]
        digest = hashlib.sha256()
        deadline = time.monotonic() + 30.0
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                if time.monotonic() > deadline:
                    raise m.MultiplayerError("content_hash_timeout", "Verifying this ROM took too long. Try again on faster local storage.", 409)
        after = path.stat()
        if stamp != [after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns]:
            raise m.MultiplayerError("content_changed", "The ROM changed while it was being verified. Try again.", 409)
        result = digest.hexdigest()
        def save(entries):
            entries[str(path)] = {"stamp": stamp, "sha256": result}
            while len(entries) > 512:
                entries.pop(next(iter(entries)))
        self._hashes.update(save)
        self._hash_view = None
        return result

    def cached_content_hash(self, game: dict) -> str:
        """The full SHA-256 of this ROM if already computed for its *current* file state, else ``""``. Never hashes."""
        try:
            path = os.path.abspath(str(Path(self.repository.roms_root) / game["system"] / game["rom_path"]))
            info = os.stat(path)
        except (OSError, KeyError, TypeError):
            return ""
        now = time.monotonic()
        view = self._hash_view
        if view is None or now - view[0] > 5.0:
            view = self._hash_view = (now, self._hashes.load())
        entry = view[1].get(path)
        stamp = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        return entry["sha256"] if isinstance(entry, dict) and entry.get("stamp") == stamp else ""

    def find_copy(self, query: dict) -> Optional[dict]:
        """Best local copy of ``query`` ({system, rom_name, title_key, fingerprint, size}), with its match level."""
        best, best_rank = None, 0
        for game in self.games(query["system"]):
            level = match_level(query, game)
            rank = LEVEL_RANK[level]
            if rank > best_rank:
                best, best_rank = (game, level), rank
                if level == "exact":
                    break
        return {"game": best[0], "level": best[1]} if best else None


class GameAssessor:
    """Per-game multiplayer assessment across every adapter, with recorded evidence applied."""

    HASHED_MECHANISMS = (m.RETROARCH_NETPLAY, m.DOLPHIN_NETPLAY)

    def __init__(self, adapters: List[Any], compat: CompatibilityDatabase, overrides_provider, cached_hash=None) -> None:
        self.adapters = adapters
        self.compat = compat
        self._overrides = overrides_provider
        self._cached_hash = cached_hash

    def assess(self, game: dict, *, game_path: Optional[Path] = None, core_override: str = "") -> List[dict]:
        overrides = dict(self._overrides())
        if core_override:
            overrides["core_overrides"] = {**(overrides.get("core_overrides") or {}), game["system"]: core_override}
        disabled = set(overrides.get("disabled_adapters") or [])
        probe_game = dict(game)
        if game_path is not None:
            probe_game["path"] = game_path
        results: List[dict] = []
        sha: Optional[str] = None
        for adapter in self.adapters:
            if adapter.id in disabled or not adapter.supports_system(game["system"]):
                continue
            try:
                assessment = adapter.assess_game(probe_game, overrides)
            except Exception as error:  # noqa: BLE001 - one broken adapter must not break the library page
                assessment = adapter.assessment(adapter.mechanisms[0] if adapter.mechanisms else m.EMULATOR_NATIVE_NETWORK,
                                                m.UNKNOWN, [f"{adapter.name} could not be assessed: {type(error).__name__}"])
            if assessment is None:
                continue
            emulator = assessment["emulator"]
            evidence_game = game
            if self._cached_hash is not None and assessment["mechanism"] in self.HASHED_MECHANISMS:
                # Evidence for these mechanisms is bound to the full ROM content, not just the sampled fingerprint.
                if sha is None:
                    sha = self._cached_hash(game) or ""
                evidence_game = {**game, "content_sha256": sha}
            fingerprint = m.config_fingerprint(assessment["mechanism"], adapter.validation_fingerprint(evidence_game, emulator))
            verdict = self.compat.evaluate(game["system"], game["title_key"], assessment["mechanism"], fingerprint,
                                           assessment["classification"])
            assessment["technical_classification"] = assessment["classification"]
            assessment["classification"] = verdict["classification"]
            assessment["compatibility_basis"] = verdict["basis"]
            assessment["config_fingerprint"] = fingerprint
            assessment["evidence"] = verdict["evidence"]
            assessment["known_issues"] = verdict["known_issues"][-3:]
            assessment["requirements"] = list(assessment.get("requirements") or []) + verdict["requirements"]
            if assessment.get("max_players") is None:
                assessment["max_players"] = verdict["max_players"]
            if assessment["classification"] in (m.INCOMPATIBLE, m.UNSUPPORTED):
                assessment["ready"] = False
            results.append(assessment)
        order = {name: index for index, name in enumerate(m.MECHANISM_PRIORITY)}
        results.sort(key=lambda item: order.get(item["mechanism"], 99))
        return results


def browser_status(assessments: List[dict]) -> str:
    """Single filter bucket for the game browser: ready | configuration | untested | unsupported."""
    if any(item["ready"] and item["classification"] in (m.VERIFIED_COMPATIBLE, m.LIKELY_COMPATIBLE) for item in assessments):
        return "ready"
    if any(item["classification"] == m.REQUIRES_CONFIGURATION or (item["readiness"] in ("configuration_required",)
           and item["classification"] in m.PLAYABLE_CLASSES) for item in assessments):
        return "configuration"
    if any(item["classification"] in (m.UNKNOWN, m.EXPERIMENTAL) and item["readiness"] != "not_installed" for item in assessments):
        return "untested"
    return "unsupported"
