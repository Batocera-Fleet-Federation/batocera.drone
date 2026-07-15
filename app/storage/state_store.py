"""SQLite-backed persistent state owned by the Drone application."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


DATABASE_FILENAME = "rom_metadata_cache.sqlite3"
PEER_ROUTE_KINDS = frozenset({"tailnet", "host", "ip"})


def database_path(userdata_root: Path) -> Path:
    configured = os.environ.get("DRONE_STATE_DATABASE_FILE")
    if configured:
        return Path(configured).resolve()
    return (userdata_root / "system" / "drone-app" / DATABASE_FILENAME).resolve()


def database_path_for_legacy_file(path: Path) -> Path:
    configured = os.environ.get("DRONE_STATE_DATABASE_FILE")
    if configured:
        return Path(configured).resolve()
    return path.resolve().parent / DATABASE_FILENAME


def open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=30)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS app_state ("
        "namespace TEXT NOT NULL, state_key TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "PRIMARY KEY (namespace, state_key))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS app_events ("
        "sequence INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT NOT NULL, payload TEXT NOT NULL, "
        "created_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS peer_route_cache ("
        "peer_id TEXT PRIMARY KEY, address TEXT NOT NULL, route_kind TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "CHECK (route_kind IN ('tailnet', 'host', 'ip')))"
    )
    connection.commit()
    return connection


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_legacy_json(path: Optional[Path]) -> Any:
    if not path:
        return None
    try:
        if path.exists() and path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None


def load_payload(
    db_path: Path,
    namespace: str,
    fallback: Any,
    *,
    legacy_path: Optional[Path] = None,
    state_key: str = "payload",
) -> Any:
    with open_database(db_path) as connection:
        row = connection.execute(
            "SELECT payload FROM app_state WHERE namespace = ? AND state_key = ?",
            (namespace, state_key),
        ).fetchone()
    if row:
        try:
            return json.loads(row[0])
        except Exception:
            return fallback
    legacy = _read_legacy_json(legacy_path)
    if legacy is not None:
        save_payload(db_path, namespace, legacy, state_key=state_key)
        try:
            legacy_path.unlink(missing_ok=True)  # type: ignore[union-attr]
        except OSError:
            pass
        return legacy
    return fallback


def save_payload(db_path: Path, namespace: str, payload: Any, *, state_key: str = "payload") -> None:
    with open_database(db_path) as connection:
        connection.execute(
            "INSERT INTO app_state (namespace, state_key, payload, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(namespace, state_key) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
            (namespace, state_key, json.dumps(payload, sort_keys=True, default=str), _now()),
        )


def load_peer_route(db_path: Path, peer_id: str) -> Optional[dict]:
    """Return the single cached successful route for a peer, if present."""
    normalized = str(peer_id or "").strip()
    if not normalized:
        return None
    with open_database(db_path) as connection:
        row = connection.execute(
            "SELECT address, route_kind, updated_at FROM peer_route_cache WHERE peer_id = ?",
            (normalized,),
        ).fetchone()
    if not row:
        return None
    return {
        "peer_id": normalized,
        "address": str(row[0]),
        "route_kind": str(row[1]),
        "updated_at": str(row[2]),
    }


def save_peer_route(db_path: Path, peer_id: str, address: str, route_kind: str) -> dict:
    """Insert or replace a peer's current route without retaining history."""
    normalized_peer_id = str(peer_id or "").strip()
    normalized_address = str(address or "").strip().rstrip("/")
    normalized_kind = str(route_kind or "").strip().lower()
    if not normalized_peer_id or not normalized_address:
        raise ValueError("peer route requires peer_id and address")
    if normalized_kind not in PEER_ROUTE_KINDS:
        raise ValueError(f"unsupported peer route kind: {normalized_kind}")
    updated_at = _now()
    with open_database(db_path) as connection:
        connection.execute(
            "INSERT INTO peer_route_cache (peer_id, address, route_kind, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(peer_id) DO UPDATE SET "
            "address=excluded.address, route_kind=excluded.route_kind, updated_at=excluded.updated_at",
            (normalized_peer_id, normalized_address, normalized_kind, updated_at),
        )
    return {
        "peer_id": normalized_peer_id,
        "address": normalized_address,
        "route_kind": normalized_kind,
        "updated_at": updated_at,
    }


def delete_peer_route(db_path: Path, peer_id: str) -> None:
    normalized = str(peer_id or "").strip()
    if not normalized:
        return
    with open_database(db_path) as connection:
        connection.execute("DELETE FROM peer_route_cache WHERE peer_id = ?", (normalized,))


def append_event(db_path: Path, namespace: str, payload: Any, *, max_events: Optional[int] = None) -> None:
    with open_database(db_path) as connection:
        connection.execute(
            "INSERT INTO app_events (namespace, payload, created_at) VALUES (?, ?, ?)",
            (namespace, json.dumps(payload, sort_keys=True, default=str), _now()),
        )
        if max_events and max_events > 0:
            connection.execute(
                "DELETE FROM app_events WHERE namespace = ? AND sequence NOT IN ("
                "SELECT sequence FROM app_events WHERE namespace = ? ORDER BY sequence DESC LIMIT ?)",
                (namespace, namespace, max_events),
            )


def load_events(
    db_path: Path,
    namespace: str,
    *,
    legacy_path: Optional[Path] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    with open_database(db_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM app_events WHERE namespace = ?",
            (namespace,),
        ).fetchone()[0]
        if count == 0 and legacy_path and legacy_path.exists():
            try:
                legacy = [
                    json.loads(line)
                    for line in legacy_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            except Exception:
                legacy = []
            for entry in legacy:
                if isinstance(entry, dict):
                    connection.execute(
                        "INSERT INTO app_events (namespace, payload, created_at) VALUES (?, ?, ?)",
                        (namespace, json.dumps(entry, sort_keys=True, default=str), _now()),
                    )
            connection.commit()
            if legacy:
                try:
                    legacy_path.unlink(missing_ok=True)
                except OSError:
                    pass
        sql = "SELECT payload FROM app_events WHERE namespace = ? ORDER BY sequence DESC"
        parameters: list[Any] = [namespace]
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        rows = connection.execute(sql, parameters).fetchall()
    entries = []
    for row in rows:
        try:
            value = json.loads(row[0])
            if isinstance(value, dict):
                entries.append(value)
        except Exception:
            continue
    return entries
