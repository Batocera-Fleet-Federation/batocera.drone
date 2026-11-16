"""Preview and apply a backup onto this machine (stdlib only).

Used both in-process and by the privileged helper (``app/apply_backup.py``), so this
module imports nothing outside the ``backups`` package.

Restore rules:

* Only the selected categories are touched.
* **Overlay** writes the backup's files and leaves everything else alone. **Exact**
  also deletes files *inside the selected categories* that the backup does not have;
  the category collector decides what is deletable, so caches, firmware, links and
  files of unselected categories are never removed.
* ``batocera.conf`` is merged key by key (``confmerge.merge_batocera_conf``); files with
  removed secrets get this machine's own values back (``confmerge.restore_redacted``).
* Every selected file is first streamed into a private staging folder and checked
  against the manifest's size and SHA-256; nothing is installed unless all of them
  verify.
* Destinations are reached through directory descriptors opened with ``O_NOFOLLOW``
  below the configured root; an existing symlink or non-regular file at a
  destination is never replaced. Files are written to a temporary name and renamed
  into place.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Set

try:
    from . import archive as _archive
    from . import categories as _categories
    from . import confmerge as _confmerge
except ImportError:  # pragma: no cover - flat execution
    from backups import archive as _archive  # type: ignore
    from backups import categories as _categories  # type: ignore
    from backups import confmerge as _confmerge  # type: ignore

MODES = ("overlay", "exact")
BATOCERA_CONF = "system/batocera.conf"
MAX_LISTED_REMOVALS = 200
_CHUNK = 1024 * 1024
_STAGING_PREFIX = ".drone-backup-"


class RestoreError(_archive.BackupError):
    pass


def check_selection(manifest: dict, selected: Iterable[str], mode: str) -> List[str]:
    if mode not in MODES:
        raise RestoreError("Restore mode must be overlay or exact.")
    available = {item["id"] for item in manifest.get("categories") or []}
    chosen = []
    for value in selected or []:
        if not _categories.valid_category(value) or value not in available:
            raise RestoreError(f"This backup has no category {value!r}.")
        if value not in chosen:
            chosen.append(value)
    if not chosen:
        raise RestoreError("Select at least one category to restore.")
    return chosen


MAX_PATH_FILTERS = 5000


def check_paths(manifest: dict, paths: Any) -> Optional[List[str]]:
    """Normalize an optional file/folder filter. ``None`` means "whole categories".

    Each entry is a backup path (``saves/snes/Mario.srm``) or a folder prefix
    (``system/configs/dolphin-emu``); a file is restored when it equals an entry or
    sits under one. Every entry must match at least one file in the backup.
    """
    if paths is None:
        return None
    if not isinstance(paths, list) or not paths:
        raise RestoreError("Select at least one file or folder to restore.")
    if len(paths) > MAX_PATH_FILTERS:
        raise RestoreError(f"Select at most {MAX_PATH_FILTERS} files or folders; choose a whole category instead.")
    files = [item["path"] for item in manifest["files"]]
    clean: List[str] = []
    for value in paths:
        value = _archive.safe_relative_path(value.rstrip("/") if isinstance(value, str) else value)
        if not any(path == value or path.startswith(value + "/") for path in files):
            raise RestoreError(f"This backup has nothing at {value!r}.")
        if value not in clean:
            clean.append(value)
    return clean


class Selection:
    """Which backup files a restore covers.

    ``categories`` are restored whole, except the ones a ``paths`` entry falls in
    (``narrowed``): there only files equal to or under one of the paths are restored,
    and Exact mode only removes local files under those paths.
    """

    def __init__(self, categories: List[str], paths: Optional[List[str]], narrowed: Set[str]):
        self.categories = categories
        self.paths = paths
        self.narrowed = narrowed

    def includes(self, path: str, category: Optional[str]) -> bool:
        if category not in self.categories:
            return False
        if path.rsplit("/", 1)[-1].lower() in _categories.PLACEHOLDER_NAMES:
            return False  # folder markers in older backups: never recreate empty folders
        if category not in self.narrowed:
            return True
        return any(path == value or path.startswith(value + "/") for value in self.paths or [])


def resolve_selection(manifest: dict, selected: Iterable[str], mode: str, paths: Any = None) -> Selection:
    """Validate categories + optional file/folder filter. With a filter and no
    categories, the categories are the ones the chosen files belong to."""
    clean_paths = check_paths(manifest, paths)
    selected = list(selected or [])
    narrowed: Set[str] = set()
    for item in manifest["files"]:
        if clean_paths and any(item["path"] == v or item["path"].startswith(v + "/") for v in clean_paths):
            narrowed.add(item["category"])
            if item["category"] not in selected:
                selected.append(item["category"])
    chosen = check_selection(manifest, selected, mode)
    return Selection(chosen, clean_paths, narrowed)


def with_extra_categories(manifest: dict, extra: Iterable[str]) -> dict:
    """Allow restoring categories with no files (a snapshot of categories that were empty)."""
    present = {item["id"] for item in manifest["categories"]}
    missing = [value for value in extra or [] if _categories.valid_category(value) and value not in present]
    if not missing:
        return manifest
    return dict(manifest, categories=list(manifest["categories"]) + [
        dict(_categories.describe(value), file_count=0, size_bytes=0) for value in missing])


def _split(arcpath: str):
    parts = PurePosixPath(arcpath).parts
    return parts[0], parts[1:]


def _root_dir(roots: "_categories.Roots", root: str, create: bool) -> Path:
    base = roots.path(root)
    if create:
        base.mkdir(parents=True, exist_ok=True)
    return Path(os.path.realpath(base))


@contextmanager
def _open_dir(root: Path, parts: Iterable[str], create: bool):
    fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            if part in ("", ".", ".."):
                raise RestoreError("Invalid path in backup.")
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def _read_local(roots: "_categories.Roots", arcpath: str, limit: Optional[int] = None) -> Optional[bytes]:
    """Bytes of a local regular file, ``None`` when absent (never through a link)."""
    root, rel = _split(arcpath)
    try:
        with _open_dir(_root_dir(roots, root, False), rel[:-1], False) as fd:
            handle = os.open(rel[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(handle, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return None
                return stream.read(limit) if limit else stream.read()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return None


def _local_digest(roots: "_categories.Roots", arcpath: str, size: int) -> Optional[str]:
    """SHA-256 of a local file when it exists with ``size`` bytes (cheap size gate first)."""
    root, rel = _split(arcpath)
    try:
        with _open_dir(_root_dir(roots, root, False), rel[:-1], False) as fd:
            info = os.stat(rel[-1], dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                return "not-a-file"
            if info.st_size != size:
                return "different"
            handle = os.open(rel[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            digest = hashlib.sha256()
            with os.fdopen(handle, "rb") as stream:
                for chunk in iter(lambda: stream.read(_CHUNK), b""):
                    digest.update(chunk)
            return digest.hexdigest()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return "unreadable"


def read_members(archive_path: Path, paths: Set[str]) -> Dict[str, bytes]:
    """Small members (merged config files) read straight from the archive."""
    found: Dict[str, bytes] = {}
    if not paths:
        return found
    for member, handle in _archive.iter_members(archive_path):
        name = member.name[len(_archive.DATA_PREFIX):] if member.name.startswith(_archive.DATA_PREFIX) else None
        if name in paths and handle is not None and member.size <= _categories.MAX_CONFIG_FILE_BYTES:
            found[name] = handle.read()
            if len(found) == len(paths):
                break
    return found


def _final_content(entry: dict, backup_bytes: bytes, local: Optional[bytes], mode: str,
                   include_machine_keys: bool) -> tuple:
    """(bytes to write, batocera.conf changes or None) for merged files."""
    if entry["path"] == BATOCERA_CONF:
        text, changes = _confmerge.merge_batocera_conf(
            _confmerge.decode(backup_bytes), _confmerge.decode(local) if local is not None else None,
            exact=mode == "exact", include_machine_keys=include_machine_keys)
        return _confmerge.encode(text), changes
    text = _confmerge.restore_redacted(_confmerge.decode(backup_bytes),
                                       _confmerge.decode(local) if local is not None else None,
                                       entry.get("redacted") or [])
    return _confmerge.encode(text), None


def _merged(entry: dict) -> bool:
    return entry["path"] == BATOCERA_CONF or bool(entry.get("redacted"))


def _removals(roots: "_categories.Roots", manifest: dict, selection: Selection) -> List[str]:
    present = {item["path"] for item in manifest["files"]}
    local = _categories.collect(roots, selection.categories)
    return [item.arcpath for item in local.files
            if item.arcpath not in present and selection.includes(item.arcpath, item.category)]


def plan(roots: "_categories.Roots", archive_path: Path, manifest: dict, selected: Iterable[str], mode: str,
         include_machine_keys: bool = False, paths: Any = None) -> dict:
    """What a restore would change, per category, without writing anything."""
    selection = resolve_selection(manifest, selected, mode, paths)
    chosen, paths = selection.categories, selection.paths
    entries = [item for item in manifest["files"] if selection.includes(item["path"], item["category"])]
    merged_paths = {item["path"] for item in entries if _merged(item)}
    backup_members = read_members(archive_path, merged_paths)
    rows = {category: {"new": 0, "changed": 0, "identical": 0, "remove": 0, "blocked": 0} for category in chosen}
    conf_changes = None
    for item in entries:
        bucket = rows[item["category"]]
        if _merged(item):
            if item["path"] not in backup_members:
                raise RestoreError("The backup archive is missing a file listed in its manifest.")
            local = _read_local(roots, item["path"])
            content, changes = _final_content(item, backup_members[item["path"]], local, mode, include_machine_keys)
            if changes is not None:
                conf_changes = changes
            bucket["new" if local is None else "identical" if local == content else "changed"] += 1
            continue
        digest = _local_digest(roots, item["path"], item["size"])
        if digest is None:
            bucket["new"] += 1
        elif digest in ("not-a-file", "unreadable"):
            bucket["blocked"] += 1
        elif digest == item["sha256"]:
            bucket["identical"] += 1
        else:
            bucket["changed"] += 1
    removals: List[str] = _removals(roots, manifest, selection) if mode == "exact" else []
    for path in removals:
        rows[_categories.classify(path)]["remove"] += 1
    return {
        "mode": mode,
        "include_machine_keys": bool(include_machine_keys),
        "categories": [dict(_categories.describe(category), **rows[category]) for category in chosen],
        "totals": {key: sum(row[key] for row in rows.values()) for key in ("new", "changed", "identical", "remove", "blocked")},
        "batocera_conf": conf_changes,
        "removals": removals[:MAX_LISTED_REMOVALS],
        "removal_count": len(removals),
        "paths": paths,
        "file_count": len(entries),
    }


def _write_file(roots: "_categories.Roots", arcpath: str, source, mode_bits: int) -> None:
    root, rel = _split(arcpath)
    with _open_dir(_root_dir(roots, root, True), rel[:-1], True) as fd:
        name = rel[-1]
        try:
            existing = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise RestoreError("refusing to replace a link, folder or special file")
        if existing is not None:
            mode_bits = stat.S_IMODE(existing.st_mode)
        temporary = f"{_STAGING_PREFIX}{secrets.token_hex(6)}"
        out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            with os.fdopen(out, "wb") as handle:
                if isinstance(source, bytes):
                    handle.write(source)
                else:
                    with open(source, "rb") as stream:
                        shutil.copyfileobj(stream, handle, _CHUNK)
                handle.flush()
                os.fchmod(handle.fileno(), mode_bits)
                os.fsync(handle.fileno())
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=fd)
            except OSError:
                pass
            raise


def _remove_file(roots: "_categories.Roots", arcpath: str) -> None:
    root, rel = _split(arcpath)
    with _open_dir(_root_dir(roots, root, False), rel[:-1], False) as fd:
        info = os.stat(rel[-1], dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise RestoreError("not a regular file")
        os.unlink(rel[-1], dir_fd=fd)


def _stage(archive_path: Path, wanted: Dict[str, dict], staging: Path) -> Dict[str, Path]:
    """Copy and verify every wanted member into ``staging``; all or nothing."""
    staged: Dict[str, Path] = {}
    for member, handle in _archive.iter_members(archive_path):
        if handle is None or not member.name.startswith(_archive.DATA_PREFIX):
            continue
        path = member.name[len(_archive.DATA_PREFIX):]
        entry = wanted.get(path)
        if entry is None:
            continue
        target = staging / f"{len(staged):07d}"
        digest, size = hashlib.sha256(), 0
        with open(target, "xb") as out:
            for chunk in iter(lambda: handle.read(_CHUNK), b""):
                size += len(chunk)
                if size > entry["size"]:
                    raise RestoreError(f"{path} is larger than its manifest says.", 409)
                digest.update(chunk)
                out.write(chunk)
        if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
            raise RestoreError(f"{path} does not match its manifest.", 409)
        staged[path] = target
    missing = set(wanted) - set(staged)
    if missing:
        raise RestoreError(f"The backup archive is missing {len(missing)} file(s) listed in its manifest.", 409)
    return staged


def apply(roots: "_categories.Roots", archive_path: Path, manifest: dict, selected: Iterable[str], mode: str,
          include_machine_keys: bool, staging_parent: Path, paths: Any = None) -> dict:
    """Restore the selected categories (optionally only some files). Caller stops ES around this."""
    selection = resolve_selection(manifest, selected, mode, paths)
    chosen, paths = selection.categories, selection.paths
    wanted = {item["path"]: item for item in manifest["files"] if selection.includes(item["path"], item["category"])}
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=str(staging_parent)))
    written = unchanged = removed = 0
    failed: List[dict] = []
    try:
        staged = _stage(archive_path, wanted, staging)
        for path, entry in sorted(wanted.items()):
            try:
                if _merged(entry):
                    local = _read_local(roots, path)
                    content, _changes = _final_content(entry, staged[path].read_bytes(), local, mode, include_machine_keys)
                    if local == content:
                        unchanged += 1
                        continue
                    _write_file(roots, path, content, 0o644)
                else:
                    if _local_digest(roots, path, entry["size"]) == entry["sha256"]:
                        unchanged += 1
                        continue
                    executable = entry["category"] == "scripts"
                    _write_file(roots, path, staged[path], 0o755 if executable else 0o644)
                written += 1
            except (OSError, RestoreError) as error:
                failed.append({"path": path, "error": str(getattr(error, "strerror", None) or error)})
        if mode == "exact":
            for path in _removals(roots, manifest, selection):
                try:
                    _remove_file(roots, path)
                    removed += 1
                except (OSError, RestoreError) as error:
                    failed.append({"path": path, "error": f"could not remove: {getattr(error, 'strerror', None) or error}"})
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"written": written, "unchanged": unchanged, "removed": removed,
            "failed": failed[:MAX_LISTED_REMOVALS], "failed_count": len(failed), "categories": chosen, "mode": mode,
            "paths": paths}
