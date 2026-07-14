"""Tailnet (Tailscale) device operations: status + UI-driven enrollment.

The installer (scripts/batocera_install.sh) puts the static binaries under
/userdata/system/tailscale and a DRONE_TAILNET service next to DRONE_SERVER;
this module is the web UI's way to finish the job without touching a shell:
report whether the mesh is installed/running/enrolled, and enroll with an
auth key pasted into the Swarm page (instead of a TS_AUTHKEY env var at
install time). Stdlib-only, shells out to the tailscale CLI like the other
device controls shell out to batocera tools.

The auth key is a secret: it is passed to the CLI and never logged or echoed
back in any error message (tailscale's own stderr does not repeat it).
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

try:
    from ..transport.tailnet import get_tailnet_ip
except ImportError:  # pragma: no cover - direct script execution fallback
    from transport.tailnet import get_tailnet_ip  # type: ignore

TAILSCALE_DIR = Path("/userdata/system/tailscale")
TAILSCALE_CLI = TAILSCALE_DIR / "bin" / "tailscale"
TAILNET_SERVICE = Path("/userdata/system/services/DRONE_TAILNET")
TAILSCALE_SOCKET = "/var/run/tailscale/tailscaled.sock"


def _run_cli(args: list, timeout: float) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [str(TAILSCALE_CLI), f"--socket={TAILSCALE_SOCKET}", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def tailnet_status() -> dict:
    """Installed / running / enrolled / tailnet_ip, for the Swarm page card."""
    status = {
        "installed": TAILSCALE_CLI.exists(),
        "running": False,
        "enrolled": False,
        "tailnet_ip": get_tailnet_ip() or "",
        "hostname": socket.gethostname().lower(),
        "backend_state": "",
    }
    if not status["installed"]:
        return status
    try:
        proc = _run_cli(["status", "--json"], timeout=5)
    except (OSError, subprocess.SubprocessError):
        return status
    if proc.returncode != 0:
        # tailscaled itself is not answering on the socket.
        return status
    status["running"] = True
    try:
        payload = json.loads(proc.stdout or "{}")
    except ValueError:
        payload = {}
    backend_state = str(payload.get("BackendState") or "")
    status["backend_state"] = backend_state
    # "Running" (connected) and "Starting" (has a node key, coming up) both
    # mean the device is enrolled; "NeedsLogin"/"NoState" mean it is not.
    status["enrolled"] = backend_state in {"Running", "Starting"}
    self_info = payload.get("Self") if isinstance(payload.get("Self"), dict) else {}
    for address in self_info.get("TailscaleIPs") or []:
        text = str(address or "").strip()
        if text and ":" not in text:
            status["tailnet_ip"] = text
            break
    return status


def _start_daemon_if_needed() -> Optional[str]:
    """Best-effort DRONE_TAILNET service start; returns an error string or None."""
    try:
        if _run_cli(["status", "--json"], timeout=5).returncode == 0:
            return None
    except (OSError, subprocess.SubprocessError):
        pass
    if not TAILNET_SERVICE.exists():
        return (
            "The DRONE_TAILNET service is not installed. Re-run the Drone installer "
            "(batocera_install.sh) once to add the mesh daemon, then try again."
        )
    try:
        subprocess.run(
            ["sh", str(TAILNET_SERVICE), "start"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return f"Could not start the tailnet daemon: {error}"
    # Give the daemon a few seconds to open its control socket.
    for _ in range(5):
        try:
            if _run_cli(["status", "--json"], timeout=5).returncode == 0:
                return None
        except (OSError, subprocess.SubprocessError):
            pass
        time.sleep(1)
    return "The tailnet daemon did not come up; check /userdata/system/logs/tailscaled.log."


def tailnet_enroll(auth_key: str) -> dict:
    """Enroll this device in the tailnet with an auth key from the admin console.

    Raises ValueError for bad input and RuntimeError with a user-facing message
    (never containing the key) when enrollment fails.
    """
    key = str(auth_key or "").strip()
    if not key:
        raise ValueError("auth key is required")
    if not TAILSCALE_CLI.exists():
        raise RuntimeError(
            "Tailscale is not installed on this Drone. Re-run the Drone installer "
            "(batocera_install.sh) once to add it, then paste the key again."
        )
    daemon_error = _start_daemon_if_needed()
    if daemon_error:
        raise RuntimeError(daemon_error)
    hostname = socket.gethostname().lower()
    try:
        # --accept-dns=false keeps Batocera's resolv.conf untouched; the Drone
        # integration works on raw 100.x addresses, not MagicDNS names.
        proc = _run_cli(
            [
                "up",
                f"--authkey={key}",
                f"--hostname={hostname}",
                "--accept-dns=false",
                "--timeout=45s",
            ],
            timeout=60,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Tailnet enrollment timed out; check the key and try again.") from error
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"Tailnet enrollment could not run: {error}") from error
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(
            "Tailnet enrollment failed: " + (detail[-1] if detail else f"exit code {proc.returncode}")
        )
    return tailnet_status()
