"""Drone self-update: poll releases, download updates, and re-exec in place."""

import http.client
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Callable, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

try:
    from .settings import Settings
except ImportError:  # pragma: no cover - direct script execution fallback
    from settings import Settings  # type: ignore

try:
    from ..device import notifications as _notifications
    from ..storage import update_history_store as _update_history_store
except ImportError:  # pragma: no cover - flat (no `app.` prefix) package mode
    from device import notifications as _notifications  # type: ignore
    from storage import update_history_store as _update_history_store  # type: ignore

DRONE_LATEST_ARCHIVE_URL = "https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/latest/download/drone-app.tar.gz"
DRONE_RELEASE_ARCHIVE_URL_TEMPLATE = (
    "https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/download/{version}/drone-app.tar.gz"
)
DRONE_LATEST_RELEASE_URL = "https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/latest"
# Ports client (ports-client/) bundles are attached to the *same* GitHub
# release as drone-app.tar.gz (see .github/workflows/release.yml's "Build
# Ports client bundles" step), one per CPU arch it's built for -- so
# "latest" here always means the exact release that was just installed for
# the main app too. Arches match scripts/batocera_install.sh's own mapping.
DRONE_PORTS_CLIENT_ARCHIVE_URL_TEMPLATE = (
    "https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/latest/download/"
    "batocera-drone-client-{arch}.tar.gz"
)
DRONE_PORTS_CLIENT_RELEASE_ARCHIVE_URL_TEMPLATE = (
    "https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/download/{version}/"
    "batocera-drone-client-{arch}.tar.gz"
)
DRONE_PORTS_CLIENT_SUPPORTED_ARCHES = ("x86_64", "aarch64")
DRONE_RELEASE_TAG_URL_TEMPLATE = "https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/tag/{version}"
DRONE_COMPARE_API_URL_TEMPLATE = "https://api.github.com/repos/Batocera-Fleet-Federation/batocera.drone/compare/{base}...{head}"
DRONE_SELF_UPDATE_EXIT_CODE = 75
DRONE_AUTO_UPDATE_FILE = "auto-update.enabled"
DRONE_UPDATE_STATUS_FILE = "self-update-status.json"
DRONE_AUTO_UPDATE_POLL_SECONDS = 60
DRONE_SERVICE_BOOTSTRAP = Path(__file__).resolve().parents[1] / "service_bootstrap.sh"
DRONE_SERVICE_PID_FILE = Path("/tmp/drone-server.pid")
# The hand-maintained CHANGELOG.md isn't updated for every release (several
# recent tags have no entry at all), so it isn't a reliable source of "what
# changed" -- the actual commit log between two tags always is.
RELEASE_NOTES_MAX_COMMITS = 50

_DRONE_UPDATE_LOCK = Lock()
# Separate from _DRONE_UPDATE_LOCK: the Ports client update touches a
# completely different directory (roms/ports, not the Drone app's own work
# dir). The API worker installs it before taking the app-tree lock, while this
# lock prevents two Ports bundle overlays from racing each other.
_PORTS_CLIENT_UPDATE_LOCK = Lock()
_DRONE_UPDATE_WORKERS_LOCK = Lock()
_DRONE_UPDATE_WORKERS = {}
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


def _drone_update_status_path(settings: Settings) -> Path:
    return _drone_work_dir(settings) / DRONE_UPDATE_STATUS_FILE


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _write_drone_update_status(settings: Settings, payload: dict) -> dict:
    """Atomically persist worker progress so either UI can reconnect later."""
    path = _drone_update_status_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    public = {
        **payload,
        "owner": "api_worker",
        "updated_at": _now_iso(),
    }
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(public, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)
    return public


def get_drone_update_status(settings: Settings) -> dict:
    path = _drone_update_status_path(settings)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        payload = {"status": "idle"}
    if not isinstance(payload, dict):
        payload = {"status": "idle"}
    payload["owner"] = "api_worker"
    payload["current_version"] = _installed_drone_version(settings)
    return payload


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


def _release_url_for_version(version: str) -> str:
    return DRONE_RELEASE_TAG_URL_TEMPLATE.format(version=version)


def _fetch_commit_notes(previous_version: str, new_version: str, timeout_seconds: float = 10.0) -> str:
    """Best-effort: a bullet list of commit summaries between two tags, from
    GitHub's compare API. Always populated (unlike CHANGELOG.md, see above)
    since it reflects real commit history, not manual changelog upkeep.
    Returns "" on any failure or when there's no real previous version to
    compare against (e.g. the very first recorded update) -- callers must
    treat that as "notes unavailable", never let it block the update itself.
    """
    if not previous_version or not new_version or previous_version == new_version:
        return ""
    url = DRONE_COMPARE_API_URL_TEMPLATE.format(base=previous_version, head=new_version)
    request = Request(
        url,
        headers={"User-Agent": "batocera-drone-self-update", "Accept": "application/vnd.github+json"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, ValueError) as error:
        print(f"Unable to fetch Drone release notes {previous_version}...{new_version}: {error}", file=sys.stderr, flush=True)
        return ""
    commits = payload.get("commits") or []
    lines = []
    for commit in commits[:RELEASE_NOTES_MAX_COMMITS]:
        message = str((commit.get("commit") or {}).get("message") or "").splitlines()
        summary = message[0].strip() if message else ""
        if not summary:
            continue
        sha = str(commit.get("sha") or "")[:7]
        lines.append(f"- {summary} ({sha})" if sha else f"- {summary}")
    remaining = len(commits) - RELEASE_NOTES_MAX_COMMITS
    if remaining > 0:
        lines.append(f"... and {remaining} more commit(s)")
    return "\n".join(lines)


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


def _resolve_within_stage(stage_dir: Path, relative_path: Path, *, member_name: str) -> Path:
    """Reject a tar member whose resolved path would escape stage_dir (a
    "tar-slip" via a ``..`` component) -- shared by both archive extractors
    below so there is exactly one implementation of this security check to
    keep tested and correct, not two copies that could silently drift."""
    target = (stage_dir / relative_path).resolve()
    if stage_dir not in target.parents and target != stage_dir:
        raise ValueError(f"archive member escapes stage directory: {member_name}")
    return target


def _download_latest_drone_app_unlocked(settings: Settings, *, release_version: Optional[str] = None) -> dict:
    default_archive_url = (
        DRONE_RELEASE_ARCHIVE_URL_TEMPLATE.format(version=release_version)
        if release_version
        else DRONE_LATEST_ARCHIVE_URL
    )
    archive_url = os.environ.get("DRONE_APP_ARCHIVE_URL", default_archive_url)
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
                target = _resolve_within_stage(stage_dir, relative_path, member_name=member.name)
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
        required_files = (
            Path("app/main.py"),
            Path("app/drone_api.py"),
            Path("app/VERSION"),
            Path("app/web/templates/index.html"),
            Path("app/web/static/js/drone.js"),
            Path("app/web/static/css/drone.css"),
            Path("content/batocera-swarm-mascot.jpg"),
            Path("content/drone.png"),
        )
        missing_files = [
            str(path)
            for path in required_files
            if not (stage_dir / path).is_file() or (stage_dir / path).stat().st_size <= 0
        ]
        if missing_files:
            raise ValueError(f"Drone archive is incomplete; missing web/API payload: {', '.join(missing_files)}")
        archive_version = (stage_dir / "app" / "VERSION").read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
        if release_version and archive_version != release_version:
            raise ValueError(
                f"Drone archive version mismatch: expected {release_version}, found {archive_version or 'empty'}"
            )
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


def _ports_client_bundle_arch() -> Optional[str]:
    machine = platform.machine()
    return machine if machine in DRONE_PORTS_CLIENT_SUPPORTED_ARCHES else None


def _ports_client_dir(settings: Settings) -> Path:
    return settings.roms_root / "ports"


def _reload_emulationstation_gamelists(timeout_seconds: float = 5.0) -> dict:
    """Ask the local Batocera EmulationStation process to reload gamelists.

    EmulationStation keeps gamelist metadata in memory after startup. Its
    localhost-only service exposes /reloadgames specifically for applying
    external gamelist edits without killing a running game or restarting the
    frontend. An unavailable ES process is non-fatal: the metadata remains on
    disk and will be loaded on its next start.
    """
    connection = http.client.HTTPConnection("127.0.0.1", 1234, timeout=timeout_seconds)
    try:
        connection.request("GET", "/reloadgames")
        response = connection.getresponse()
        response.read()
        if 200 <= response.status < 300:
            return {"status": "requested", "http_status": response.status}
        return {
            "status": "error",
            "http_status": response.status,
            "error": str(response.reason or f"HTTP {response.status}"),
        }
    except (OSError, http.client.HTTPException) as error:
        return {"status": "unavailable", "error": f"{error.__class__.__name__}: {error}"}
    finally:
        connection.close()


def _refresh_installed_ports_client_gamelist(settings: Settings) -> dict:
    """Run the bundled, idempotent ES metadata integration after install/start.

    The startup call is important for the first upgrade from a release whose
    old updater could copy the newly fixed bundle but did not yet know to run
    its helper. Once the API restarts into the new code, this reconciles the
    artwork without waiting for another Drone release.
    """
    try:
        ports_dir = _ports_client_dir(settings)
    except AttributeError:  # lightweight settings doubles in unit tests
        return {"status": "unavailable"}
    gamelist_helper = ports_dir / ".data" / "batocera-drone-client" / "client" / "gamelist_integration.py"
    if not gamelist_helper.is_file():
        return {"status": "unavailable"}
    try:
        completed = subprocess.run(
            [sys.executable, str(gamelist_helper), str(ports_dir)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if completed.returncode == 0:
            try:
                payload = json.loads(completed.stdout.strip())
            except ValueError:
                payload = {"status": "updated"}
            result = dict(payload) if isinstance(payload, dict) else {"status": "updated"}
            # Reload even when the helper reports "current". This covers the
            # first API start after an older updater installed the new Ports
            # bundle and edited gamelist.xml before the new API code existed.
            result["emulationstation_reload"] = _reload_emulationstation_gamelists()
            return result
        detail = (completed.stderr or completed.stdout or "unknown error").strip()
        print(f"Ports artwork gamelist refresh failed: {detail}", file=sys.stderr, flush=True)
        return {"status": "error", "error": detail[:500]}
    except (OSError, subprocess.SubprocessError) as error:
        print(f"Ports artwork gamelist refresh failed: {error}", file=sys.stderr, flush=True)
        return {"status": "error", "error": f"{error.__class__.__name__}: {error}"}


def _download_latest_ports_client_unlocked(settings: Settings, *, release_version: Optional[str] = None) -> dict:
    """Downloads and installs the Ports client bundle matching this device's
    CPU arch into <roms_root>/ports -- the same tarball layout (a top-level
    launcher script + .data/batocera-drone-client/, see
    ports-client/scripts/build_release_bundle.sh) that
    batocera_install.sh's install_ports_client() extracts there directly.

    On a supported architecture this is a required part of a complete Drone
    release. The caller installs it before advancing the web/API app version,
    ensuring a missing or incomplete Ports asset leaves the release retryable.
    """
    arch = _ports_client_bundle_arch()
    if arch is None:
        return {"status": "unsupported_arch", "arch": platform.machine()}

    default_archive_url = (
        DRONE_PORTS_CLIENT_RELEASE_ARCHIVE_URL_TEMPLATE.format(version=release_version, arch=arch)
        if release_version
        else DRONE_PORTS_CLIENT_ARCHIVE_URL_TEMPLATE.format(arch=arch)
    )
    archive_url = os.environ.get("DRONE_PORTS_CLIENT_ARCHIVE_URL", default_archive_url)
    ports_dir = _ports_client_dir(settings)
    ports_dir.mkdir(parents=True, exist_ok=True)
    started_at = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="drone-ports-client-update-", dir=str(ports_dir)) as temp_dir_name:
        temp_dir = Path(temp_dir_name).resolve()
        archive_path = temp_dir / "batocera-drone-client.tar.gz"
        request = Request(archive_url, headers={"User-Agent": "batocera-drone-self-update"})
        with urlopen(request, timeout=120) as response:
            with archive_path.open("wb") as output:
                shutil.copyfileobj(response, output)
        if not archive_path.exists() or archive_path.stat().st_size <= 0:
            raise ValueError("downloaded Ports client archive was empty")

        stage_dir = temp_dir / "stage"
        stage_dir.mkdir()
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                relative_path = Path(member.name.lstrip("/"))
                target = _resolve_within_stage(stage_dir, relative_path, member_name=member.name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                source = archive.extractfile(member)
                if source is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)

        required_files = (
            Path("batocera-drone-client.sh"),
            Path("images/batocera-drone_marquee.png"),
            Path("images/main.jpg"),
            Path(".data/batocera-drone-client/main.py"),
            Path(".data/batocera-drone-client/client/endpoints.py"),
            Path(".data/batocera-drone-client/client/gamelist_integration.py"),
            Path(".data/batocera-drone-client/ui/app.py"),
            Path(".data/batocera-drone-client/ui/assets/logo.png"),
        )
        missing_files = [
            str(path)
            for path in required_files
            if not (stage_dir / path).is_file() or (stage_dir / path).stat().st_size <= 0
        ]
        if missing_files:
            raise ValueError(f"Ports client archive is incomplete; missing: {', '.join(missing_files)}")

        copied_files = _overlay_drone_release_tree(stage_dir, ports_dir)
        # _overlay_drone_release_tree chmods every copied file 0o664 (right
        # for the app's own tree, which is never executed directly) -- the
        # Ports launcher is exec'd straight by EmulationStation and needs
        # its executable bit restored.
        launcher = ports_dir / "batocera-drone-client.sh"
        if launcher.is_file():
            try:
                launcher.chmod(0o755)
            except OSError:
                pass

        gamelist_result = _refresh_installed_ports_client_gamelist(settings)

    return {
        "status": "updated",
        "arch": arch,
        "archive_url": archive_url,
        "ports_dir": str(ports_dir),
        "copied_files": copied_files,
        "duration_ms": int((time.monotonic() - started_at) * 1000),
        "gamelist": gamelist_result,
    }


def _download_latest_ports_client(settings: Settings, *, release_version: Optional[str] = None) -> dict:
    try:
        with _PORTS_CLIENT_UPDATE_LOCK:
            return _download_latest_ports_client_unlocked(settings, release_version=release_version)
    except Exception as error:  # noqa: BLE001 - convert worker failure to structured status
        message = f"{error.__class__.__name__}: {error}"
        print(
            f"Ports client update failed; complete Drone release will be retried: {message}",
            file=sys.stderr,
            flush=True,
        )
        return {"status": "error", "error": message}


def _download_latest_drone_app(settings: Settings, *, release_version: Optional[str] = None) -> dict:
    previous_version = _installed_drone_version(settings)
    # Install the architecture-matched Ports runtime first. On supported
    # Batocera architectures an error aborts the overall update before the
    # app VERSION changes, so the API worker retries the whole release on its
    # next check instead of declaring the web UI current while Ports is stale.
    ports_client = _download_latest_ports_client(settings, release_version=release_version)
    if ports_client.get("status") == "error":
        raise RuntimeError(f"Ports client bundle is required: {ports_client.get('error') or 'update failed'}")
    with _DRONE_UPDATE_LOCK:
        result = _download_latest_drone_app_unlocked(settings, release_version=release_version)
    result["ports_client"] = ports_client
    new_version = _installed_drone_version(settings)
    release_url = _release_url_for_version(new_version) if new_version else ""
    release_notes = _fetch_commit_notes(previous_version, new_version)
    _update_history_store.record_update(
        settings,
        version=new_version,
        previous_version=previous_version,
        release_url=release_url,
        release_notes=release_notes,
    )
    message = f"{previous_version or 'unknown'} -> {new_version or 'unknown'}; restarting to apply."
    if release_notes:
        message = f"{message}\n\n{release_notes}"
    _notifications.record_event(settings, "drone_updated", "Drone app updated", message)
    return result


def _schedule_supervised_service_restart(delay_seconds: float) -> bool:
    """Ask the newly staged bootstrap to replace the whole service tree.

    A plain app-process exec cannot adopt changes to the already-running shell
    supervisor. Launch the restart in a detached session so it survives killing
    this app and its old supervisor. Unsupervised/dev runs retain the exec path.
    """
    if not DRONE_SERVICE_BOOTSTRAP.is_file() or not DRONE_SERVICE_PID_FILE.is_file():
        return False
    try:
        subprocess.Popen(
            [
                "sh",
                "-c",
                'sleep "$1"; exec sh "$2" restart',
                "drone-service-update",
                str(max(0.1, delay_seconds)),
                str(DRONE_SERVICE_BOOTSTRAP),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    return True


def _restart_drone_process_soon(delay_seconds: float = 1.0) -> None:
    def restart() -> None:
        if _schedule_supervised_service_restart(delay_seconds):
            print(
                "Drone self-update restart requested: replacing supervised service",
                file=sys.stderr,
                flush=True,
            )
            return
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


def _run_drone_auto_update_check_once(
    settings: Settings,
    *,
    respect_auto_update_setting: bool = True,
    progress_callback: Optional[Callable[..., None]] = None,
) -> dict:
    if respect_auto_update_setting and not is_drone_auto_update_enabled(settings):
        return {"status": "disabled"}

    current_version = _installed_drone_version(settings)
    current_semantic_version = _semantic_version(current_version)
    if current_semantic_version is None:
        return {"status": "skipped", "reason": "installed version is not semantic", "current_version": current_version}

    if progress_callback:
        progress_callback("checking", "Checking GitHub for the latest complete Drone release", current_version=current_version)
    latest_version = _latest_drone_release_version()
    latest_semantic_version = _semantic_version(latest_version)
    if latest_semantic_version is None or latest_semantic_version <= current_semantic_version:
        return {"status": "current", "current_version": current_version, "latest_version": latest_version}

    # The checkbox may have been cleared while the network check was in flight.
    if respect_auto_update_setting and not is_drone_auto_update_enabled(settings):
        return {"status": "disabled"}

    print(
        f"Drone update worker found: installed={current_version} latest={latest_version}; downloading...",
        file=sys.stdout,
        flush=True,
    )
    if progress_callback:
        progress_callback(
            "downloading",
            "Downloading and installing web/API code, web images, and the Ports client bundle",
            current_version=current_version,
            latest_version=latest_version,
        )
    # Pin every artifact to the version discovered by the check. This avoids
    # mixing Ports and web/API files if GitHub's "latest" release changes
    # between the HEAD request and the two downloads.
    result = _download_latest_drone_app(settings, release_version=latest_version)
    result.update({"status": "updated", "current_version": current_version, "latest_version": latest_version})
    print(
        f"Drone update worker downloaded: {current_version} -> {latest_version}; restarting app process.",
        file=sys.stdout,
        flush=True,
    )
    if progress_callback:
        progress_callback(
            "restart_scheduled",
            "Both UI bundles are installed; restarting the Drone API service",
            current_version=current_version,
            latest_version=latest_version,
            ports_client=result.get("ports_client"),
        )
    _restart_drone_process_soon()
    return result


def request_drone_update_check(settings: Settings, *, source: str = "manual") -> dict:
    """Queue a complete update check on an API-owned background worker.

    Web and Ports clients are intentionally limited to this submission API
    and the read-only status API. They never contact GitHub, extract release
    archives, modify the installed tree, or restart the service themselves.
    """
    normalized_source = "automatic" if source == "automatic" else "manual"
    key = str(_drone_work_dir(settings))
    with _DRONE_UPDATE_WORKERS_LOCK:
        existing = _DRONE_UPDATE_WORKERS.get(key)
        if existing is not None and existing.is_alive():
            return {**get_drone_update_status(settings), "accepted": False, "already_running": True}

        queued = _write_drone_update_status(
            settings,
            {
                "status": "queued",
                "source": normalized_source,
                "accepted": True,
                "requested_at": _now_iso(),
                "detail": "Update check accepted by the Drone API worker",
            },
        )

        def progress(status: str, detail: str, **extra) -> None:
            _write_drone_update_status(
                settings,
                {
                    "status": status,
                    "source": normalized_source,
                    "requested_at": queued["requested_at"],
                    "detail": detail,
                    **extra,
                },
            )

        def run() -> None:
            try:
                result = _run_drone_auto_update_check_once(
                    settings,
                    respect_auto_update_setting=normalized_source == "automatic",
                    progress_callback=progress,
                )
                status = "restart_scheduled" if result.get("status") == "updated" else str(result.get("status") or "complete")
                _write_drone_update_status(
                    settings,
                    {
                        **result,
                        "status": status,
                        "source": normalized_source,
                        "requested_at": queued["requested_at"],
                        "detail": (
                            "Both UI bundles are installed; the Drone API service is restarting"
                            if status == "restart_scheduled"
                            else "The installed Drone release is already current"
                            if status == "current"
                            else str(result.get("reason") or "Update check completed")
                        ),
                    },
                )
            except Exception as error:  # noqa: BLE001 - worker must report failures instead of dying silently
                _write_drone_update_status(
                    settings,
                    {
                        "status": "error",
                        "source": normalized_source,
                        "requested_at": queued["requested_at"],
                        "detail": f"{error.__class__.__name__}: {error}",
                        "error": str(error),
                    },
                )

        thread = Thread(target=run, name="drone-self-update-worker", daemon=True)
        _DRONE_UPDATE_WORKERS[key] = thread
        thread.start()
        return queued


def _start_drone_auto_update_poller(
    settings: Settings,
    poll_seconds: Optional[float] = None,
    stop_event: Optional[Event] = None,
) -> Optional[Thread]:
    # Reconcile versioned Ports metadata on every API start. This is cheap and
    # idempotent, and makes the first upgrade from the previously incomplete
    # bundle refresh its EmulationStation marquee immediately after restart.
    _refresh_installed_ports_client_gamelist(settings)
    if poll_seconds is None:
        poll_seconds = float(os.environ.get("DRONE_AUTO_UPDATE_POLL_SECONDS", str(DRONE_AUTO_UPDATE_POLL_SECONDS)))
        if poll_seconds > 0:
            poll_seconds = max(5.0, poll_seconds)
    if poll_seconds <= 0:
        print("Automatic Drone update poller disabled: DRONE_AUTO_UPDATE_POLL_SECONDS=0", flush=True)
        return None

    stopped = stop_event or Event()

    def poll() -> None:
        while not stopped.wait(poll_seconds):
            if not is_drone_auto_update_enabled(settings):
                continue
            # The poller only schedules work. The same named API worker used
            # by the manual endpoint owns GitHub I/O, validation, installation,
            # persistent status, and restart scheduling for automatic checks.
            request_drone_update_check(settings, source="automatic")

    thread = Thread(target=poll, name="drone-auto-update-poller", daemon=True)
    thread.start()
    print(f"Automatic Drone update poller started: poll_seconds={poll_seconds:g}", flush=True)
    return thread
