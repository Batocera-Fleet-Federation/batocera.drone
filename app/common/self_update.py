"""Signed Drone self-update with staged extraction and atomic activation."""

import hashlib
import json
import os
import re
import shutil
import subprocess
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
DRONE_LATEST_MANIFEST_URL = "https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/latest/download/release-manifest.json"
DRONE_LATEST_MANIFEST_SIGNATURE_URL = "https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/latest/download/release-manifest.sig"
DRONE_LATEST_RELEASE_URL = "https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/latest"
DRONE_RELEASE_DOWNLOAD_ROOT = "https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/download"
DRONE_UPDATE_PUBLIC_KEY = Path(__file__).resolve().parents[1] / "update-signing-public.pem"
DRONE_UPDATE_MANIFEST_MAX_BYTES = 64 * 1024
DRONE_UPDATE_SIGNATURE_MAX_BYTES = 16 * 1024
DRONE_UPDATE_ARCHIVE_MAX_BYTES = 128 * 1024 * 1024
DRONE_SELF_UPDATE_EXIT_CODE = 75
DRONE_AUTO_UPDATE_FILE = "auto-update.enabled"
DRONE_AUTO_UPDATE_POLL_SECONDS = 60
DRONE_SERVICE_BOOTSTRAP = Path(__file__).resolve().parents[1] / "service_bootstrap.sh"
DRONE_SERVICE_PID_FILE = Path("/tmp/drone-server.pid")

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


def _download_file(url: str, destination: Path, *, max_bytes: int) -> None:
    request = Request(url, headers={"User-Agent": "batocera-drone-self-update"})
    total = 0
    with urlopen(request, timeout=120) as response, destination.open("wb") as output:
        while True:
            chunk = response.read(min(1024 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"download exceeded the {max_bytes}-byte safety limit: {url}")
            output.write(chunk)
    if total <= 0:
        raise ValueError(f"download was empty: {url}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_manifest_signature(manifest_path: Path, signature_path: Path) -> None:
    public_key = Path(os.environ.get("DRONE_UPDATE_PUBLIC_KEY_FILE", str(DRONE_UPDATE_PUBLIC_KEY))).resolve()
    if not public_key.is_file():
        raise ValueError(f"Drone update public key is missing: {public_key}")
    openssl = shutil.which("openssl")
    if not openssl:
        raise ValueError("openssl is required to verify Drone update signatures")
    result = subprocess.run(
        [
            openssl,
            "dgst",
            "-sha256",
            "-verify",
            str(public_key),
            "-signature",
            str(signature_path),
            str(manifest_path),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0 or "Verified OK" not in (result.stdout or ""):
        raise ValueError("Drone release manifest signature verification failed")


def _load_release_manifest(manifest_path: Path) -> dict:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Drone release manifest is invalid: {error}") from error
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise ValueError("Drone release manifest has an unsupported schema")
    version = str(payload.get("version") or "")
    if _semantic_version(version) is None:
        raise ValueError("Drone release manifest has an invalid version")
    assets = payload.get("assets")
    if not isinstance(assets, dict):
        raise ValueError("Drone release manifest has no asset map")
    for asset_name, metadata in assets.items():
        if not isinstance(asset_name, str) or Path(asset_name).name != asset_name:
            raise ValueError("Drone release manifest contains an unsafe asset name")
        if not isinstance(metadata, dict):
            raise ValueError(f"Drone release manifest metadata is invalid for {asset_name}")
        sha256 = str(metadata.get("sha256") or "")
        size = metadata.get("size")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256) or not isinstance(size, int) or size <= 0:
            raise ValueError(f"Drone release manifest digest is invalid for {asset_name}")
    if "drone-app.tar.gz" not in assets:
        raise ValueError("Drone release manifest does not describe drone-app.tar.gz")
    return payload


def _verified_release_manifest(temp_dir: Path) -> dict:
    manifest_url = os.environ.get("DRONE_UPDATE_MANIFEST_URL", DRONE_LATEST_MANIFEST_URL)
    signature_url = os.environ.get("DRONE_UPDATE_MANIFEST_SIGNATURE_URL", DRONE_LATEST_MANIFEST_SIGNATURE_URL)
    manifest_path = temp_dir / "release-manifest.json"
    signature_path = temp_dir / "release-manifest.sig"
    _download_file(manifest_url, manifest_path, max_bytes=DRONE_UPDATE_MANIFEST_MAX_BYTES)
    _download_file(signature_url, signature_path, max_bytes=DRONE_UPDATE_SIGNATURE_MAX_BYTES)
    _verify_manifest_signature(manifest_path, signature_path)
    return _load_release_manifest(manifest_path)


def _verify_release_asset(path: Path, metadata: dict) -> None:
    expected_size = int(metadata["size"])
    actual_size = path.stat().st_size
    if actual_size != expected_size:
        raise ValueError(f"Drone archive size mismatch: expected {expected_size}, got {actual_size}")
    actual_digest = _sha256_file(path)
    if actual_digest != metadata["sha256"]:
        raise ValueError("Drone archive SHA-256 verification failed")


def _extract_release_archive(archive_path: Path, stage_dir: Path) -> int:
    wanted_roots = {"app", "content"}
    extracted_roots = set()
    copied_files = 0
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
            if "__pycache__" in relative_path.parts or member.name.endswith(".pyc"):
                continue
            if member.issym() or member.islnk() or member.isdev():
                raise ValueError(f"archive contains a disallowed special entry: {member.name}")
            target = (stage_dir / relative_path).resolve()
            if stage_dir not in target.parents and target != stage_dir:
                raise ValueError(f"archive member escapes stage directory: {member.name}")
            extracted_roots.add(parts[0])
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ValueError(f"archive contains an unsupported entry: {member.name}")
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"archive entry could not be read: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            copied_files += 1
    missing = wanted_roots - extracted_roots
    if missing:
        raise ValueError(f"Drone archive is missing required directories: {', '.join(sorted(missing))}")
    required = (
        stage_dir / "app" / "main.py",
        stage_dir / "app" / "drone_api.py",
        stage_dir / "app" / "service_bootstrap.sh",
        stage_dir / "app" / "VERSION",
    )
    for path in required:
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError(f"Drone archive is missing required file: {path.relative_to(stage_dir)}")
    return copied_files


def _activate_release(work_dir: Path, staged_release: Path, release_name: str) -> Path:
    releases_dir = work_dir / ".releases"
    releases_dir.mkdir(parents=True, exist_ok=True)
    final_release = releases_dir / release_name
    if final_release.exists():
        shutil.rmtree(staged_release)
    else:
        staged_release.replace(final_release)

    current_link = work_dir / "current"
    temporary_current = work_dir / ".current.new"
    temporary_current.unlink(missing_ok=True)
    temporary_current.symlink_to(Path(".releases") / release_name, target_is_directory=True)

    app_path = work_dir / "app"
    content_path = work_dir / "content"
    links_ready = app_path.is_symlink() and content_path.is_symlink()
    if links_ready:
        os.replace(temporary_current, current_link)
        return final_release

    legacy_release = releases_dir / f"pre-signed-update-{int(time.time())}"
    legacy_release.mkdir(parents=True, exist_ok=False)
    temporary_app = work_dir / ".app.new"
    temporary_content = work_dir / ".content.new"
    temporary_app.unlink(missing_ok=True)
    temporary_content.unlink(missing_ok=True)
    temporary_app.symlink_to(Path("current") / "app", target_is_directory=True)
    temporary_content.symlink_to(Path("current") / "content", target_is_directory=True)
    moved = []
    try:
        for name, path in (("app", app_path), ("content", content_path)):
            if path.exists() or path.is_symlink():
                destination = legacy_release / name
                path.replace(destination)
                moved.append((path, destination))
        os.replace(temporary_current, current_link)
        os.replace(temporary_app, app_path)
        os.replace(temporary_content, content_path)
    except Exception:
        temporary_app.unlink(missing_ok=True)
        temporary_content.unlink(missing_ok=True)
        app_path.unlink(missing_ok=True)
        content_path.unlink(missing_ok=True)
        for original, saved in reversed(moved):
            if saved.exists() or saved.is_symlink():
                saved.replace(original)
        raise
    return final_release


def _download_latest_drone_app_unlocked(settings: Settings) -> dict:
    work_dir = _drone_work_dir(settings)
    work_dir.mkdir(parents=True, exist_ok=True)
    started_at = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="drone-update-", dir=str(work_dir)) as temp_dir_name:
        temp_dir = Path(temp_dir_name).resolve()
        manifest = _verified_release_manifest(temp_dir)
        version = manifest["version"]
        archive_url = os.environ.get(
            "DRONE_APP_ARCHIVE_URL",
            f"{DRONE_RELEASE_DOWNLOAD_ROOT}/{version}/drone-app.tar.gz",
        )
        archive_path = temp_dir / "drone-app.tar.gz"
        _download_file(archive_url, archive_path, max_bytes=DRONE_UPDATE_ARCHIVE_MAX_BYTES)
        _verify_release_asset(archive_path, manifest["assets"]["drone-app.tar.gz"])
        stage_dir = temp_dir / "release"
        stage_dir.mkdir()
        copied_files = _extract_release_archive(archive_path, stage_dir)
        archive_version = (stage_dir / "app" / "VERSION").read_text(encoding="utf-8").splitlines()[0].strip()
        if archive_version != version:
            raise ValueError(f"Drone archive version {archive_version!r} does not match signed manifest {version!r}")
        release_name = f"{version.lstrip('v')}-{manifest['assets']['drone-app.tar.gz']['sha256'][:12]}"
        activated_release = _activate_release(work_dir, stage_dir, release_name)
    return {
        "status": "downloaded",
        "archive_url": archive_url,
        "version": version,
        "work_dir": str(work_dir),
        "release_dir": str(activated_release),
        "copied_files": copied_files,
        "duration_ms": int((time.monotonic() - started_at) * 1000),
        "restart_required": True,
    }


def _download_latest_drone_app(settings: Settings) -> dict:
    with _DRONE_UPDATE_LOCK:
        return _download_latest_drone_app_unlocked(settings)


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
