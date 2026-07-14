"""Drone self-update: download the latest release bundle and re-exec in place.

Extracted from ``drone_api.py``. Triggered by an Overmind "self-update" action:
fetch the latest ``drone-app.tar.gz``, overlay it onto the running app tree, then
signal the service supervisor to relaunch by exiting with a dedicated code.
Self-contained file/tarball ops + stdlib HTTP.
"""

import os
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from threading import Thread
from urllib.request import Request, urlopen

try:
    from .settings import Settings
except ImportError:  # pragma: no cover - direct script execution fallback
    from settings import Settings  # type: ignore

DRONE_LATEST_ARCHIVE_URL = "https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/latest/download/drone-app.tar.gz"
DRONE_SELF_UPDATE_EXIT_CODE = 75
DRONE_AUTO_UPDATE_FILE = "auto-update.enabled"


def _drone_work_dir(settings: Settings) -> Path:
    return Path(os.environ.get("DRONE_APP_WORK_DIR", str(settings.userdata_root / "system" / "drone-app"))).resolve()


def _drone_auto_update_path(settings: Settings) -> Path:
    return _drone_work_dir(settings) / DRONE_AUTO_UPDATE_FILE


def is_drone_auto_update_enabled(settings: Settings) -> bool:
    path = _drone_auto_update_path(settings)
    try:
        value = path.read_text(encoding="utf-8", errors="ignore").strip().lower()
    except FileNotFoundError:
        return True
    except OSError:
        return True
    return value not in {"0", "false", "no", "off", "disabled"}


def set_drone_auto_update_enabled(settings: Settings, enabled: bool) -> bool:
    path = _drone_auto_update_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text("1\n" if enabled else "0\n", encoding="utf-8")
    temp_path.replace(path)
    return bool(enabled)


def _overlay_drone_release_tree(source: Path, target: Path) -> int:
    copied = 0
    if not source.exists() or not source.is_dir():
        raise ValueError(f"release source directory is missing: {source}")
    target.mkdir(parents=True, exist_ok=True)
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        if "__pycache__" in relative.parts or item.name.endswith(".pyc"):
            continue
        destination = target / relative
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item, destination)
        try:
            destination.chmod(0o664)
        except OSError:
            pass
        copied += 1
    return copied


def _download_latest_drone_app(settings: Settings) -> dict:
    archive_url = os.environ.get("DRONE_APP_ARCHIVE_URL", DRONE_LATEST_ARCHIVE_URL)
    work_dir = _drone_work_dir(settings)
    work_dir.mkdir(parents=True, exist_ok=True)
    started_at = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="drone-update-", dir=str(work_dir)) as temp_dir_name:
        temp_dir = Path(temp_dir_name).resolve()
        archive_path = temp_dir / "drone-app.tar.gz"
        request = Request(archive_url, headers={"User-Agent": "batocera-drone-self-update"})
        with urlopen(request, timeout=120) as response:
            with archive_path.open("wb") as output:
                shutil.copyfileobj(response, output)
        if not archive_path.exists() or archive_path.stat().st_size <= 0:
            raise ValueError("downloaded Drone archive was empty")
        stage_dir = temp_dir / "stage"
        stage_dir.mkdir()
        wanted_roots = {"app", "content"}
        extracted_roots = set()
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                relative = member.name.lstrip("/")
                parts = relative.split("/", 1)
                if parts and parts[0] not in wanted_roots and len(parts) == 2:
                    relative = parts[1]
                    parts = relative.split("/", 1)
                if not parts or parts[0] not in wanted_roots:
                    continue
                relative_path = Path(relative)
                if "__pycache__" in relative_path.parts:
                    continue
                target = (stage_dir / relative_path).resolve()
                if stage_dir not in target.parents and target != stage_dir:
                    raise ValueError(f"archive member escapes stage directory: {member.name}")
                extracted_roots.add(parts[0])
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                source = archive.extractfile(member)
                if source is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
        missing = wanted_roots - extracted_roots
        if missing:
            raise ValueError(f"Drone archive is missing required directories: {', '.join(sorted(missing))}")
        copied_files = 0
        for name in sorted(wanted_roots):
            source = stage_dir / name
            target = work_dir / name
            copied_files += _overlay_drone_release_tree(source, target)
    return {
        "status": "downloaded",
        "archive_url": archive_url,
        "work_dir": str(work_dir),
        "copied_files": copied_files,
        "duration_ms": int((time.monotonic() - started_at) * 1000),
        "restart_required": True,
    }


def _restart_drone_process_soon(delay_seconds: float = 1.0) -> None:
    def restart() -> None:
        time.sleep(max(0.1, delay_seconds))
        print(
            "Drone self-update restart requested: re-executing app process",
            file=sys.stderr,
            flush=True,
        )
        try:
            os.execv(sys.executable, [sys.executable, *sys.argv])
        except Exception as exc:
            print(
                f"Drone self-update re-exec failed: {exc!r}; exiting with code {DRONE_SELF_UPDATE_EXIT_CODE}",
                file=sys.stderr,
                flush=True,
            )
            os._exit(DRONE_SELF_UPDATE_EXIT_CODE)

    Thread(target=restart, name="drone-self-update-restart", daemon=True).start()
