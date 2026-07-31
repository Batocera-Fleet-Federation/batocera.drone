"""Config-backup tarball creation for the "Backups" admin tile.

Bundles Batocera + emulator settings (not the Drone app's own credentials --
see the skill/PR discussion for why that's excluded) into one downloadable
``tar.gz``:

* ``system/batocera.conf``
* ``system/configs/**`` -- recursive, but only files whose extension is a
  recognized text-based settings format (``_CONFIG_TEXT_EXTENSIONS``), still
  capped per-file at ``BACKUP_MAX_CONFIG_FILE_BYTES``. This directory is
  *not* reliably "just settings": on a real device it held 254GB, almost all
  of it one emulator's installed game/firmware content (Switch ``.nca``
  files); a later live audit of a real ~1.44GB backup found several
  emulators ship their OWN UI resources (icons, bezels, fonts, sound
  effects -- e.g. Dolphin's ``Sys/Resources/``, hypseus-singe's ``pics/``)
  and firmware/NAND installs (yuzu, Ryujinx, RPCS3) inside this same tree,
  none of which is "configuration". An extension allowlist excludes all of
  that -- and anything similar from an emulator Batocera adds in the future
  -- without a hand-maintained per-emulator exclude list; the size cap
  remains only as a backstop for a genuinely oversized text file.
* ``roms/<system>/gamelist.xml`` for every system folder
* ``system/services/*``, ``system/custom.sh*``, ``system/pro-custom.sh``,
  ``system/{custom,custom-scripts,scripts}/*`` -- small user-customization
  scripts
* ``saves/**`` -- recursive, all extensions kept (unlike ``configs/``, a real
  save file is legitimately binary in whatever format its emulator uses),
  still size-capped, generously, as a defensive measure. Cache directories,
  firmware/disk-image extensions, known emulated firmware/OS-partition
  directories, and a plugin's own bundled reference data (see
  ``_CACHE_DIR_NAMES`` / ``_FIRMWARE_IMAGE_EXTENSIONS`` /
  ``_EMULATOR_FIRMWARE_DIR_NAMES`` / ``_is_plugin_reference_data`` below) are
  excluded here too -- the same live audit found a 238MB Xbox virtual-disk
  image, ~1GB of Vita3K/yuzu emulated-firmware-partition data, and a 60MB
  MAME History-plugin database (``plugins/data/history.db``, a bundled
  reference dataset, not anyone's save) filed under "saves" that isn't
  actually save data.

Deliberately excluded: ``/userdata/bios`` and ROM files themselves (large,
copyrighted, not "settings"), the Drone app's own state (credentials,
VPN/SMTP passwords -- a separate, security-sensitive category the user chose
to keep out of a downloadable file).

The tarball is built on a background thread (creation can take real time
once saves are included) and written to a temp path, then atomically
renamed into place -- a half-built tarball is never visible/downloadable.
Only one build runs at a time; the SQLite ``config_backups`` row (not an
in-process flag) is the single source of truth for that, so it works
correctly across process restarts and doesn't leak state between tests.
"""

from __future__ import annotations

import io
import json
import os
import re
import socket
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Tuple

try:
    from ..apply_config_backup import restore_config_backup as _restore_config_backup_helper
    from ..common.batocera_version import _read_batocera_version
    from ..common.runtime_state import _ES_LIFECYCLE_LOCK
    from ..device import notifications as _notifications
    from ..device import smtp_manager as _smtp
    from ..storage import config_backup_store as _store
except ImportError:  # pragma: no cover - direct script execution fallback
    from apply_config_backup import restore_config_backup as _restore_config_backup_helper  # type: ignore
    from common.batocera_version import _read_batocera_version  # type: ignore
    from common.runtime_state import _ES_LIFECYCLE_LOCK  # type: ignore
    from device import notifications as _notifications  # type: ignore
    from device import smtp_manager as _smtp  # type: ignore
    from storage import config_backup_store as _store  # type: ignore

BACKUP_MAX_CONFIG_FILE_BYTES = 20 * 1024 * 1024  # 20MB, see module docstring
BACKUP_MAX_SAVE_FILE_BYTES = 500 * 1024 * 1024  # defensive only; real saves are far smaller
# Most SMTP providers cap attachments around 20-25MB (Gmail is 25MB); a backup
# dominated by saves easily exceeds that, so this is checked up front with a
# clear error rather than letting the send fail obscurely partway through.
BACKUP_EMAIL_MAX_BYTES = 25 * 1024 * 1024
# Generous: EmulationStation stop/start is usually well under a minute, but a
# restore can also be extracting saves, so this leaves real headroom rather
# than timing out a restore that is still genuinely in progress.
APPLY_SERVICE_CONTROL_TIMEOUT_SECONDS = 600

_CUSTOM_SCRIPT_DIRS = ("custom", "custom-scripts", "scripts")

# Genuine, human-editable configuration/settings text formats -- see the module
# docstring for why system/configs/** is filtered to this allowlist rather than
# a size cap alone. Deliberately excludes image/audio/video/font/executable/
# library/archive/database formats (all confirmed present in a real backup)
# and, implicitly, anything with no extension at all (firmware/NAND dumps
# overwhelmingly have none in practice).
_CONFIG_TEXT_EXTENSIONS = {
    ".cfg", ".conf", ".config", ".ini", ".xml", ".json", ".yaml", ".yml",
    ".toml", ".properties", ".txt", ".cnf", ".list", ".opt", ".lpl",
}

# Pure caches -- regenerated automatically, never "settings" or "save data".
# Matched as any directory segment (not just top-level), since some emulators
# nest their own cache dir a level or two down.
_CACHE_DIR_NAMES = {"cache", "mesa_shader_cache", "shader_cache", "shadercache", "radv_builtin_shaders"}

# Full-disk/virtual-machine image formats -- never a real per-game save file
# regardless of which emulator wrote them.
_FIRMWARE_IMAGE_EXTENSIONS = {".qcow2", ".vdi", ".vmdk", ".img", ".iso"}

# Emulated OS/firmware partitions some emulators keep alongside real per-game
# saves in the same "saves" tree -- shipped system software, not anything the
# user created. Vita3K's vs0 (firmware) / sa0 (system app data) / os0 (OS
# core) / vd0 / pd0 partitions; real Vita game saves live under ux0, which is
# deliberately NOT in this list and stays included.
_EMULATOR_FIRMWARE_DIR_NAMES = {"vs0", "sa0", "os0", "vd0", "pd0"}

# A Switch title ID of all zeros is always the system/firmware pseudo-title
# (keys, NAND, firmware updates) in yuzu/Ryujinx-style NAND layouts, never a
# real game's save data -- matched by pattern so this generalizes to any such
# emulator rather than naming each one.
_ALL_ZERO_TITLE_ID = re.compile(r"^0{16}$")

# A plugin's own "data" subdirectory holds shipped/downloaded reference
# datasets, not saves -- confirmed live: MAME's History plugin bundles
# saves/mame/plugins/data/history.db (60MB, dated 2024 -- clearly not
# generated by anything the user did). A plugin's own small settings file
# alongside it (e.g. plugins/hiscore/plugin.cfg) has no "data" segment and
# stays included. Both segments are required (not just "data" alone) so an
# unrelated emulator's legitimately-named "data" save subdirectory isn't
# caught by this.
_PLUGIN_DIR_NAME = "plugins"
_PLUGIN_REFERENCE_DATA_DIR_NAME = "data"


def _is_plugin_reference_data(parts: Tuple[str, ...]) -> bool:
    dirs = [part.lower() for part in parts[:-1]]
    return _PLUGIN_DIR_NAME in dirs and _PLUGIN_REFERENCE_DATA_DIR_NAME in dirs


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


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _has_dir_segment(parts: Tuple[str, ...], names) -> bool:
    # parts includes the filename as its last element -- only directory
    # segments should be checked against a directory-name set.
    return any(part.lower() in names for part in parts[:-1])


def _has_all_zero_title_id_segment(parts: Tuple[str, ...]) -> bool:
    return any(_ALL_ZERO_TITLE_ID.match(part) for part in parts[:-1])


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
        rel = path.relative_to(configs_dir)
        arcname = f"system/configs/{rel.as_posix()}"
        if _has_dir_segment(rel.parts, _CACHE_DIR_NAMES):
            skipped.append({"path": arcname, "size": _safe_size(path), "reason": "cache directory, not configuration"})
            continue
        if path.suffix.lower() not in _CONFIG_TEXT_EXTENSIONS:
            skipped.append({"path": arcname, "size": _safe_size(path), "reason": "not a recognized configuration file type"})
            continue
        _add_file(path, arcname, included, skipped, max_bytes=BACKUP_MAX_CONFIG_FILE_BYTES)

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
        rel = path.relative_to(saves_root)
        arcname = f"saves/{rel.as_posix()}"
        if _has_dir_segment(rel.parts, _CACHE_DIR_NAMES):
            skipped.append({"path": arcname, "size": _safe_size(path), "reason": "cache directory, not save data"})
            continue
        if _has_dir_segment(rel.parts, _EMULATOR_FIRMWARE_DIR_NAMES) or _has_all_zero_title_id_segment(rel.parts):
            skipped.append({"path": arcname, "size": _safe_size(path), "reason": "emulator firmware/system-partition data, not a save file"})
            continue
        if path.suffix.lower() in _FIRMWARE_IMAGE_EXTENSIONS:
            skipped.append({"path": arcname, "size": _safe_size(path), "reason": "firmware/disk-image format, not a save file"})
            continue
        if _is_plugin_reference_data(rel.parts):
            skipped.append({"path": arcname, "size": _safe_size(path), "reason": "plugin-bundled reference data, not a save file"})
            continue
        _add_file(path, arcname, included, skipped, max_bytes=BACKUP_MAX_SAVE_FILE_BYTES)

    return included, skipped


# A real device's saves/configs trees can contain thousands of non-config
# files (images, firmware, caches); listing every one individually would make
# MANIFEST.txt itself unwieldy. The aggregate count/bytes above is always
# accurate regardless of this cap -- only the per-file listing is truncated.
_MANIFEST_MAX_LISTED_SKIPPED = 500


def _manifest_bytes(included: List[Tuple[Path, str, int]], skipped: List[dict]) -> bytes:
    skipped_bytes = sum(int(entry.get("size") or 0) for entry in skipped)
    lines = [
        "Batocera Drone configuration backup",
        f"Created: {datetime.now(timezone.utc).isoformat()}",
        f"Included files: {len(included)}",
        f"Skipped files: {len(skipped)} ({skipped_bytes} bytes)",
        "",
        "Skipped (over the per-file size limit, unreadable, or not recognized as configuration/save data):",
    ]
    if skipped:
        for entry in skipped[:_MANIFEST_MAX_LISTED_SKIPPED]:
            lines.append(f"  {entry['path']}  ({entry['size']} bytes) -- {entry['reason']}")
        remaining = len(skipped) - _MANIFEST_MAX_LISTED_SKIPPED
        if remaining > 0:
            lines.append(f"  ... and {remaining} more")
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


def build_backup_tree(settings: Any, backup_id: int) -> dict:
    """List every member (files only -- there are no directory entries, see
    ``_build_tarball``'s ``recursive=False`` add) with its size, for the
    read-only "view contents" tree in the admin UI. Never extracts anything
    to disk; ``tarfile`` reads the member headers by streaming through the
    gzip stream, so this is proportional to the archive's compressed size,
    not something requiring a full extraction to answer."""
    row = _store.get(settings, backup_id)
    if row is None or row.get("status") != "complete":
        return {"status": "not_found"}
    file_path = backups_directory(settings) / row["file_name"]
    if not file_path.is_file():
        return {"status": "not_found"}
    files: List[dict] = []
    try:
        with tarfile.open(file_path, "r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                files.append({"relative_path": member.name, "size": member.size})
    except (tarfile.TarError, OSError) as error:
        return {"status": "error", "error": str(error)}
    return {
        "status": "ok",
        "file_name": row["file_name"],
        "name": row.get("name") or "",
        "size_bytes": row.get("size_bytes") or 0,
        "files": files,
    }


def _request_config_backup_apply_service_control(request_payload: dict) -> bool:
    control_dir = Path(os.environ.get("DRONE_SERVICE_CONTROL_DIR", "/userdata/system/drone-app/control"))
    request_path = control_dir / "apply-config-backup.request"
    result_path = control_dir / "apply-config-backup.result"
    try:
        result_path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        control_dir.mkdir(parents=True, exist_ok=True)
        request_path.write_text(json.dumps(request_payload), encoding="utf-8")
    except OSError:
        return False
    deadline = time.monotonic() + max(
        30.0, float(os.environ.get("DRONE_CONFIG_BACKUP_APPLY_TIMEOUT_SECONDS", str(APPLY_SERVICE_CONTROL_TIMEOUT_SECONDS)))
    )
    while time.monotonic() < deadline:
        try:
            if result_path.exists():
                result = result_path.read_text(encoding="utf-8", errors="ignore").strip()
                result_path.unlink(missing_ok=True)
                if result == "ok":
                    return True
                raise OSError(result or "Privileged config backup restore failed")
        except OSError:
            raise
        time.sleep(0.25)
    try:
        request_path.unlink(missing_ok=True)
    except OSError:
        pass
    raise OSError("Timed out waiting for the privileged config backup restore")


def apply_backup_to_machine(settings: Any, backup_id: int) -> dict:
    """Extract a completed backup back over this machine's real config/gamelist/
    saves paths. This is an overlay, not a wipe-and-replace: only the files the
    backup actually contains are written, straight onto their mapped destination;
    nothing is deleted and no destination directory is cleared first, so a config
    file or save from a different emulator/game that simply isn't part of this
    backup is left untouched. Overwriting a file the backup DOES contain is still
    irreversible -- the admin UI requires an explicit confirmation before calling
    this. Stops EmulationStation (and any running game) during the copy and
    restarts it afterward; see apply_config_backup.py for the actual extraction, shared
    between this in-process (root) path and the privileged-worker path below so
    the sequence can never drift between the two entry points."""
    row = _store.get(settings, backup_id)
    if row is None or row.get("status") != "complete":
        return {"status": "not_found"}
    archive_path = backups_directory(settings) / row["file_name"]
    if not archive_path.is_file():
        return {"status": "not_found"}

    if getattr(settings, "use_fake_data", False):
        result = {"restored_file_count": row.get("included_file_count") or 0, "skipped_file_count": 0, "restarted_emulationstation": False}
    else:
        request_payload = {
            "archive_path": str(archive_path),
            "userdata_root": str(settings.userdata_root),
            "roms_root": str(settings.roms_root),
            "saves_root": str(settings.saves_root),
        }
        try:
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                # Serialized against screen-mode/ES-collections restarts for the same
                # reason those share this lock: two overlapping stop/start sequences
                # race the same EmulationStation/X session and can wedge it into a
                # permanent crash loop.
                with _ES_LIFECYCLE_LOCK:
                    result = _restore_config_backup_helper(
                        archive_path, settings.userdata_root, settings.roms_root, settings.saves_root
                    )
            else:
                if not _request_config_backup_apply_service_control(request_payload):
                    raise OSError(
                        "Unable to dispatch the privileged config backup restore request; the Drone "
                        "service control worker may not be running."
                    )
                # The privileged worker doesn't hand back file counts (only "ok"/error
                # text) -- report the backup's own known file count instead of a real
                # restored-count, which is still meaningful to show the user.
                result = {
                    "restored_file_count": row.get("included_file_count") or 0,
                    "skipped_file_count": 0,
                    "restarted_emulationstation": True,
                }
        except (OSError, RuntimeError) as error:
            return {"status": "error", "error": str(error)}

    _notifications.record_event(
        settings,
        "config_backup_applied",
        "Config backup applied to this machine",
        f"{row.get('name') or row['file_name']}: {result.get('restored_file_count', 0)} file(s) restored.",
    )
    return {"status": "ok", **result}


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
