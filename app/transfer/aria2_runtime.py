"""aria2c runtime for torrent downloads: locate/install the binary, run the daemon.

The Drone drives torrents through a locally spawned ``aria2c --enable-rpc``
daemon (JSON-RPC over localhost, secret-token gated, bound to 127.0.0.1 only).
Batocera images do not ship aria2c, so this module can also install a fully
static musl build from the abcfy2/aria2-static-build GitHub releases into the
Drone work dir (``<userdata>/system/drone-app/bin/aria2c``) -- the same
outbound-only HTTPS fetch pattern the Drone self-update already uses.

Pure stdlib. The daemon is tied to the app's lifetime via
``--stop-with-process=<pid>`` so a Drone restart never leaks an aria2c process.
"""

import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen

try:
    from ..common.self_update import _drone_work_dir
    from ..common.settings import Settings
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.self_update import _drone_work_dir  # type: ignore
    from common.settings import Settings  # type: ignore

ARIA2_DOWNLOAD_VERSION = os.environ.get("ARIA2_DOWNLOAD_VERSION", "1.37.0")
ARIA2_DOWNLOAD_URL_TEMPLATE = (
    "https://github.com/abcfy2/aria2-static-build/releases/download/{version}/{asset}"
)
ARIA2_DOWNLOAD_MAX_BYTES = 64 * 1024 * 1024
ARIA2_RPC_TIMEOUT_SECONDS = 5.0
ARIA2_STARTUP_TIMEOUT_SECONDS = 10.0

# Fully static musl builds, one asset per architecture (single `aria2c` binary
# inside a zip). Keyed by normalized `platform.machine()` values.
ARIA2_STATIC_ASSETS = {
    "x86_64": "aria2-x86_64-linux-musl_static.zip",
    "amd64": "aria2-x86_64-linux-musl_static.zip",
    "aarch64": "aria2-aarch64-linux-musl_static.zip",
    "arm64": "aria2-aarch64-linux-musl_static.zip",
    "armv7l": "aria2-armv7-linux-musleabihf_static.zip",
    "armv7": "aria2-armv7-linux-musleabihf_static.zip",
    # armv6 boards (original Pi/Zero) run the soft-float arm build.
    "armv6l": "aria2-arm-linux-musleabi_static.zip",
    "arm": "aria2-arm-linux-musleabi_static.zip",
    "i686": "aria2-i686-linux-musl_static.zip",
    "i586": "aria2-i686-linux-musl_static.zip",
    "i386": "aria2-i686-linux-musl_static.zip",
}

_VERSION_CACHE: dict = {}


class Aria2RpcError(RuntimeError):
    """An aria2 JSON-RPC call failed (transport error or error response)."""


def managed_aria2c_path(settings: Settings) -> Path:
    return _drone_work_dir(settings) / "bin" / "aria2c"


def find_aria2c(settings: Settings) -> Optional[dict]:
    """Return ``{"path", "source"}`` for the usable aria2c binary, if any.

    A system-wide install (PATH) wins over the Drone-managed copy so a future
    Batocera image that ships aria2c is preferred automatically.
    """
    system_path = shutil.which("aria2c")
    if system_path:
        return {"path": system_path, "source": "system"}
    managed = managed_aria2c_path(settings)
    if managed.is_file() and os.access(managed, os.X_OK):
        return {"path": str(managed), "source": "managed"}
    return None


def aria2c_version(binary_path: str) -> Optional[str]:
    """Best-effort ``aria2c --version`` parse, cached per (path, mtime)."""
    try:
        mtime = os.stat(binary_path).st_mtime
    except OSError:
        return None
    cached = _VERSION_CACHE.get(binary_path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        result = subprocess.run(
            [binary_path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"aria2 version ([0-9][0-9.]*)", result.stdout or "")
    version = match.group(1) if match else None
    if version:
        _VERSION_CACHE[binary_path] = (mtime, version)
    return version


def aria2_install_state(settings: Settings) -> dict:
    found = find_aria2c(settings)
    if not found:
        return {"installed": False, "path": None, "source": None, "version": None}
    return {
        "installed": True,
        "path": found["path"],
        "source": found["source"],
        "version": aria2c_version(found["path"]),
    }


def _asset_for_machine(machine: str) -> str:
    normalized = str(machine or "").strip().lower()
    asset = ARIA2_STATIC_ASSETS.get(normalized)
    if not asset:
        raise ValueError(f"no static aria2c build is available for this architecture: {machine!r}")
    return asset


def _download_bytes(url: str, timeout: float = 180.0, max_bytes: int = ARIA2_DOWNLOAD_MAX_BYTES) -> bytes:
    request = Request(url, headers={"User-Agent": "batocera-drone-torrents"})
    chunks = []
    total = 0
    with urlopen(request, timeout=timeout) as response:
        while True:
            chunk = response.read(1024 * 256)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"aria2c download exceeded {max_bytes} bytes: {url}")
            chunks.append(chunk)
    if not total:
        raise ValueError(f"aria2c download was empty: {url}")
    return b"".join(chunks)


def _extract_aria2c_from_zip(archive_bytes: bytes) -> bytes:
    with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            if Path(info.filename).name == "aria2c":
                return archive.read(info)
    raise ValueError("downloaded archive did not contain an aria2c binary")


def install_aria2(settings: Settings) -> dict:
    """Download the static aria2c build for this machine into the work dir."""
    override_url = (os.environ.get("ARIA2_DOWNLOAD_URL") or "").strip()
    if override_url:
        url = override_url
    else:
        asset = _asset_for_machine(platform.machine())
        url = ARIA2_DOWNLOAD_URL_TEMPLATE.format(version=ARIA2_DOWNLOAD_VERSION, asset=asset)
    started = time.monotonic()
    binary_bytes = _extract_aria2c_from_zip(_download_bytes(url))
    target = managed_aria2c_path(settings)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name("aria2c.tmp")
    staged.write_bytes(binary_bytes)
    staged.chmod(0o755)
    version = aria2c_version(str(staged))
    if not version:
        try:
            staged.unlink()
        except OSError:
            pass
        raise ValueError("downloaded aria2c binary failed to run on this machine (--version check)")
    staged.replace(target)
    _VERSION_CACHE.pop(str(staged), None)
    _VERSION_CACHE.pop(str(target), None)
    return {
        "status": "installed",
        "path": str(target),
        "version": version,
        "source_url": url,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


class Aria2Rpc:
    """Minimal JSON-RPC 2.0 client for a localhost aria2c daemon."""

    def __init__(self, port: int, secret: str) -> None:
        self._url = f"http://127.0.0.1:{port}/jsonrpc"
        self._secret = secret

    def call(self, method: str, params: Optional[list] = None, timeout: float = ARIA2_RPC_TIMEOUT_SECONDS):
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "drone",
                "method": method,
                "params": [f"token:{self._secret}", *(params or [])],
            }
        ).encode("utf-8")
        request = Request(self._url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except Aria2RpcError:
            raise
        except Exception as error:
            raise Aria2RpcError(f"aria2 RPC {method} failed: {error}") from error
        if isinstance(payload, dict) and payload.get("error"):
            message = payload["error"].get("message") or str(payload["error"])
            raise Aria2RpcError(str(message))
        return payload.get("result") if isinstance(payload, dict) else None


def _free_localhost_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Aria2Daemon:
    """One app-lifetime aria2c daemon: spawn, health-check, expose its RPC."""

    def __init__(self, binary_path: str, download_dir: Path, log_file: Optional[Path] = None) -> None:
        self.binary_path = binary_path
        self.download_dir = download_dir
        self.log_file = log_file
        self.process: Optional[subprocess.Popen] = None
        self.rpc: Optional[Aria2Rpc] = None
        self.port: Optional[int] = None
        self.last_error = ""

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None and self.rpc is not None

    def start(self) -> bool:
        if self.running:
            return True
        self.stop()
        port = _free_localhost_port()
        secret = secrets.token_hex(16)
        try:
            self.download_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        args = [
            self.binary_path,
            "--enable-rpc",
            "--rpc-listen-all=false",
            f"--rpc-listen-port={port}",
            f"--rpc-secret={secret}",
            f"--stop-with-process={os.getpid()}",
            f"--dir={self.download_dir}",
            # The TorrentManager runs its own queue (paused adds + slot
            # scheduler) so force-start can bypass the concurrency limit;
            # aria2's own limit just needs to stay out of the way.
            "--max-concurrent-downloads=64",
            "--continue=true",
            "--auto-save-interval=30",
            "--bt-save-metadata=false",
            # Never mirror RPC-uploaded .torrent metadata into --dir as
            # <infohash>.torrent: the watch folder IS the download folder, so
            # the scanner would re-register aria2's own copy as a new torrent.
            "--rpc-save-upload-metadata=false",
            "--summary-interval=0",
            "--quiet=true",
        ]
        if self.log_file is not None:
            try:
                self.log_file.parent.mkdir(parents=True, exist_ok=True)
                args.extend([f"--log={self.log_file}", "--log-level=warn"])
            except OSError:
                pass
        try:
            self.process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            self.last_error = f"failed to launch aria2c: {error}"
            self.process = None
            return False
        rpc = Aria2Rpc(port, secret)
        deadline = time.monotonic() + ARIA2_STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.last_error = f"aria2c exited during startup (code {self.process.returncode})"
                self.process = None
                return False
            try:
                rpc.call("aria2.getVersion", timeout=1.0)
            except Aria2RpcError:
                time.sleep(0.2)
                continue
            self.rpc = rpc
            self.port = port
            self.last_error = ""
            print(f"aria2c daemon started: pid={self.process.pid} rpc_port={port}", file=sys.stdout, flush=True)
            return True
        self.last_error = "aria2c RPC did not become ready in time"
        self.stop()
        return False

    def stop(self) -> None:
        process = self.process
        self.process = None
        self.rpc = None
        self.port = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        except OSError:
            pass
