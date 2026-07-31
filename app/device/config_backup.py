"""Config-backup tarball creation for the "Backups" admin tile.

Bundles Batocera + emulator settings (not the Drone app's own credentials --
see the skill/PR discussion for why that's excluded) into one downloadable
``tar.gz``:

* ``system/batocera.conf``
* ``system/configs/**`` -- recursive, but any single file over
  ``BACKUP_MAX_CONFIG_FILE_BYTES`` is skipped. This directory is *not*
  reliably small: on a real device it held 254GB, almost all of it one
  emulator's installed game/firmware content (Switch ``.nca`` files) rather
  than settings. The size cap is what keeps this feature from trying to
  tar an entire game library -- it excludes exactly the handful of
  firmware/shader-cache/game-content outliers while keeping every genuine
  small settings file, without needing a hand-maintained per-emulator
  exclude list that would go stale as Batocera adds emulators.
* ``roms/<system>/gamelist.xml`` for every system folder
* ``system/services/*``, ``system/custom.sh*``, ``system/pro-custom.sh``,
  ``system/{custom,custom-scripts,scripts}/*`` -- small user-customization
  scripts
* ``saves/**`` -- recursive, all files (also size-capped, generously, as a
  pure defensive measure -- real saves total ~1GB on a real device)

Deliberately excluded: ``/userdata/bios`` and ROM files themselves (large,
copyrighted, not "settings"), ``.cache``/``cache``/shader-cache directories,
the Drone app's own state (credentials, VPN/SMTP passwords -- a separate,
security-sensitive category the user chose to keep out of a downloadable
file).

The tarball is built on a background thread (creation can take real time
once saves are included) and written to a temp path, then atomically
renamed into place -- a half-built tarball is never visible/downloadable.
Only one build runs at a time; the SQLite ``config_backups`` row (not an
in-process flag) is the single source of truth for that, so it works
correctly across process restarts and doesn't leak state between tests.
"""

from __future__ import annotations

import io
import os
import socket
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Tuple

try:
    from ..common.batocera_version import _read_batocera_version
    from ..device import smtp_manager as _smtp
    from ..storage import config_backup_store as _store
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.batocera_version import _read_batocera_version  # type: ignore
    from device import smtp_manager as _smtp  # type: ignore
    from storage import config_backup_store as _store  # type: ignore

BACKUP_MAX_CONFIG_FILE_BYTES = 20 * 1024 * 1024  # 20MB, see module docstring
BACKUP_MAX_SAVE_FILE_BYTES = 500 * 1024 * 1024  # defensive only; real saves are far smaller
# Most SMTP providers cap attachments around 20-25MB (Gmail is 25MB); a backup
# dominated by saves easily exceeds that, so this is checked up front with a
# clear error rather than letting the send fail obscurely partway through.
BACKUP_EMAIL_MAX_BYTES = 25 * 1024 * 1024

_CUSTOM_SCRIPT_DIRS = ("custom", "custom-scripts", "scripts")


def backups_directory(settings: Any) -> Path:
    directory = Path(settings.userdata_root) / "system" / "drone-app" / "config-backups"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _walk_files(root: Path):
    if not root.is_dir():
        return
    for current_root, _dirs, file_names in os.walk(root, followlinks=False):
        for name in file_names:
            yield Path(current_root) / name


def _add_file(path: Path, arcname: str, included: List[Tuple[Path, str, int]], skipped: List[dict], *, max_bytes) -> None:
    try:
        size = path.stat().st_size
    except OSError as error:
        skipped.append({"path": arcname, "size": 0, "reason": f"stat failed: {error}"})
        return
    if max_bytes is not None and size > max_bytes:
        skipped.append({"path": arcname, "size": size, "reason": f"exceeds {max_bytes // (1024 * 1024)}MB limit"})
        return
    included.append((path, arcname, size))


def collect_sources(settings: Any) -> Tuple[List[Tuple[Path, str, int]], List[dict]]:
    """Returns (included, skipped). Exposed separately from tar-building so tests
    can check exactly what would be bundled without writing a real tarball."""
    included: List[Tuple[Path, str, int]] = []
    skipped: List[dict] = []
    system_root = Path(settings.userdata_root) / "system"

    conf = system_root / "batocera.conf"
    if conf.is_file():
        _add_file(conf, "system/batocera.conf", included, skipped, max_bytes=None)

    configs_dir = system_root / "configs"
    for path in _walk_files(configs_dir):
        rel = path.relative_to(configs_dir).as_posix()
        _add_file(path, f"system/configs/{rel}", included, skipped, max_bytes=BACKUP_MAX_CONFIG_FILE_BYTES)

    roms_root = Path(settings.roms_root)
    if roms_root.is_dir():
        for system_dir in sorted(p for p in roms_root.iterdir() if p.is_dir()):
            gamelist = system_dir / "gamelist.xml"
            if gamelist.is_file():
                _add_file(gamelist, f"roms/{system_dir.name}/gamelist.xml", included, skipped, max_bytes=None)

    services_dir = system_root / "services"
    for path in _walk_files(services_dir):
        rel = path.relative_to(services_dir).as_posix()
        _add_file(path, f"system/services/{rel}", included, skipped, max_bytes=None)

    for name in _CUSTOM_SCRIPT_DIRS:
        sub_dir = system_root / name
        for path in _walk_files(sub_dir):
            rel = path.relative_to(sub_dir).as_posix()
            _add_file(path, f"system/{name}/{rel}", included, skipped, max_bytes=None)

    for path in sorted(system_root.glob("custom.sh*")):
        if path.is_file():
            _add_file(path, f"system/{path.name}", included, skipped, max_bytes=None)
    pro_custom = system_root / "pro-custom.sh"
    if pro_custom.is_file():
        _add_file(pro_custom, "system/pro-custom.sh", included, skipped, max_bytes=None)

    saves_root = Path(settings.saves_root)
    for path in _walk_files(saves_root):
        rel = path.relative_to(saves_root).as_posix()
        _add_file(path, f"saves/{rel}", included, skipped, max_bytes=BACKUP_MAX_SAVE_FILE_BYTES)

    return included, skipped


def _manifest_bytes(included: List[Tuple[Path, str, int]], skipped: List[dict]) -> bytes:
    skipped_bytes = sum(int(entry.get("size") or 0) for entry in skipped)
    lines = [
        "Batocera Drone configuration backup",
        f"Created: {datetime.now(timezone.utc).isoformat()}",
        f"Included files: {len(included)}",
        f"Skipped files: {len(skipped)} ({skipped_bytes} bytes)",
        "",
        "Skipped (over the per-file size limit, or unreadable):",
    ]
    if skipped:
        for entry in skipped:
            lines.append(f"  {entry['path']}  ({entry['size']} bytes) -- {entry['reason']}")
    else:
        lines.append("  (none)")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _generate_file_name() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"drone-config-backup-{stamp}.tar.gz"


def _build_tarball(settings: Any, file_name: str) -> dict:
    included, skipped = collect_sources(settings)
    directory = backups_directory(settings)
    final_path = directory / file_name
    tmp_path = directory / f".{file_name}.tmp"
    manifest = _manifest_bytes(included, skipped)
    try:
        with tarfile.open(tmp_path, "w:gz") as tar:
            manifest_info = tarfile.TarInfo(name="MANIFEST.txt")
            manifest_info.size = len(manifest)
            manifest_info.mtime = int(time.time())
            tar.addfile(manifest_info, io.BytesIO(manifest))
            for abs_path, arcname, _size in included:
                try:
                    tar.add(abs_path, arcname=arcname, recursive=False)
                except OSError as error:
                    skipped.append({"path": arcname, "size": 0, "reason": f"could not read: {error}"})
        tmp_path.replace(final_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return {
        "size_bytes": final_path.stat().st_size,
        "included_file_count": len(included),
        "skipped_file_count": len(skipped),
        "skipped_bytes": sum(int(entry.get("size") or 0) for entry in skipped),
    }


def _run_backup_job(settings: Any, backup_id: int, file_name: str) -> None:
    try:
        result = _build_tarball(settings, file_name)
        _store.mark_complete(
            settings,
            backup_id,
            size_bytes=result["size_bytes"],
            included_file_count=result["included_file_count"],
            skipped_file_count=result["skipped_file_count"],
            skipped_bytes=result["skipped_bytes"],
        )
    except Exception as error:  # noqa: BLE001 - must not leave the row stuck "creating"
        _store.mark_error(settings, backup_id, str(error))


def create_backup(settings: Any, *, name: str = "", description: str = "") -> dict:
    if _store.any_creating(settings):
        return {"status": "already_creating"}
    file_name = _generate_file_name()
    row = _store.create_pending(settings, file_name, name=name, description=description)
    thread = threading.Thread(
        target=_run_backup_job, args=(settings, row["id"], file_name), name="config-backup-build", daemon=True
    )
    thread.start()
    return {"status": "ok", "backup": row}


def delete_backup(settings: Any, backup_id: int) -> dict:
    row = _store.get(settings, backup_id)
    if row is None:
        return {"status": "not_found"}
    file_path = backups_directory(settings) / row["file_name"]
    file_path.unlink(missing_ok=True)
    _store.delete(settings, backup_id)
    return {"status": "deleted"}


def _drone_hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return ""


def email_backup(settings: Any, backup_id: int) -> dict:
    """Email a completed backup as an attachment. Checks configuration and
    size before ever touching smtplib, so the caller (the admin UI) can
    distinguish "SMTP isn't set up" (show a popup pointing at the Email tile)
    from "too large" from a genuine send failure, rather than one generic
    error for all three."""
    row = _store.get(settings, backup_id)
    if row is None or row.get("status") != "complete":
        return {"status": "not_found"}
    if not _smtp.get_settings(settings).get("has_config"):
        return {"status": "not_configured"}
    size_bytes = int(row.get("size_bytes") or 0)
    if size_bytes > BACKUP_EMAIL_MAX_BYTES:
        return {"status": "too_large", "size_bytes": size_bytes, "limit_bytes": BACKUP_EMAIL_MAX_BYTES}
    file_path = backups_directory(settings) / row["file_name"]
    if not file_path.is_file():
        return {"status": "not_found"}

    display_name = row.get("name") or row["file_name"]
    hostname = _drone_hostname()
    device_id = settings.device_id or ""
    machine_label = f"{hostname} ({device_id})" if hostname and device_id else (device_id or hostname or "unknown drone")
    batocera_version = _read_batocera_version(settings.userdata_root) or "unknown"
    subject = f"Batocera Drone [{machine_label}]: config backup \"{display_name}\""
    lines = [
        f"Config backup: {display_name}",
        f"Description: {row.get('description') or '(none)'}",
        f"Created: {row.get('created_at') or 'unknown'}",
        f"Size: {size_bytes} bytes",
        f"Files included: {row.get('included_file_count') or 0}",
        "",
        f"Drone: {machine_label}",
        f"Hostname: {hostname or 'unknown'}",
        f"Device ID: {device_id or 'unknown'}",
        f"Batocera version: {batocera_version}",
        f"Sent at: {datetime.now(timezone.utc).replace(microsecond=0).isoformat()}",
    ]
    if row.get("source_drone_name") or row.get("source_drone_id"):
        lines.insert(2, f"Originally created on: {row.get('source_drone_name') or row.get('source_drone_id')}")
    body = "\n".join(lines)
    try:
        _smtp.send_mail_with_attachment(settings, subject, body, file_path, row["file_name"])
    except _smtp.SmtpSendError as error:
        return {"status": "error", "error": str(error)}
    return {"status": "sent"}
