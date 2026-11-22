"""Backups: creation jobs, restore jobs with pre-restore snapshots, and the swarm side.

* Archives and their manifest sidecars live in ``backups_directory()`` named by
  ``backup_id``; ``storage/backup_store.py`` holds the listing metadata.
* One user backup builds at a time (background thread). A restore first builds an
  automatic **snapshot** of the categories it is about to change, synchronously, so a
  restore can always be rolled back; the newest ``SNAPSHOT_KEEP`` snapshots are kept.
* Restores run as root: in-process when this Drone already is root, otherwise via the
  privileged service worker (``apply-backup.request``/``.result`` in the control dir,
  ``app/apply_backup.py``). EmulationStation and any running game are stopped for the
  copy and EmulationStation is restarted afterwards.
* Peers list each other's backups over the pinned mTLS channel
  (``/peer/backups``, ``/peer/backups/<id>``) and pull archives through the normal
  DownloadManager queue (``/peer/backups/<id>/archive``). A downloaded archive is
  installed only after ``archive.verify_archive`` proves it matches the listing's
  SHA-256 and its own manifest. Snapshots are never offered to peers, and backups are
  only ever applied to the Drone the user is operating -- no peer can trigger a
  restore elsewhere.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

try:
    from . import archive as _archive
    from . import assets as _assets
    from . import categories as _categories
    from . import restore as _restore
    from ..common.batocera_version import _read_batocera_version
    from ..common.runtime_state import _ES_LIFECYCLE_LOCK
    from ..storage import backup_store as _store
except ImportError:  # pragma: no cover - flat execution
    from backups import archive as _archive  # type: ignore
    from backups import assets as _assets  # type: ignore
    from backups import categories as _categories  # type: ignore
    from backups import restore as _restore  # type: ignore
    from common.batocera_version import _read_batocera_version  # type: ignore
    from common.runtime_state import _ES_LIFECYCLE_LOCK  # type: ignore
    from storage import backup_store as _store  # type: ignore

BackupError = _archive.BackupError

SNAPSHOT_KEEP = 5
PEER_LIST_TIMEOUT = 15
PEER_MANIFEST_TIMEOUT = 30
PEER_LIST_MAX_BYTES = 4 * 1024 * 1024
PEER_MANIFEST_MAX_BYTES = 48 * 1024 * 1024
RESTORE_TIMEOUT_SECONDS = 1800
_BOARD_FILES = (Path("/boot/boot/batocera.board"), Path("/usr/share/batocera/batocera.board"))
_DRONE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

_LOCK = threading.RLock()
_ACTIVE_BUILDS: Dict[str, str] = {}
_RESTORE: Dict[str, Any] = {"state": "idle"}


# ------------------------------------------------------------------ paths & facts

def backups_directory(settings: Any) -> Path:
    directory = Path(settings.userdata_root) / "system" / "drone-app" / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def archive_path(settings: Any, backup_id: str) -> Path:
    return backups_directory(settings) / f"{backup_id}.tar.gz"


def manifest_path(settings: Any, backup_id: str) -> Path:
    return backups_directory(settings) / f"{backup_id}.manifest.json"


def assets_path(settings: Any, backup_id: str) -> Path:
    """Local copy of the backup's ``assets.json.gz`` member (planning never reads the archive)."""
    return backups_directory(settings) / f"{backup_id}.assets.json.gz"


def valid_backup_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_archive.BACKUP_ID.match(value))


def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return ""


def _board() -> str:
    for candidate in _BOARD_FILES:
        try:
            text = candidate.read_text(encoding="utf-8", errors="ignore").strip().splitlines()
            if text and text[0].strip():
                return text[0].strip()[:100]
        except OSError:
            continue
    return ""


def source_info(settings: Any) -> dict:
    hostname = _hostname()
    return {
        "drone_id": str(settings.device_id or ""),
        "drone_name": hostname or str(settings.device_id or ""),
        "hostname": hostname,
        "batocera_version": _read_batocera_version(Path(settings.userdata_root)) or "",
        "architecture": os.uname().machine if hasattr(os, "uname") else "",
        "board": _board(),
    }


def _major(version: str) -> str:
    match = re.match(r"\s*(\d+)", str(version or ""))
    return match.group(1) if match else ""


def compatibility(settings: Any, source: dict) -> dict:
    """How a backup's origin compares with this machine; warnings require extra confirmation."""
    local = source_info(settings)
    source = source if isinstance(source, dict) else {}
    warnings: List[str] = []
    src_version, local_version = str(source.get("batocera_version") or ""), local["batocera_version"]
    if _major(src_version) and _major(local_version) and _major(src_version) != _major(local_version):
        warnings.append(f"Made on Batocera {src_version}; this Drone runs Batocera {local_version}. "
                        "Settings may not carry over and save states are the most likely to break.")
    src_arch, local_arch = str(source.get("architecture") or ""), local["architecture"]
    if src_arch and local_arch and src_arch != local_arch:
        warnings.append(f"Made on a {src_arch} machine; this Drone is {local_arch}. "
                        "Emulator choices and save states can differ between architectures.")
    src_board, local_board = str(source.get("board") or ""), local["board"]
    if src_board and local_board and src_board != local_board and src_arch == local_arch:
        warnings.append(f"Made on a {src_board} board; this Drone is a {local_board}.")
    return {
        "same_drone": bool(source.get("drone_id")) and source.get("drone_id") == local["drone_id"],
        "local": local,
        "warnings": warnings,
        "compatible": not warnings,
    }


# ----------------------------------------------------------------------- listing

def _reconcile(settings: Any) -> None:
    """Rows left 'creating' by an earlier process can never finish."""
    with _LOCK:
        for backup_id in _store.creating_ids(settings):
            if backup_id not in _ACTIVE_BUILDS:
                _store.mark_error(settings, backup_id, "Interrupted: the Drone restarted while this backup was being built.")
                _remove_files(settings, backup_id)


def _remove_files(settings: Any, backup_id: str) -> None:
    for path in (archive_path(settings, backup_id), manifest_path(settings, backup_id), assets_path(settings, backup_id)):
        try:
            path.unlink()
        except OSError:
            pass


def _decorate(settings: Any, row: dict) -> dict:
    row = dict(row)
    row["compatibility"] = compatibility(settings, row.get("source") or {}) if row.get("status") == "complete" else None
    return row


def list_local(settings: Any) -> dict:
    _reconcile(settings)
    rows = [_decorate(settings, row) for row in _store.list_rows(settings)]
    return {
        "backups": [row for row in rows if row["kind"] == "backup"],
        "snapshots": [row for row in rows if row["kind"] == "snapshot"],
        "restore": restore_status(),
    }


def get_local(settings: Any, backup_id: str, *, complete: bool = True) -> dict:
    if not valid_backup_id(backup_id):
        raise BackupError("Backup not found.", 404)
    row = _store.get(settings, backup_id)
    if row is None or (complete and row.get("status") != _store.STATUS_COMPLETE):
        raise BackupError("Backup not found.", 404)
    return row


def local_manifest(settings: Any, backup_id: str) -> dict:
    row = get_local(settings, backup_id)
    manifest = _archive.read_sidecar(manifest_path(settings, backup_id))
    if manifest is None or not archive_path(settings, backup_id).is_file():
        raise BackupError("This backup's files are missing from disk.", 410)
    manifest = dict(manifest, name=row.get("name") or manifest.get("name"),
                    description=row.get("description") or manifest.get("description"))
    return manifest


def detail(settings: Any, backup_id: str) -> dict:
    row = _decorate(settings, get_local(settings, backup_id))
    manifest = local_manifest(settings, backup_id)
    return {"backup": row, "files": manifest["files"], "skipped": manifest["skipped"],
            "assets": assets_overview(manifest)}


def assets_overview(manifest: dict) -> dict:
    """What a backup records about games and media, for the detail view and the wizards."""
    recorded = manifest.get("assets")
    if recorded:
        return {"recorded": True, "legacy": False, "counts": recorded["counts"], "bytes": recorded["bytes"],
                "truncated": recorded["truncated"], "media_roms": recorded.get("media_roms", 0)}
    has_gamelists = any(item["category"] == "gamelists" for item in manifest.get("files") or [])
    return {"recorded": False, "legacy": has_gamelists, "counts": {kind: 0 for kind in _assets.KINDS},
            "bytes": {kind: 0 for kind in _assets.KINDS}, "truncated": False, "media_roms": 0}


def load_assets(settings: Any, backup_id: str) -> dict:
    """The asset wish list of a local backup: the recorded list, else a path-only ROM list read
    from its archived gamelists (``legacy`` is True). Never raises for a damaged record."""
    manifest = local_manifest(settings, backup_id)
    recorded = manifest.get("assets")
    if recorded:
        blob = None
        try:
            blob = assets_path(settings, backup_id).read_bytes()
        except OSError:
            pass
        if blob is None or _sha256(blob) != recorded["sha256"]:
            blob = _read_archive_assets(archive_path(settings, backup_id), recorded["sha256"])
        if blob is not None:
            try:
                document = _assets.decode(blob)
                document["legacy"] = False
                return document
            except ValueError:
                pass
    try:
        document = _assets.legacy_from_gamelists(_archive.iter_members(archive_path(settings, backup_id)))
    except (OSError, ValueError, EOFError, tarfile.TarError):
        document = _assets.sanitize({"version": _assets.ASSETS_VERSION})
        document["legacy"] = True
    return document


def _sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _read_archive_assets(path: Path, expected_sha256: str) -> Optional[bytes]:
    try:
        for member, handle in _archive.iter_members(path):
            if member.name == _archive.ASSETS_MEMBER and handle is not None and member.size <= _assets.MAX_MEMBER_BYTES:
                blob = handle.read()
                return blob if _sha256(blob) == expected_sha256 else None
    except (OSError, EOFError, tarfile.TarError):
        return None
    return None


def available_categories(settings: Any) -> dict:
    collection = _categories.collect(_categories.Roots.from_settings(settings))
    return {"categories": _categories.summarize(collection.files),
            "skipped_count": len(collection.skipped), "skipped_bytes": collection.skipped_bytes}


# ---------------------------------------------------------------------- creation

def _clean_categories(values: Any) -> Optional[List[str]]:
    if values is None:
        return None
    if not isinstance(values, list) or not values:
        raise BackupError("Select at least one category.")
    chosen = []
    for value in values:
        if not _categories.valid_category(value):
            raise BackupError(f"Unknown category {value!r}.")
        if value not in chosen:
            chosen.append(value)
    return chosen


def _build(settings: Any, backup_id: str, *, kind: str, name: str, description: str,
           categories: Optional[List[str]], snapshot_of: Optional[dict], include_saves: bool = True) -> dict:
    try:
        package = None
        if kind == "backup":
            # Every user backup (even settings-only) records which games and media this Drone has.
            # Snapshots are throw-away rollback points and never carry the list. A cache that
            # cannot be read must not stop a backup of the settings.
            try:
                package = _assets.package(_assets.collect(settings))
            except Exception:  # noqa: BLE001
                package = None
        manifest = _archive.build_archive(
            _categories.Roots.from_settings(settings), archive_path(settings, backup_id),
            backup_id=backup_id, kind=kind, name=name, description=description, categories=categories,
            source=source_info(settings), snapshot_of=snapshot_of, include_saves=include_saves,
            assets_package=package)
        _archive.write_sidecar(manifest_path(settings, backup_id), manifest)
        if package is not None:
            _write_assets_sidecar(assets_path(settings, backup_id), package[0])
        path = archive_path(settings, backup_id)
        _store.mark_complete(settings, backup_id, manifest, size_bytes=path.stat().st_size,
                             archive_sha256=_archive.file_sha256(path))
        return manifest
    except Exception as error:  # noqa: BLE001 - a row must never stay "creating"
        _store.mark_error(settings, backup_id, str(error) or error.__class__.__name__)
        _remove_files(settings, backup_id)
        raise
    finally:
        with _LOCK:
            _ACTIVE_BUILDS.pop(backup_id, None)


def _write_assets_sidecar(path: Path, blob: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(blob)
    os.replace(temporary, path)


def create_backup(settings: Any, *, name: str = "", description: str = "", categories: Any = None,
                  kind: str = "backup", snapshot_of: Optional[dict] = None, wait: bool = False) -> dict:
    chosen = _clean_categories(categories)
    name = " ".join(str(name or "").split())[:120]
    description = str(description or "").strip()[:1000]
    if not name:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        name = f"Before restoring {snapshot_of.get('name') or 'a backup'}" if snapshot_of else f"Backup {stamp}"
    _reconcile(settings)
    backup_id = secrets.token_hex(16)
    with _LOCK:
        if kind == "backup" and "backup" in _ACTIVE_BUILDS.values():
            raise BackupError("A backup is already being created.", 409)
        _ACTIVE_BUILDS[backup_id] = kind
    try:
        row = _store.create_pending(settings, backup_id, kind=kind, name=name, description=description,
                                    snapshot_of=snapshot_of)
    except Exception:
        with _LOCK:
            _ACTIVE_BUILDS.pop(backup_id, None)
        raise
    # Without an explicit choice a backup holds settings only: saves and save states are
    # large and move per game through each ROM's Saves & Saved States card instead.
    options = dict(kind=kind, name=name, description=description, categories=chosen, snapshot_of=snapshot_of,
                   include_saves=chosen is not None or kind != "backup")
    if wait:
        _build(settings, backup_id, **options)
        return _store.get(settings, backup_id) or row
    thread = threading.Thread(target=_build_quietly, args=(settings, backup_id), kwargs=options,
                              name="drone-backup-build", daemon=True)
    thread.start()
    return row


def _build_quietly(settings: Any, backup_id: str, **options) -> None:
    try:
        _build(settings, backup_id, **options)
    except Exception:  # noqa: BLE001 - already recorded on the row
        pass


def rename(settings: Any, backup_id: str, *, name: Any, description: Any) -> dict:
    row = get_local(settings, backup_id)
    clean_name = " ".join(str(name if name is not None else row["name"]).split())[:120] or row["name"]
    clean_description = str(description if description is not None else row["description"]).strip()[:1000]
    return _decorate(settings, _store.update_text(settings, backup_id, name=clean_name, description=clean_description))


def delete(settings: Any, backup_id: str) -> dict:
    row = get_local(settings, backup_id, complete=False)
    with _LOCK:
        if backup_id in _ACTIVE_BUILDS:
            raise BackupError("This backup is still being created.", 409)
        if _RESTORE.get("state") == "running" and _RESTORE.get("backup_id") == backup_id:
            raise BackupError("This backup is being restored.", 409)
    _store.delete(settings, backup_id)
    _remove_files(settings, row["backup_id"])
    return {"status": "deleted"}


def _prune_snapshots(settings: Any) -> None:
    snapshots = [row for row in _store.list_rows(settings, kind="snapshot") if row["status"] == _store.STATUS_COMPLETE]
    for row in snapshots[SNAPSHOT_KEEP:]:
        _store.delete(settings, row["backup_id"])
        _remove_files(settings, row["backup_id"])


# ----------------------------------------------------------------------- restore

def _snapshot_categories(row: dict) -> List[str]:
    snapshot_of = row.get("snapshot_of") if row.get("kind") == "snapshot" else None
    return list((snapshot_of or {}).get("categories") or [])


def _selectable(manifest: dict, row: dict) -> dict:
    """A snapshot may roll back categories that had no files before the restore."""
    return _restore.with_extra_categories(manifest, _snapshot_categories(row))


def _warnings(settings: Any, row: dict, chosen: List[str]) -> List[str]:
    compat = compatibility(settings, row.get("source") or {})
    warnings = list(compat["warnings"])
    if "scripts" in chosen and not compat["same_drone"]:
        warnings.append("Custom scripts from another Drone run on this machine at startup. Restore them only if you trust that Drone.")
    return warnings


def plan(settings: Any, backup_id: str, *, categories: Any, mode: str, include_machine_keys: bool = False,
         paths: Any = None) -> dict:
    row = get_local(settings, backup_id)
    manifest = _selectable(local_manifest(settings, backup_id), row)
    result = _restore.plan(_categories.Roots.from_settings(settings), archive_path(settings, backup_id), manifest,
                           categories or [], mode, bool(include_machine_keys), paths)
    result["warnings"] = _warnings(settings, row, [item["id"] for item in result["categories"]])
    result["backup"] = _decorate(settings, row)
    return result


def restore_status() -> dict:
    with _LOCK:
        return {key: value for key, value in _RESTORE.items() if not key.startswith("_")}


def _set_restore(**values) -> None:
    with _LOCK:
        _RESTORE.update(values)


def start_restore(settings: Any, backup_id: str, *, categories: Any, mode: str, include_machine_keys: bool = False,
                  acknowledge_warnings: bool = False, runner: Optional[Callable] = None, paths: Any = None,
                  assets: Any = None, manager: Any = None) -> dict:
    """Validate, then snapshot + restore on a background thread; poll ``restore_status``.

    ``assets`` (``{plan_id, selected, deselected, deselected_groups}`` from a plan made with
    ``plan_assets``) asks for the confirmed games and media to be queued **after** the
    configuration restore succeeds; a failed restore queues nothing. Progress is
    ``restore_status()["assets"]``."""
    row = get_local(settings, backup_id)
    asset_job = None
    if assets is not None:
        asset_job = _prepare_asset_restore(backup_id, assets, manager)
    manifest = _selectable(local_manifest(settings, backup_id), row)
    selection = _restore.resolve_selection(manifest, categories or [], mode, paths)
    chosen, paths = selection.categories, selection.paths
    warnings = _warnings(settings, row, chosen)
    if warnings and not acknowledge_warnings:
        raise BackupError("Confirm the compatibility warnings before restoring this backup.", 409,
                          {"warnings": warnings, "needs_acknowledgement": True})
    with _LOCK:
        if _RESTORE.get("state") == "running":
            raise BackupError("A restore is already running.", 409)
        if "backup" in _ACTIVE_BUILDS.values():
            raise BackupError("Wait for the backup that is being created to finish.", 409)
        _RESTORE.clear()
        _RESTORE.update(state="running", stage="snapshot", backup_id=backup_id, backup_name=row["name"],
                        categories=chosen, paths=paths, mode=mode, started_at=_archive.now_iso(), finished_at=None,
                        result=None, error=None, snapshot_id=None,
                        assets={"state": "pending"} if asset_job else None)
    thread = threading.Thread(
        target=_restore_job, name="drone-backup-restore", daemon=True,
        args=(settings, row, chosen, mode, bool(include_machine_keys), runner or _run_restore, paths, asset_job))
    thread.start()
    return restore_status()


def _restore_job(settings: Any, row: dict, chosen: List[str], mode: str, include_machine_keys: bool,
                 runner: Callable, paths: Optional[List[str]] = None, asset_job: Optional[tuple] = None) -> None:
    backup_id = row["backup_id"]
    try:
        snapshot = create_backup(
            settings, kind="snapshot", categories=chosen, wait=True,
            snapshot_of=dict({"backup_id": backup_id, "name": row["name"], "categories": chosen},
                             **({"paths": paths} if paths else {})))
        if snapshot.get("status") != _store.STATUS_COMPLETE:
            raise BackupError(snapshot.get("error_message") or "The safety snapshot could not be created.", 500)
        _set_restore(stage="restore", snapshot_id=snapshot["backup_id"])
        request = {
            "archive_path": str(archive_path(settings, backup_id)),
            "manifest": local_manifest(settings, backup_id),
            "extra_categories": _snapshot_categories(row),
            "categories": chosen,
            "paths": paths,
            "mode": mode,
            "include_machine_keys": include_machine_keys,
            "userdata_root": str(settings.userdata_root),
            "roms_root": str(settings.roms_root),
            "saves_root": str(settings.saves_root),
            "staging_dir": str(backups_directory(settings)),
        }
        result = runner(settings, request)
        _prune_snapshots(settings)
        _set_restore(state="complete", stage="done", finished_at=_archive.now_iso(), result=result)
        _notify(settings, row, result)
    except Exception as error:  # noqa: BLE001 - reported through restore_status
        _set_restore(state="error", stage="failed", finished_at=_archive.now_iso(), error=str(error) or error.__class__.__name__)
        if asset_job:
            _set_restore(assets={"state": "skipped", "error": "The restore failed, so no games or media were queued."})
        return
    if asset_job:
        # Only reached after the configuration restore (and the EmulationStation restart) succeeded.
        _set_restore(assets={"state": "queueing"})
        _queue_assets(settings, row, *asset_job)


# ------------------------------------------------------------------ asset restore

ASSET_KIND_CHOICES = ("roms", "bios", "movies", "music")


def _asset_restore():
    try:
        from . import asset_restore as _module
    except ImportError:  # pragma: no cover - flat execution
        from backups import asset_restore as _module  # type: ignore
    return _module


def _clean_asset_kinds(kinds: Any, include_bios: bool) -> List[str]:
    if kinds is None:
        chosen = ["roms", "movies", "music"]
    elif isinstance(kinds, list) and all(isinstance(value, str) for value in kinds):
        chosen = list(kinds)
    else:
        raise BackupError("Invalid asset kinds.")
    if include_bios and "bios" not in chosen:
        chosen.append("bios")
    for value in chosen:
        if value not in ASSET_KIND_CHOICES:
            raise BackupError(f"Unknown asset kind {value!r}.")
    return [value for value in ASSET_KIND_CHOICES if value in chosen]


def plan_assets(settings: Any, backup_id: str, *, kinds: Any = None, include_bios: bool = False,
                online_ids: Iterable[str] = (), manager: Any = None, fetch_page: Optional[Callable] = None) -> dict:
    """Search the online paired Drones for what a local backup recorded and this Drone lacks."""
    module = _asset_restore()
    row = get_local(settings, backup_id)
    chosen = _clean_asset_kinds(kinds, bool(include_bios))
    document = load_assets(settings, backup_id)
    hold = _peer_tools()[2]
    try:
        plan = module.build_plan(
            settings, backup=row, document=document, kinds=chosen,
            sources=module.online_sources(settings, online_ids, manager),
            fetch_page=fetch_page or module.peer_inventory_fetcher(settings),
            manager=manager, repository=getattr(manager, "repository", None))
    except hold as error:
        raise module.SwarmUnavailable() from error
    return module.public_plan(plan)


def fetch_assets(settings: Any, manager: Any, backup_id: str, selection: Any) -> dict:
    """Queue the confirmed part of a plan ("Get missing games"); no configuration is restored."""
    module = _asset_restore()
    row = get_local(settings, backup_id)
    plan = module.PLANS.get((selection or {}).get("plan_id") if isinstance(selection, dict) else None, backup_id)
    items = module.resolve_selection(plan, selection)
    if not items:
        raise BackupError("Nothing is selected.", 400)
    result = module.fetch_plan(settings, manager, plan, items, label=row["name"])
    _notify_assets(settings, row, result)
    return dict(result, job_ids=result["job_ids"][:500])


def _prepare_asset_restore(backup_id: str, assets: Any, manager: Any) -> tuple:
    module = _asset_restore()
    if manager is None:
        raise BackupError("The download queue is unavailable.", 503)
    if not isinstance(assets, dict):
        raise BackupError("Invalid asset selection.")
    plan = module.PLANS.get(assets.get("plan_id"), backup_id)
    items = module.resolve_selection(plan, assets)
    return (manager, plan, items)


def _queue_assets(settings: Any, row: dict, manager: Any, plan: dict, items: List[dict]) -> None:
    module = _asset_restore()
    try:
        result = module.fetch_plan(settings, manager, plan, items, label=row["name"])
        _notify_assets(settings, row, result)
        _set_restore(assets={key: value for key, value in result.items() if key != "job_ids"} | {"state": "queued"})
    except Exception as error:  # noqa: BLE001 - the configuration is already restored; report only
        _set_restore(assets={"state": "error", "error": str(error) or error.__class__.__name__})


def _notify_assets(settings: Any, row: dict, result: dict) -> None:
    """Unavailable items are reported, never persisted: a bell entry when nothing could be queued
    for them (the batch's own entries already carry the counts when downloads were queued)."""
    if result.get("queued") or not result.get("unavailable"):
        return
    try:
        try:
            from ..device import notifications as _notifications
        except ImportError:  # pragma: no cover - flat execution
            from device import notifications as _notifications  # type: ignore
        _notifications.record_event(
            settings, "asset_transfer_finished", "Restoring from a backup finished",
            f'Backup "{row.get("name")}": nothing was queued; {int(result["unavailable"])} item(s) are not '
            "available on any online Drone. Run Get missing games again when more Drones are online.",
            details={"label": row.get("name"), "unavailable": int(result["unavailable"]), "queued": 0})
    except Exception:  # noqa: BLE001 - a notification never fails a restore
        pass


def _notify(settings: Any, row: dict, result: dict) -> None:
    try:
        try:
            from ..device import notifications as _notifications
        except ImportError:  # pragma: no cover - flat execution
            from device import notifications as _notifications  # type: ignore
        title = "Backup restored on this machine" if row.get("kind") == "backup" else "Restore rolled back"
        _notifications.record_event(settings, "backup_restored", title,
                                    f"{row.get('name')}: {int(result.get('written') or 0)} file(s) written, "
                                    f"{int(result.get('removed') or 0)} removed.")
    except Exception:  # noqa: BLE001 - a notification never fails a restore
        pass


def _run_restore(settings: Any, request: dict) -> dict:
    try:
        from .. import apply_backup as _helper
    except ImportError:  # pragma: no cover - flat execution
        import apply_backup as _helper  # type: ignore
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        with _ES_LIFECYCLE_LOCK:
            return _helper.run(request, manage_emulationstation=True)
    if getattr(settings, "use_fake_data", False):
        return _helper.run(request, manage_emulationstation=False)
    return _request_privileged_restore(request)


def _request_privileged_restore(request: dict) -> dict:
    control_dir = Path(os.environ.get("DRONE_SERVICE_CONTROL_DIR", "/userdata/system/drone-app/control"))
    request_path = control_dir / "apply-backup.request"
    result_path = control_dir / "apply-backup.result"
    detail_path = control_dir / "apply-backup.detail.json"
    for stale in (result_path, detail_path):
        try:
            stale.unlink()
        except OSError:
            pass
    control_dir.mkdir(parents=True, exist_ok=True)
    request = dict(request, result_path=str(detail_path))
    request_path.write_text(json.dumps(request), encoding="utf-8")
    deadline = time.monotonic() + float(os.environ.get("DRONE_BACKUP_RESTORE_TIMEOUT_SECONDS", RESTORE_TIMEOUT_SECONDS))
    while time.monotonic() < deadline:
        if result_path.exists():
            outcome = result_path.read_text(encoding="utf-8", errors="ignore").strip()
            result_path.unlink(missing_ok=True)
            try:
                detail_data = json.loads(detail_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                detail_data = {}
            detail_path.unlink(missing_ok=True)
            if outcome != "ok":
                raise BackupError(detail_data.get("error") or outcome or "The privileged restore failed.", 500)
            return detail_data
        time.sleep(0.5)
    request_path.unlink(missing_ok=True)
    raise BackupError("Timed out waiting for the privileged restore; the Drone service worker may not be running.", 504)


# ------------------------------------------------------------------- peers serve

def peer_listing(settings: Any) -> dict:
    """What paired Drones may see: complete user backups, without per-file lists."""
    backups = []
    for row in _store.list_rows(settings, kind="backup", complete_only=True):
        if not archive_path(settings, row["backup_id"]).is_file():
            continue
        backups.append({key: row.get(key) for key in (
            "backup_id", "name", "description", "created_at", "size_bytes", "archive_sha256", "file_count",
            "content_bytes", "categories", "source", "skipped_count", "skipped_bytes")})
    return {"drone_id": str(settings.device_id or ""), "backups": backups}


def peer_manifest(settings: Any, backup_id: str) -> dict:
    row = get_local(settings, backup_id)
    if row.get("kind") != "backup":
        raise BackupError("Backup not found.", 404)
    return local_manifest(settings, backup_id)


def peer_archive(settings: Any, backup_id: str) -> Path:
    row = get_local(settings, backup_id)
    path = archive_path(settings, backup_id)
    if row.get("kind") != "backup" or not path.is_file():
        raise BackupError("Backup not found.", 404)
    return path


# -------------------------------------------------------------------- peers pull

def _peer_tools():
    try:
        from ..transfer import game_saves as _pinned
        from ..transfer import local_network as _local_network
        from ..transfer.peer_connectivity import PeerIdentityHold, _read_capped
    except ImportError:  # pragma: no cover - flat execution
        from transfer import game_saves as _pinned  # type: ignore
        from transfer import local_network as _local_network  # type: ignore
        from transfer.peer_connectivity import PeerIdentityHold, _read_capped  # type: ignore
    return _pinned, _local_network, PeerIdentityHold, _read_capped


def _peer_failure(error: Exception) -> BackupError:
    import http.client
    import ssl
    from urllib.error import HTTPError, URLError

    _pinned, _ln, PeerIdentityHold, _read_capped = _peer_tools()
    if isinstance(error, BackupError):
        return error
    if isinstance(error, _pinned.SaveError):
        return BackupError(str(error).replace("Save transfers", "Backup transfers"), error.status)
    if isinstance(error, PeerIdentityHold):
        return BackupError("This Drone's identity is being verified; swarm connections are paused.", 503)
    if isinstance(error, HTTPError):
        if error.code in (404, 405, 501):
            return BackupError("That Drone is online but too old to share backups. Update it to the latest Drone release.", 502)
        if error.code in (401, 403):
            return BackupError("That Drone refused the request. Check that the pairing is still approved on both Drones.", 502)
        return BackupError(f"That Drone could not list its backups (HTTP {error.code}).", 502)
    if isinstance(error, URLError) and isinstance(error.reason, BaseException):
        return _peer_failure(error.reason)
    if isinstance(error, (TimeoutError, socket.timeout)):
        return BackupError("That Drone took too long to answer.", 504)
    if isinstance(error, ssl.SSLError):
        return BackupError("The secure connection to that Drone failed. Its certificate may have changed; re-pair it in Swarm.", 502)
    if isinstance(error, (ValueError, http.client.HTTPException)):
        return BackupError("That Drone returned an unreadable backup list.", 502)
    return BackupError("Could not reach that Drone. It may be offline.", 502)


def _peer_json(settings: Any, peer_id: str, endpoint: str, timeout: int, limit: int):
    _pinned, _ln, _hold, _read_capped = _peer_tools()
    try:
        peer = _pinned.tls_peer(settings, peer_id)
        with _pinned._open_peer(settings, peer, endpoint, timeout=timeout) as response:
            return peer, json.loads(_read_capped(response, limit))
    except Exception as error:  # noqa: BLE001 - mapped to a specific message
        raise _peer_failure(error) from error


def _peer_entry(settings: Any, raw: Any, drone_id: str, drone_name: str) -> Optional[dict]:
    if not isinstance(raw, dict) or not valid_backup_id(raw.get("backup_id")):
        return None
    sha = raw.get("archive_sha256")
    size = raw.get("size_bytes")
    if not isinstance(sha, str) or not _archive.SHA256.match(sha) or type(size) is not int or size <= 0:
        return None
    clean_categories = []
    for item in raw.get("categories") if isinstance(raw.get("categories"), list) else []:
        if isinstance(item, dict) and _categories.valid_category(item.get("id")):
            clean_categories.append(dict(_categories.describe(item["id"]),
                                         file_count=int(item.get("file_count") or 0) if isinstance(item.get("file_count"), int) else 0,
                                         size_bytes=int(item.get("size_bytes") or 0) if isinstance(item.get("size_bytes"), int) else 0))
    source = _archive._clean_source(raw.get("source"))
    return {
        "backup_id": raw["backup_id"],
        "name": _archive._clean_text(raw.get("name"), 120) or "Backup",
        "description": _archive._clean_text(raw.get("description"), 1000),
        "created_at": _archive._clean_text(raw.get("created_at"), 40),
        "size_bytes": size,
        "archive_sha256": sha,
        "file_count": raw.get("file_count") if isinstance(raw.get("file_count"), int) else 0,
        "content_bytes": raw.get("content_bytes") if isinstance(raw.get("content_bytes"), int) else 0,
        "categories": clean_categories,
        "source": source,
        "drone_id": drone_id,
        "drone_name": drone_name,
        "is_local": False,
        "compatibility": compatibility(settings, source),
    }


def peer_backups(settings: Any, peer_id: str) -> List[dict]:
    peer, payload = _peer_json(settings, peer_id, "/v1/api/peer/backups", PEER_LIST_TIMEOUT, PEER_LIST_MAX_BYTES)
    if not isinstance(payload, dict) or not isinstance(payload.get("backups"), list):
        raise BackupError("That Drone returned an unreadable backup list.", 502)
    name = str(peer.get("name") or peer.get("hostname") or peer_id)
    entries = [_peer_entry(settings, raw, peer_id, name) for raw in payload["backups"][:2000]]
    local_ids = {row["backup_id"] for row in _store.list_rows(settings, complete_only=True)}
    result = []
    for entry in entries:
        if entry is not None:
            entry["available_locally"] = entry["backup_id"] in local_ids
            result.append(entry)
    return result


def peer_backup_detail(settings: Any, peer_id: str, backup_id: str) -> dict:
    if not valid_backup_id(backup_id):
        raise BackupError("Backup not found.", 404)
    _peer, payload = _peer_json(settings, peer_id, f"/v1/api/peer/backups/{backup_id}",
                                PEER_MANIFEST_TIMEOUT, PEER_MANIFEST_MAX_BYTES)
    manifest = _archive.validate_manifest(payload, expected_id=backup_id)
    return {"backup": dict(_archive.summary(manifest), compatibility=compatibility(settings, manifest["source"])),
            "files": manifest["files"], "skipped": manifest["skipped"], "assets": assets_overview(manifest)}


def local_entries(settings: Any) -> List[dict]:
    name = source_info(settings)["drone_name"]
    entries = []
    for row in _store.list_rows(settings, kind="backup", complete_only=True):
        entry = _decorate(settings, row)
        entry.update(drone_id=str(settings.device_id or ""), drone_name=name, is_local=True, available_locally=True)
        entries.append(entry)
    return entries


def swarm_listing(settings: Any, sources: List[dict], query: str = "") -> dict:
    """Backups on this Drone and on each requested online peer, fetched in parallel."""
    query = str(query or "").strip().lower()[:200]

    def matches(entry: dict) -> bool:
        if not query:
            return True
        haystack = " ".join([entry.get("name") or "", entry.get("description") or "", entry.get("drone_name") or "",
                             (entry.get("source") or {}).get("drone_name") or "",
                             (entry.get("source") or {}).get("batocera_version") or ""]
                            + [item.get("label") or "" for item in entry.get("categories") or []]).lower()
        return all(word in haystack for word in query.split())

    def load(source: dict) -> dict:
        try:
            entries = local_entries(settings) if source.get("is_local") else peer_backups(settings, source["drone_id"])
            return {"drone_id": source["drone_id"], "name": source.get("name"), "status": "ok",
                    "backups": [entry for entry in entries if matches(entry)]}
        except BackupError as error:
            return {"drone_id": source["drone_id"], "name": source.get("name"), "status": "error",
                    "error": str(error), "backups": []}

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(sources)))) as pool:
        results = list(pool.map(load, sources))
    return {"sources": results}


def enqueue_download(settings: Any, manager: Any, peer_id: str, backup_id: str) -> dict:
    if not valid_backup_id(backup_id) or not _DRONE_ID.match(str(peer_id or "")):
        raise BackupError("Backup not found.", 404)
    if manager is None:
        raise BackupError("The download queue is unavailable.", 503)
    existing = _store.get(settings, backup_id)
    if existing and existing.get("status") == _store.STATUS_COMPLETE and archive_path(settings, backup_id).is_file():
        return {"status": "exists", "backup": _decorate(settings, existing)}
    entry = next((item for item in peer_backups(settings, peer_id) if item["backup_id"] == backup_id), None)
    if entry is None:
        raise BackupError("That Drone no longer has this backup.", 404)
    _pinned, _local_network, _hold, _read = _peer_tools()
    peer = _local_network.get_paired_peer(settings, peer_id)
    job = manager.enqueue_backup({"network_mode": "local_network"}, peer, backup_id,
                                 expected_size=entry["size_bytes"], expected_sha256=entry["archive_sha256"],
                                 backup_name=entry["name"])
    return {"status": "queued", "job": job}


def install_download(settings: Any, partial: Path, *, backup_id: str, expected_sha256: str, peer: dict) -> dict:
    """Verify a fully downloaded archive and register it; the partial file is consumed."""
    try:
        carried: List[bytes] = []
        manifest = _archive.verify_archive(partial, expected_id=backup_id, expected_sha256=expected_sha256,
                                           assets_out=carried)
        if manifest["kind"] != "backup":
            raise BackupError("Snapshots cannot be shared between Drones.", 409)
        size = partial.stat().st_size
        os.replace(partial, archive_path(settings, backup_id))
        _archive.write_sidecar(manifest_path(settings, backup_id), manifest)
        if carried:
            _write_assets_sidecar(assets_path(settings, backup_id), carried[0])
    except BaseException:
        try:
            partial.unlink()
        except OSError:
            pass
        raise
    peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
    return _store.record_downloaded(settings, manifest, size_bytes=size, archive_sha256=expected_sha256,
                                    from_id=peer_id, from_name=str(peer.get("name") or peer.get("hostname") or peer_id))
