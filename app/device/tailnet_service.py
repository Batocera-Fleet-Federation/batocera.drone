"""Tailnet (Tailscale) device operations: status + UI-driven enrollment.

The installer (scripts/batocera_install.sh) puts the static binaries under
/userdata/system/tailscale and a DRONE_TAILNET service next to DRONE_SERVER;
this module is the web UI's way to finish the job without touching a shell:
report whether the mesh is installed/running/enrolled, list its online peers,
and enroll with an auth key pasted into the Swarm page (instead of a TS_AUTHKEY
env var at install time). Stdlib-only, shells out to the tailscale CLI like
the other device controls shell out to batocera tools.

The auth key is a secret: it is passed to the CLI and never logged or echoed
back in any error message (tailscale's own stderr does not repeat it).
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
from pathlib import Path
from typing import Iterable, Optional

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


def _first_address(addresses: Iterable[object], *, ipv4: bool = True) -> str:
    for address in addresses:
        text = str(address or "").strip()
        if text and ((":" not in text) if ipv4 else (":" in text)):
            return text
    return ""


def _tailnet_peers(payload: dict) -> list[dict]:
    """Return online devices from ``tailscale status --json`` in UI-safe form."""
    raw_peers = payload.get("Peer")
    if isinstance(raw_peers, dict):
        entries = raw_peers.items()
    elif isinstance(raw_peers, list):
        entries = ((str(index), peer) for index, peer in enumerate(raw_peers))
    else:
        entries = ()
    peers = []
    for peer_key, raw in entries:
        if not isinstance(raw, dict) or raw.get("Online") is not True:
            continue
        addresses = raw.get("TailscaleIPs") or []
        tailnet_ip = _first_address(addresses) or _first_address(addresses, ipv4=False)
        if not tailnet_ip:
            continue
        dns_name = str(raw.get("DNSName") or "").strip().rstrip(".")
        hostname = str(raw.get("HostName") or dns_name.split(".", 1)[0] or tailnet_ip).strip()
        peers.append(
            {
                "tailnet_id": str(raw.get("ID") or peer_key or tailnet_ip),
                "name": hostname,
                "hostname": hostname,
                "dns_name": dns_name,
                "tailnet_ip": tailnet_ip,
                "addresses": [str(value) for value in addresses if str(value or "").strip()],
                "last_seen": str(raw.get("LastSeen") or ""),
                "os": str(raw.get("OS") or ""),
                "online": True,
            }
        )
    return sorted(peers, key=lambda peer: (str(peer.get("name") or "").lower(), str(peer.get("tailnet_ip") or "")))


def tailnet_status() -> dict:
    """Installed / running / enrolled / tailnet_ip, for the Swarm page card."""
    status = {
        "installed": TAILSCALE_CLI.exists(),
        "running": False,
        "enrolled": False,
        "tailnet_ip": get_tailnet_ip() or "",
        "hostname": socket.gethostname().lower(),
        "backend_state": "",
        "peers": [],
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
    own_address = _first_address(self_info.get("TailscaleIPs") or [])
    if own_address:
        status["tailnet_ip"] = own_address
    status["peers"] = _tailnet_peers(payload)
    return status


def tailnet_peer_ips() -> set[str]:
    """Addresses currently authenticated as online peers by local tailscaled."""
    status = tailnet_status()
    if not status.get("enrolled"):
        return set()
    return {
        str(address).strip()
        for peer in status.get("peers") or []
        if isinstance(peer, dict)
        for address in peer.get("addresses") or [peer.get("tailnet_ip")]
        if str(address or "").strip()
    }


def ensure_tailnet_networking() -> None:
    """Apply the Batocera-compatible netfilter preference, best effort.

    Batocera's kernel omits the iptables filter modules expected by Tailscale.
    Re-applying this at Drone startup also repairs already-enrolled nodes whose
    persisted preference predates the installer/enrollment fix. Start the
    bundled daemon first when it is installed but no longer running; its
    service is a launcher rather than a long-lived supervisor, so a stale PID
    must not leave Tailnet recovery dependent on a reboot.
    """
    if not TAILSCALE_CLI.exists():
        return
    if _start_daemon_if_needed():
        return
    try:
        _run_cli(["set", "--netfilter-mode=off"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return


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
                # Batocera's kernel does not provide the iptables filter
                # modules Tailscale tries to manage by default. Its base image
                # also has no host firewall to configure, so leave filtering
                # off and let tailscaled use the existing tailscale0 routes.
                "--netfilter-mode=off",
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


def tailnet_rotate_auth_key(auth_key: str) -> dict:
    """Re-authenticate this Drone with a replacement Tailscale auth key.

    Tailscale auth keys are enrollment credentials rather than durable session
    tokens, so changing the credential for an already-enrolled node requires a
    logout followed by a fresh ``up``. The caller must confirm that brief
    disconnect before invoking this operation.
    """
    key = str(auth_key or "").strip()
    if not key:
        raise ValueError("auth key is required")
    current = tailnet_status()
    if not current.get("installed"):
        raise RuntimeError(
            "Tailscale is not installed on this Drone. Re-run the Drone installer "
            "(batocera_install.sh) once to add it, then try again."
        )
    if not current.get("enrolled"):
        raise RuntimeError("Tailnet is not connected; use Connect with the new auth key instead.")
    try:
        proc = _run_cli(["logout"], timeout=30)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Tailnet auth token rotation timed out while disconnecting.") from error
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"Tailnet auth token rotation could not disconnect: {error}") from error
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        message = detail[-1] if detail else f"exit code {proc.returncode}"
        raise RuntimeError(f"Tailnet auth token rotation could not disconnect: {message.replace(key, '[redacted]')}")
    try:
        return tailnet_enroll(key)
    except RuntimeError as error:
        raise RuntimeError(
            "Tailnet auth token rotation disconnected this Drone but re-enrollment failed: " + str(error).replace(key, "[redacted]")
        ) from error
