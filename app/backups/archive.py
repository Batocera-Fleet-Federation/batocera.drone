"""Backup archive format: build, verify, and read (stdlib only).

An archive is a gzip tar holding ``data/<root>/<path>`` regular files followed by a
``manifest.json`` member (the manifest needs every file's SHA-256, so it is written
last). A sidecar copy of the manifest is kept next to each local archive so listing
never has to decompress anything.

Manifest (``format`` ``batocera-drone-backup``, ``version`` 1)::

    {"backup_id": "<32 hex>", "kind": "backup"|"snapshot", "name", "description",
     "created_at", "source": {"drone_id", "drone_name", "hostname", "batocera_version",
     "architecture", "board"}, "files": [{"path", "category", "size", "sha256",
     "redacted"?: [[section, key], ...]}], "skipped_count", "skipped_bytes",
     "skipped": [...], "snapshot_of"?: {...}}

Archives from peers are untrusted. ``verify_archive`` streams the whole file once,
checks its SHA-256 against the pinned peer's listing, refuses anything but regular
files under ``data/`` with safe relative paths, recomputes every member's size and
SHA-256, and requires the manifest to describe exactly those members, each filed under
the category ``categories.classify`` assigns to its path.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Dict, Iterable, List, Optional

try:
    from . import categories as _categories
    from . import confmerge as _confmerge
except ImportError:  # pragma: no cover - flat execution
    from backups import categories as _categories  # type: ignore
    from backups import confmerge as _confmerge  # type: ignore

FORMAT = "batocera-drone-backup"
VERSION = 1
MANIFEST_MEMBER = "manifest.json"
DATA_PREFIX = "data/"
MAX_FILES = 200_000
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024 * 1024
MAX_SKIPPED_LISTED = 200
BACKUP_ID = re.compile(r"^[0-9a-f]{32}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
KINDS = ("backup", "snapshot")
_CHUNK = 1024 * 1024


class BackupError(ValueError):
    def __init__(self, message: str, status: int = 400, details: Optional[dict] = None):
        super().__init__(message)
        self.status = status
        self.details = details or {}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def safe_relative_path(value: Any) -> str:
    """A normalized archive path such as ``saves/snes/game.srm``, or BackupError."""
    if not isinstance(value, str) or not value or len(value) > 1024 or "\\" in value or "\x00" in value:
        raise BackupError("Invalid path in backup.")
    parts = value.split("/")
    if len(parts) < 2 or len(parts) > 40 or parts[0] not in _categories.ROOT_NAMES:
        raise BackupError("Invalid path in backup.")
    if any(part in ("", ".", "..") or part.startswith(".drone-") for part in parts):
        raise BackupError("Invalid path in backup.")
    return value


def _clean_text(value: Any, limit: int) -> str:
    text = str(value or "") if isinstance(value, (str, int, float)) else ""
    return "".join(ch for ch in text if ch == "\n" or ord(ch) >= 32)[:limit]


def _clean_source(value: Any) -> dict:
    value = value if isinstance(value, dict) else {}
    return {key: _clean_text(value.get(key), 200) for key in
            ("drone_id", "drone_name", "hostname", "batocera_version", "architecture", "board")}


def _clean_redacted(value: Any) -> List[List[Optional[str]]]:
    if value in (None, []):
        return []
    if not isinstance(value, list) or len(value) > 1000:
        raise BackupError("Invalid redaction list in backup.")
    pairs = []
    for pair in value:
        if (not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[1], str) or not pair[1]
                or len(pair[1]) > 300 or not (pair[0] is None or isinstance(pair[0], str) and len(pair[0]) <= 200)):
            raise BackupError("Invalid redaction list in backup.")
        pairs.append([pair[0], pair[1]])
    return pairs


def validate_manifest(raw: Any, *, expected_id: Optional[str] = None) -> dict:
    """A sanitized copy of an (untrusted) manifest, or BackupError."""
    if not isinstance(raw, dict) or raw.get("format") != FORMAT:
        raise BackupError("This is not a Drone backup.")
    if raw.get("version") != VERSION:
        raise BackupError("This backup was made by a newer Drone. Update this Drone to use it.", 409)
    backup_id = raw.get("backup_id")
    if not isinstance(backup_id, str) or not BACKUP_ID.match(backup_id):
        raise BackupError("Invalid backup id.")
    if expected_id is not None and backup_id != expected_id:
        raise BackupError("The backup id does not match the requested backup.", 409)
    kind = raw.get("kind") if raw.get("kind") in KINDS else "backup"
    files = raw.get("files")
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise BackupError("Invalid file list in backup.")
    clean_files, seen, total = [], set(), 0
    for item in files:
        if not isinstance(item, dict):
            raise BackupError("Invalid file entry in backup.")
        path = safe_relative_path(item.get("path"))
        if path in seen:
            raise BackupError("Duplicate file in backup.")
        seen.add(path)
        size, digest = item.get("size"), item.get("sha256")
        if type(size) is not int or size < 0 or not isinstance(digest, str) or not SHA256.match(digest):
            raise BackupError("Invalid file entry in backup.")
        category = _categories.classify(path)
        if category is None or item.get("category") != category:
            raise BackupError(f"Backup files a path under the wrong category: {path}")
        total += size
        entry = {"path": path, "category": category, "size": size, "sha256": digest}
        redacted = _clean_redacted(item.get("redacted"))
        if redacted:
            entry["redacted"] = redacted
        clean_files.append(entry)
    if total > MAX_TOTAL_BYTES:
        raise BackupError("Backup is too large.")
    skipped = []
    for item in (raw.get("skipped") if isinstance(raw.get("skipped"), list) else [])[:MAX_SKIPPED_LISTED]:
        if isinstance(item, dict):
            skipped.append({"path": _clean_text(item.get("path"), 1024), "size": int(item.get("size") or 0)
                            if isinstance(item.get("size"), int) else 0, "reason": _clean_text(item.get("reason"), 200)})
    snapshot_of = raw.get("snapshot_of") if isinstance(raw.get("snapshot_of"), dict) else None
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "backup_id": backup_id,
        "kind": kind,
        "name": _clean_text(raw.get("name"), 120),
        "description": _clean_text(raw.get("description"), 1000),
        "created_at": _clean_text(raw.get("created_at"), 40),
        "source": _clean_source(raw.get("source")),
        "files": clean_files,
        "categories": _categories.summarize(clean_files),
        "file_count": len(clean_files),
        "size_bytes": total,
        "skipped_count": raw.get("skipped_count") if isinstance(raw.get("skipped_count"), int) else len(skipped),
        "skipped_bytes": raw.get("skipped_bytes") if isinstance(raw.get("skipped_bytes"), int) else 0,
        "skipped": skipped,
    }
    if snapshot_of:
        restored = [value for value in snapshot_of.get("categories") or [] if _categories.valid_category(value)]
        manifest["snapshot_of"] = {
            "backup_id": _clean_text(snapshot_of.get("backup_id"), 32),
            "name": _clean_text(snapshot_of.get("name"), 120),
            "categories": restored[:2000],
        }
        paths = snapshot_of.get("paths")
        if isinstance(paths, list):
            clean_paths = []
            for value in paths[:5000]:
                try:
                    clean_paths.append(safe_relative_path(value))
                except BackupError:
                    continue
            manifest["snapshot_of"]["paths"] = clean_paths
    return manifest


class _HashingReader(io.RawIOBase):
    """Feeds tarfile exactly ``size`` bytes while hashing them."""

    def __init__(self, handle: BinaryIO, size: int) -> None:
        self._handle, self._remaining = handle, size
        self.digest = hashlib.sha256()

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        want = self._remaining if size is None or size < 0 else min(size, self._remaining)
        chunk = self._handle.read(want)
        if not chunk:
            raise OSError("file shrank while it was being backed up")
        self._remaining -= len(chunk)
        self.digest.update(chunk)
        return chunk


def _tar_info(name: str, size: int, mtime: float) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = int(mtime)
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.type = tarfile.REGTYPE
    return info


def _scrubbable(entry) -> bool:
    return (entry.arcpath.startswith("system/") and Path(entry.arcpath).suffix.lower() in _confmerge.SCRUBBABLE_SUFFIXES
            and entry.size <= _categories.MAX_CONFIG_FILE_BYTES)


def build_archive(
    roots: "_categories.Roots",
    destination: Path,
    *,
    backup_id: str,
    kind: str,
    name: str,
    description: str,
    categories: Optional[Iterable[str]],
    source: dict,
    snapshot_of: Optional[dict] = None,
    include_saves: bool = True,
) -> dict:
    """Write an archive atomically to ``destination``; returns its validated manifest."""
    collection = _categories.collect(roots, categories, include_saves=include_saves)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    files: List[dict] = []
    skipped = list(collection.skipped)
    try:
        with tarfile.open(temporary, "w:gz", format=tarfile.PAX_FORMAT) as tar:
            for entry in collection.files:
                try:
                    handle = open(entry.absolute, "rb", buffering=0)
                except OSError as error:
                    skipped.append({"path": entry.arcpath, "size": entry.size, "reason": f"unreadable: {error.strerror or error}"})
                    continue
                with handle:
                    info = os.fstat(handle.fileno())
                    record = {"path": entry.arcpath, "category": entry.category}
                    if _scrubbable(entry):
                        text, redacted = _confmerge.scrub(_confmerge.decode(handle.read()), Path(entry.arcpath).suffix)
                        data = _confmerge.encode(text)
                        tar.addfile(_tar_info(DATA_PREFIX + entry.arcpath, len(data), info.st_mtime), io.BytesIO(data))
                        record.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
                        if redacted:
                            record["redacted"] = redacted
                    else:
                        reader = _HashingReader(handle, info.st_size)
                        tar.addfile(_tar_info(DATA_PREFIX + entry.arcpath, info.st_size, info.st_mtime), reader)
                        record.update(size=info.st_size, sha256=reader.digest.hexdigest())
                files.append(record)
            manifest = {
                "format": FORMAT,
                "version": VERSION,
                "backup_id": backup_id,
                "kind": kind,
                "name": name,
                "description": description,
                "created_at": now_iso(),
                "source": source,
                "files": files,
                "skipped_count": len(skipped),
                "skipped_bytes": sum(int(item.get("size") or 0) for item in skipped),
                "skipped": skipped[:MAX_SKIPPED_LISTED],
            }
            if snapshot_of:
                manifest["snapshot_of"] = snapshot_of
            payload = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
            tar.addfile(_tar_info(MANIFEST_MEMBER, len(payload), time.time()), io.BytesIO(payload))
        os.replace(temporary, destination)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return validate_manifest(manifest)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_members(archive_path: Path):
    """Yield ``(member, fileobj)`` for each member, streaming (no seeking, no extraction)."""
    with tarfile.open(archive_path, "r|gz") as tar:
        for member in tar:
            yield member, (tar.extractfile(member) if member.isreg() else None)


def verify_archive(archive_path: Path, *, expected_id: Optional[str] = None,
                   expected_sha256: Optional[str] = None) -> dict:
    """Prove an archive is a well-formed, self-consistent backup; returns its manifest."""
    if expected_sha256 is not None and file_sha256(archive_path) != expected_sha256:
        raise BackupError("The downloaded backup does not match the listing of the source Drone.", 409)
    actual: Dict[str, tuple] = {}
    manifest_raw = None
    try:
        for member, handle in iter_members(archive_path):
            if manifest_raw is not None:
                raise BackupError("Unexpected data after the backup manifest.")
            if not member.isreg() or handle is None:
                raise BackupError("Backups may only contain regular files.")
            if member.name == MANIFEST_MEMBER:
                if member.size > MAX_MANIFEST_BYTES:
                    raise BackupError("Backup manifest is too large.")
                manifest_raw = handle.read()
                continue
            if not member.name.startswith(DATA_PREFIX):
                raise BackupError("Unexpected file in backup.")
            path = safe_relative_path(member.name[len(DATA_PREFIX):])
            if path in actual or len(actual) >= MAX_FILES:
                raise BackupError("Duplicate or too many files in backup.")
            digest, size = hashlib.sha256(), 0
            for chunk in iter(lambda: handle.read(_CHUNK), b""):
                size += len(chunk)
                digest.update(chunk)
            actual[path] = (size, digest.hexdigest())
    except (tarfile.TarError, EOFError, OSError, UnicodeDecodeError) as error:
        raise BackupError(f"The backup archive is damaged: {error}") from error
    if manifest_raw is None:
        raise BackupError("The backup has no manifest.")
    try:
        manifest = validate_manifest(json.loads(manifest_raw.decode("utf-8")), expected_id=expected_id)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BackupError("The backup manifest is unreadable.") from error
    listed = {item["path"]: (item["size"], item["sha256"]) for item in manifest["files"]}
    if listed != actual:
        raise BackupError("The backup contents do not match its manifest.", 409)
    return manifest


def write_sidecar(path: Path, manifest: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


def read_sidecar(path: Path) -> Optional[dict]:
    try:
        return validate_manifest(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def summary(manifest: dict) -> dict:
    """Listing fields of a manifest (everything except the per-file list)."""
    return {key: value for key, value in manifest.items() if key not in ("files", "skipped", "format", "version")}
