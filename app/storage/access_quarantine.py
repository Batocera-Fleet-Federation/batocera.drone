"""Deny-list for admin credentials that were copied onto a held clone.

When a clone is held, its inherited browser sessions and MCP bearer must stop working even
if SQLite refuses to delete or rewrite their rows. Only SHA-256 digests are kept, in memory
and in a file beside the state database, so a restart keeps the denial until the full swarm
reset removes the rows. Tokens issued afterwards are unaffected.

Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Dict, Iterable, Set

QUARANTINE_FILENAME = "access-quarantine.json"

_LOCK = threading.Lock()
_MEMORY: Dict[str, Dict[str, Set[str]]] = {}


def digest(secret: str) -> str:
    return hashlib.sha256(str(secret or "").strip().encode("utf-8")).hexdigest()


def _key(db_path: Path) -> str:
    return str(Path(db_path).resolve())


def _file(db_path: Path) -> Path:
    return Path(db_path).resolve().with_name(QUARANTINE_FILENAME)


def _load(db_path: Path) -> Dict[str, Set[str]]:
    entry = _MEMORY.setdefault(_key(db_path), {"sessions": set(), "mcp": set()})
    try:
        stored = json.loads(_file(db_path).read_text(encoding="utf-8"))
        for name in entry:
            entry[name].update(str(item) for item in stored.get(name, []) if isinstance(item, str))
    except (OSError, ValueError, AttributeError):
        pass
    return entry


def deny(db_path: Path, *, sessions: Iterable[str] = (), mcp: Iterable[str] = ()) -> None:
    """Remember digests of session tokens / MCP bearer digests that must never validate again."""
    with _LOCK:
        entry = _load(db_path)
        entry["sessions"].update(digest(token) for token in sessions)
        entry["mcp"].update(str(item) for item in mcp if item)
        payload = {name: sorted(values) for name, values in entry.items()}
        target = _file(db_path)
        temporary = target.with_name(target.name + ".tmp")
        try:
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temporary, target)
        except OSError:
            # The in-memory denial still applies for this run; the caller logs the SQLite outcome.
            pass


def session_denied(db_path: Path, token: str) -> bool:
    with _LOCK:
        return digest(token) in _load(db_path)["sessions"]


def mcp_denied(db_path: Path, token_digest: str) -> bool:
    with _LOCK:
        return str(token_digest) in _load(db_path)["mcp"]
