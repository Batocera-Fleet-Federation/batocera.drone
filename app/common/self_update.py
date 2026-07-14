"""Drone self-update: poll releases, download updates, and re-exec in place."""

import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Optional, Tuple
from urllib.error import HTTPError
from urllib.parse import unquote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

try:
    from .settings import Settings
except ImportError:  # pragma: no cover - direct script execution fallback
    from settings import Settings  # type: ignore

DRONE_LATEST_ARCHIVE_URL = "https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/latest/download/drone-app.tar.gz"
DRONE_LATEST_RELEASE_URL = "https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/latest"
DRONE_SELF_UPDATE_EXIT_CODE = 75
DRONE_AUTO_UPDATE_FILE = "auto-update.enabled"
DRONE_AUTO_UPDATE_POLL_SECONDS = 60

_DRONE_UPDATE_LOCK = Lock()
_SEMANTIC_VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


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


def _semantic_version(value: str) -> Optional[Tuple[int, int, int]]:
    match = _SEMANTIC_VERSION_PATTERN.fullmatch(str(value or "").strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _release_version_from_redirect(location: str) -> str:
    path = unquote(urlparse(str(location or "")).path)
    match = re.search(r"/releases/(?:tag|download)/([^/]+)", path)
    if not match or _semantic_version(match.group(1)) is None:
        raise ValueError(f"latest Drone release redirect did not contain a semantic version: {location!r}")
    return match.group(1)


def _latest_drone_release_version(timeout_seconds: float = 10.0) -> str:
    request = Request(
        DRONE_LATEST_RELEASE_URL,
        method="HEAD",
        headers={"User-Agent": "batocera-drone-auto-update"},
    )
    opener = build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            location = response.headers.get("Location") or response.geturl()
    except HTTPError as error:
        if error.code not in {301, 302, 303, 307, 308}:
            raise
        location = error.headers.get("Location")
        error.close()
    if not location:
        raise ValueError("latest Drone release response did not include a redirect location")
    return _release_version_from_redirect(location)


def _installed_drone_version(settings: Settings) -> str:
    version_file = _drone_work_dir(settings) / "app" / "VERSION"
    try:
        return version_file.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
    except (OSError, IndexError):
        return ""


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


def _download_latest_drone_app_unlocked(settings: Settings) -> dict:
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


def _download_latest_drone_app(settings: Settings) -> dict:
    with _DRONE_UPDATE_LOCK:
        return _download_latest_drone_app_unlocked(settings)


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


def _run_drone_auto_update_check_once(settings: Settings) -> dict:
    if not is_drone_auto_update_enabled(settings):
        return {"status": "disabled"}

    current_version = _installed_drone_version(settings)
    current_semantic_version = _semantic_version(current_version)
    if current_semantic_version is None:
        return {"status": "skipped", "reason": "installed version is not semantic", "current_version": current_version}

    latest_version = _latest_drone_release_version()
    latest_semantic_version = _semantic_version(latest_version)
    if latest_semantic_version is None or latest_semantic_version <= current_semantic_version:
        return {"status": "current", "current_version": current_version, "latest_version": latest_version}

    # The checkbox may have been cleared while the network check was in flight.
    if not is_drone_auto_update_enabled(settings):
        return {"status": "disabled"}

    print(
        f"Automatic Drone update found: installed={current_version} latest={latest_version}; downloading...",
        file=sys.stdout,
        flush=True,
    )
    result = _download_latest_drone_app(settings)
    result.update({"status": "updated", "current_version": current_version, "latest_version": latest_version})
    print(
        f"Automatic Drone update downloaded: {current_version} -> {latest_version}; restarting app process.",
        file=sys.stdout,
        flush=True,
    )
    _restart_drone_process_soon()
    return result


def _start_drone_auto_update_poller(
    settings: Settings,
    poll_seconds: Optional[float] = None,
    stop_event: Optional[Event] = None,
) -> Optional[Thread]:
    if poll_seconds is None:
        poll_seconds = float(os.environ.get("DRONE_AUTO_UPDATE_POLL_SECONDS", str(DRONE_AUTO_UPDATE_POLL_SECONDS)))
        if poll_seconds > 0:
            poll_seconds = max(5.0, poll_seconds)
    if poll_seconds <= 0:
        print("Automatic Drone update poller disabled: DRONE_AUTO_UPDATE_POLL_SECONDS=0", flush=True)
        return None

    stopped = stop_event or Event()

    def poll() -> None:
        last_error = ""
        while not stopped.wait(poll_seconds):
            try:
                result = _run_drone_auto_update_check_once(settings)
                last_error = ""
            except Exception as error:  # Best effort: an offline Drone must keep serving requests.
                message = f"{error.__class__.__name__}: {error}"
                if message != last_error:
                    print(f"Automatic Drone update check failed: {message}", file=sys.stderr, flush=True)
                    last_error = message
                continue
            if result.get("status") == "updated":
                return

    thread = Thread(target=poll, name="drone-auto-update-poller", daemon=True)
    thread.start()
    print(f"Automatic Drone update poller started: poll_seconds={poll_seconds:g}", flush=True)
    return thread
