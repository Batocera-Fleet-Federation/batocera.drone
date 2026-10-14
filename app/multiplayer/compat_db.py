"""Versioned multiplayer compatibility database.

Records live in the Drone state DB (they grow incrementally from real sessions)
on top of a deliberately tiny bundled seed (``data/compat_seed.json``). It works
fully offline. A record looks like::

    {"system": "gamecube", "gameId": "<title key>", "title": "...",
     "mechanisms": {"DOLPHIN_NETPLAY": {...evidence...}}, "maximumPlayers": 4,
     "requirements": [], "knownIssues": [], "lastValidated": null, "source": "local"}

Evidence is per *tested configuration fingerprint* (mechanism + emulator +
emulator/core version). One success on one configuration is never universal
proof: ``VERIFIED_COMPATIBLE`` is reported only while the current configuration
matches a tested one with more successes than failures.
Records shared between swarm members keep their source attribution and are
capped below verified when imported.
"""

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import models as m
from .persistence import Document

DB_VERSION = 1
SEED_PATH = Path(__file__).with_name("data") / "compat_seed.json"
MAX_RECORDS = 5000
MAX_ISSUES = 20
SHAREABLE_LIMIT = 500


def record_key(system: str, title_key: str) -> str:
    return f"{system}|{title_key}"


def load_seed(path: Path = SEED_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema_version": DB_VERSION, "systems": {}, "games": []}
    return data if isinstance(data, dict) else {"schema_version": DB_VERSION, "systems": {}, "games": []}


class CompatibilityDatabase:
    def __init__(self, settings: Any, seed_path: Path = SEED_PATH) -> None:
        self._doc = Document(settings, "compatibility", lambda: {"version": DB_VERSION, "records": {}})
        self._seed = load_seed(seed_path)
        self._snapshot: Optional[tuple] = None

    # ------------------------------------------------------------------- reads
    def system_hint(self, system: str) -> dict:
        hint = (self._seed.get("systems") or {}).get(system)
        return dict(hint) if isinstance(hint, dict) else {}

    def _records(self) -> Dict[str, dict]:
        now = time.monotonic()
        if self._snapshot and now - self._snapshot[0] < 5.0:
            return self._snapshot[1]
        records = self._build_records()
        self._snapshot = (now, records)
        return records

    def _build_records(self) -> Dict[str, dict]:
        records: Dict[str, dict] = {}
        for item in self._seed.get("games") or []:
            if isinstance(item, dict) and item.get("system") and item.get("gameId"):
                records[record_key(str(item["system"]), str(item["gameId"]))] = {**item, "source": item.get("source") or "builtin"}
        records.update(self._doc.load().get("records", {}))
        return records

    def get(self, system: str, title_key: str) -> Optional[dict]:
        return self._records().get(record_key(system, title_key))

    def all(self) -> List[dict]:
        return sorted(self._records().values(), key=lambda item: (item.get("system", ""), item.get("title", "")))

    def evaluate(self, system: str, title_key: str, mechanism: str, config_fp: str, technical: str) -> dict:
        """Combine the adapter's technical class with recorded evidence for this configuration."""
        record = self.get(system, title_key)
        result = {"classification": technical, "basis": "technical", "evidence": None, "known_issues": [], "max_players": None,
                  "requirements": []}
        if not record:
            return result
        result["known_issues"] = list(record.get("knownIssues") or [])
        result["requirements"] = list(record.get("requirements") or [])
        result["max_players"] = record.get("maximumPlayers")
        evidence = (record.get("mechanisms") or {}).get(mechanism)
        if not isinstance(evidence, dict):
            return result
        tested = (evidence.get("tested") or {}).get(config_fp)
        declared = evidence.get("compatibility")
        if isinstance(tested, dict):
            successes, failures = int(tested.get("successes", 0)), int(tested.get("failures", 0))
            result["evidence"] = {"config": config_fp, "successes": successes, "failures": failures,
                                  "last": tested.get("last"), "source": record.get("source", "local")}
            if failures > successes:
                result.update(classification=m.INCOMPATIBLE, basis="failed-sessions")
            elif successes > failures and record.get("source", "local") in ("local", "builtin"):
                if technical not in (m.UNSUPPORTED, m.INCOMPATIBLE):
                    result.update(classification=m.VERIFIED_COMPATIBLE, basis="confirmed-session")
            elif successes > 0:
                # Another machine's report is attribution only: it never raises the local classification.
                result["basis"] = "shared-report"
            return result
        local_source = record.get("source", "local") in ("local", "builtin")
        if declared in m.COMPATIBILITY_CLASSES and declared != m.VERIFIED_COMPATIBLE:
            # A declared class applies, but only evidence from this exact configuration can verify. Peer-declared
            # records can only lower the class, never lift an untested game.
            if declared in (m.INCOMPATIBLE, m.UNSUPPORTED) or (
                    local_source and technical in (m.UNKNOWN, m.EXPERIMENTAL, m.REQUIRES_CONFIGURATION)):
                result.update(classification=declared, basis="record")
        elif declared == m.VERIFIED_COMPATIBLE and technical not in (m.UNSUPPORTED, m.INCOMPATIBLE):
            result["basis"] = "record-other-configuration"
        return result

    # ------------------------------------------------------------------ writes
    def record_session_result(self, *, system: str, title_key: str, title: str, mechanism: str, config_fp: str,
                              emulator_version: str, success: bool, max_players: Optional[int] = None,
                              failure: str = "") -> dict:
        """Remember how a real session went. Counts per tested configuration, never global."""
        key = record_key(system, title_key)

        def apply(doc: dict) -> dict:
            doc["version"] = DB_VERSION
            records = doc.setdefault("records", {})
            record = records.setdefault(key, {
                "system": system, "gameId": title_key, "title": m.clean_text(title, m.MAX_TITLE), "mechanisms": {},
                "maximumPlayers": None, "requirements": [], "knownIssues": [], "verifiedEmulatorVersions": [],
                "lastValidated": None, "source": "local",
            })
            if record.get("source") not in ("local",):
                # A local observation must not turn the peer's other reports into verified evidence.
                record["mechanisms"] = {}
                record["verifiedEmulatorVersions"] = []
                record["lastValidated"] = None
                record["source"] = "local"
            evidence = record["mechanisms"].setdefault(mechanism, {"compatibility": m.UNKNOWN, "tested": {}})
            tested = evidence["tested"].setdefault(config_fp, {"successes": 0, "failures": 0, "emulator_version": m.clean_version(emulator_version)})
            tested["successes" if success else "failures"] += 1
            tested["last"] = m.now_iso()
            if max_players:
                record["maximumPlayers"] = max(int(record.get("maximumPlayers") or 0), int(max_players))
            if success:
                record["lastValidated"] = tested["last"]
                version = m.clean_version(emulator_version)
                if version and version not in record["verifiedEmulatorVersions"]:
                    record["verifiedEmulatorVersions"].append(version)
                    del record["verifiedEmulatorVersions"][:-10]
            elif failure:
                issues = record["knownIssues"]
                issues.append({"at": tested["last"], "config": config_fp, "detail": m.clean_text(failure, 300)})
                del issues[:-MAX_ISSUES]
            if len(records) > MAX_RECORDS:
                oldest = sorted(records, key=lambda name: records[name].get("lastValidated") or "")[: len(records) - MAX_RECORDS]
                for name in oldest:
                    records.pop(name, None)
            return dict(record)
        result = self._doc.update(apply)
        self._snapshot = None
        return result

    # ----------------------------------------------------------------- sharing
    def export_shareable(self) -> List[dict]:
        """Locally confirmed records only, with their evidence counts (no per-machine detail)."""
        shared = []
        for record in self._doc.load().get("records", {}).values():
            if record.get("source") != "local" or not record.get("lastValidated"):
                continue
            mechanisms = {}
            for name, evidence in (record.get("mechanisms") or {}).items():
                tested = {fp: {"successes": item.get("successes", 0), "failures": item.get("failures", 0),
                               "emulator_version": item.get("emulator_version", ""), "last": item.get("last")}
                          for fp, item in (evidence.get("tested") or {}).items() if item.get("successes")}
                if tested:
                    mechanisms[name] = {"compatibility": m.LIKELY_COMPATIBLE, "tested": tested}
            if mechanisms:
                shared.append({"system": record["system"], "gameId": record["gameId"], "title": record.get("title", ""),
                               "maximumPlayers": record.get("maximumPlayers"), "mechanisms": mechanisms,
                               "lastValidated": record.get("lastValidated")})
        return shared[:SHAREABLE_LIMIT]

    def import_shared(self, records: Any, source_peer_id: str, source_name: str = "") -> dict:
        """Merge a paired peer's report. Attribution is kept; nothing is elevated to verified."""
        if not isinstance(records, list):
            raise m.MultiplayerError("invalid_body", "records must be a list")
        added = updated = skipped = 0
        source = f"peer:{m.clean_peer_id(source_peer_id)}"

        def apply(doc: dict) -> None:
            nonlocal added, updated, skipped
            store = doc.setdefault("records", {})
            for raw in records[:SHAREABLE_LIMIT]:
                try:
                    system = m.clean_system(raw.get("system"))
                    title_key = m.clean_text(raw.get("gameId"), 120).lower()
                    if not title_key:
                        raise m.MultiplayerError("invalid_game", "gameId required")
                    mechanisms = {}
                    for name, evidence in (raw.get("mechanisms") or {}).items():
                        mechanism = m.clean_mechanism(name)
                        tested = {}
                        for fp, item in list((evidence.get("tested") or {}).items())[:20]:
                            if not isinstance(fp, str) or len(fp) > 32 or not isinstance(item, dict):
                                continue
                            tested[fp] = {"successes": max(0, min(int(item.get("successes", 0)), 1000)),
                                          "failures": max(0, min(int(item.get("failures", 0)), 1000)),
                                          "emulator_version": m.clean_version(item.get("emulator_version")),
                                          "last": m.clean_text(item.get("last"), 40)}
                        if tested:
                            mechanisms[mechanism] = {"compatibility": m.LIKELY_COMPATIBLE, "tested": tested}
                except (m.MultiplayerError, AttributeError, TypeError, ValueError, OverflowError):
                    skipped += 1
                    continue
                key = record_key(system, title_key)
                existing = store.get(key)
                if existing and existing.get("source") == "local":
                    skipped += 1  # never overwrite what this machine observed itself
                    continue
                players = raw.get("maximumPlayers")
                store[key] = {"system": system, "gameId": title_key, "title": m.clean_text(raw.get("title"), m.MAX_TITLE),
                              "mechanisms": mechanisms, "maximumPlayers": players if isinstance(players, int) and 1 <= players <= 16 else None,
                              "requirements": [], "knownIssues": [], "verifiedEmulatorVersions": [],
                              "lastValidated": None, "source": source, "source_name": m.clean_text(source_name, 80),
                              "imported_at": m.now_iso()}
                if existing:
                    updated += 1
                else:
                    added += 1
        self._doc.update(apply)
        self._snapshot = None
        return {"added": added, "updated": updated, "skipped": skipped}
