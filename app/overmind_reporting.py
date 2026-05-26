"""Change-only payload collection for Drone-to-Overmind reporting."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Tuple


_STATE_SCHEMA_VERSION = 2
_CONFIG_SUFFIXES = {".cfg", ".conf", ".ini", ".json", ".toml", ".xml", ".yml", ".yaml", ".bml", ".reg"}


def _state_path(settings: Any, filename: str) -> Path:
    return (settings.userdata_root / "system" / "drone-app" / filename).resolve()


def _read_json_file(path: Path, fallback: Any) -> Any:
    try:
        if path.exists() and path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return fallback


def _write_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary_path.replace(path)


def _read_delivery_state(settings: Any, filename: str, key: str) -> dict:
    state = _read_json_file(_state_path(settings, filename), {})
    if not isinstance(state, dict) or state.get("schema_version") != _STATE_SCHEMA_VERSION:
        return {}
    values = state.get(key)
    return values if isinstance(values, dict) else {}


def _commit_delivery_state(settings: Any, filename: str, key: str, values: dict) -> None:
    _write_json_file(
        _state_path(settings, filename),
        {"schema_version": _STATE_SCHEMA_VERSION, key: dict(values or {})},
    )


def load_uploaded_log_cursors(settings: Any) -> dict:
    return _read_delivery_state(settings, "overmind_log_cursors.json", "cursors")


def commit_log_cursors(settings: Any, cursors: dict) -> None:
    _commit_delivery_state(settings, "overmind_log_cursors.json", "cursors", cursors)


def load_uploaded_emulator_config_fingerprints(settings: Any) -> dict:
    return _read_delivery_state(settings, "overmind_config_fingerprints.json", "fingerprints")


def commit_emulator_config_fingerprints(settings: Any, fingerprints: dict) -> None:
    _commit_delivery_state(settings, "overmind_config_fingerprints.json", "fingerprints", fingerprints)


def _resolve_userdata_path(settings: Any, candidate: str) -> Path:
    if candidate == "/userdata":
        return settings.userdata_root.resolve()
    if candidate.startswith("/userdata/"):
        return (settings.userdata_root / candidate[len("/userdata/") :]).resolve()
    return Path(candidate).resolve()


def _read_text_file(path: Path, max_bytes: int) -> dict:
    try:
        raw = path.read_bytes()[: max_bytes + 1]
        truncated = len(raw) > max_bytes
        if truncated:
            raw = raw[:max_bytes]
        return {
            "path": str(path),
            "size": path.stat().st_size,
            "truncated": truncated,
            "content": raw.decode("utf-8", errors="replace"),
        }
    except Exception as error:
        return {"path": str(path), "error": str(error)}


def _read_text_file_delta(path: Path, cursor: dict, max_bytes: int) -> Tuple[dict, dict]:
    try:
        stat = path.stat()
        key = str(path.resolve())
        previous = cursor.get(key) if isinstance(cursor.get(key), dict) else {}
        previous_size = int(previous.get("size") or 0)
        size = int(stat.st_size)
        previous_mtime_ns = int(previous.get("mtime_ns") or 0)
        mtime_ns = int(stat.st_mtime_ns)
        if size > previous_size >= 0:
            start = previous_size
        elif size == previous_size and mtime_ns == previous_mtime_ns:
            start = size
        else:
            start = max(0, size - max_bytes)
        with path.open("rb") as handle:
            handle.seek(start)
            raw = handle.read(max_bytes + 1)
        truncated = len(raw) > max_bytes
        if truncated:
            raw = raw[:max_bytes]
        next_cursor = {"size": start + len(raw), "mtime_ns": mtime_ns}
        return {
            "path": str(path),
            "size": size,
            "offset": start,
            "truncated": truncated,
            "content": raw.decode("utf-8", errors="replace"),
            "delta": True,
        }, next_cursor
    except Exception as error:
        return {"path": str(path), "error": str(error), "delta": True}, {}


def collect_log_sources(settings: Any, include_unchanged: bool = False) -> dict:
    """Build changed log source payloads and deferred delivery cursors."""
    candidates = {
        "es_launch_stdout": ["/userdata/system/logs/es_launch_stdout.log"],
        "es_launch_stderr": ["/userdata/system/logs/es_launch_stderr.log"],
        "drone_stdout": [str((settings.log_dir / settings.stdout_log_file).resolve())],
        "drone_stderr": [str((settings.log_dir / settings.stderr_log_file).resolve())],
    }
    cursor = {} if include_unchanged else load_uploaded_log_cursors(settings)
    next_cursor = dict(cursor)
    logs = []
    for source, paths in candidates.items():
        entry = {"source": source, "files": []}
        for raw_path in paths:
            path = _resolve_userdata_path(settings, raw_path)
            if path.exists() and path.is_file():
                file_info, file_cursor = _read_text_file_delta(path, cursor, max_bytes=262144)
                if file_cursor:
                    next_cursor[str(path.resolve())] = file_cursor
                if str(file_info.get("content") or "") or file_info.get("error"):
                    entry["files"].append(file_info)
        if entry["files"]:
            logs.append(entry)
    return {
        "type": "log_sources",
        "collected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "logs": logs,
        "append": True,
        "_cursors": next_cursor,
    }


def collect_emulator_configs(settings: Any, include_unchanged: bool = False) -> dict:
    """Build changed emulator config payloads and deferred fingerprints."""
    roots = [
        settings.userdata_root / "system" / "configs",
        settings.userdata_root / "system" / ".config",
    ]
    previous_fingerprints = load_uploaded_emulator_config_fingerprints(settings)
    next_fingerprints = {}
    configs = []
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if len(configs) >= 250:
                break
            if not path.is_file() or path.suffix.lower() not in _CONFIG_SUFFIXES:
                continue
            if ".bak" in path.name.lower() or ".bak" in str(path.relative_to(root)).lower():
                continue
            item = _read_text_file(path, max_bytes=131072)
            try:
                item["relative_path"] = str(path.relative_to(root))
            except Exception:
                item["relative_path"] = path.name
            item["root"] = str(root)
            key = f"{item['root']}:{item['relative_path']}"
            fingerprint = hashlib.sha256(str(item.get("content") or "").encode("utf-8", errors="replace")).hexdigest()
            next_fingerprints[key] = fingerprint
            if include_unchanged or previous_fingerprints.get(key) != fingerprint:
                item["fingerprint"] = fingerprint
                configs.append(item)
    return {
        "type": "emulator_configs",
        "collected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "configs": configs,
        "changed": bool(configs),
        "incremental": not include_unchanged,
        "_fingerprints": next_fingerprints,
    }
