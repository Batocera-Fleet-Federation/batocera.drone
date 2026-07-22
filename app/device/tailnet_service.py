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

Drones are often deployed unattended (no one able to paste a fresh key when a
node's Tailscale key eventually expires), so enroll/rotate/startup also make a
best-effort, opt-in call to Tailscale's own admin API to disable key expiry
for this device -- see disable_key_expiry() and _maybe_disable_key_expiry().
That call needs an OAuth client (settings.tailscale_oauth_client_id/_secret);
without one configured, this is a silent no-op and nothing changes from
before -- a human still has to paste a key, and it can still expire.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    from ..common.http_errors import _format_http_error
    from ..transport.tailnet import get_tailnet_ip
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.http_errors import _format_http_error  # type: ignore
    from transport.tailnet import get_tailnet_ip  # type: ignore

TAILSCALE_DIR = Path("/userdata/system/tailscale")
TAILSCALE_CLI = TAILSCALE_DIR / "bin" / "tailscale"
TAILNET_SERVICE = Path("/userdata/system/services/DRONE_TAILNET")
TAILSCALE_SOCKET = "/var/run/tailscale/tailscaled.sock"

# Tailscale's admin API, used only to disable key expiry for this device (see
# disable_key_expiry() below) -- never for anything else, and only when an
# OAuth client is configured (opt-in).
TAILSCALE_API_BASE = "https://api.tailscale.com/api/v2"
TAILSCALE_API_TIMEOUT_SECONDS = 15.0


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
    """Installed / running / enrolled details for admin diagnostics and Swarm."""
    status = {
        "installed": TAILSCALE_CLI.exists(),
        "running": False,
        "enrolled": False,
        "tailnet_ip": get_tailnet_ip() or "",
        "hostname": socket.gethostname().lower(),
        "backend_state": "",
        "version": "",
        "dns_name": "",
        "tailnet_name": "",
        "magic_dns_suffix": "",
        "relay": "",
        "health": [],
        "peers": [],
        # This device's own Tailscale node ID (Self.ID), needed to target the
        # admin API's per-device endpoints (see disable_key_expiry()) -- not to
        # be confused with the Drone's own peer-identity device_id elsewhere.
        "tailscale_device_id": "",
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
    status["version"] = str(payload.get("Version") or "")
    # "Running" (connected) and "Starting" (has a node key, coming up) both
    # mean the device is enrolled; "NeedsLogin"/"NoState" mean it is not.
    status["enrolled"] = backend_state in {"Running", "Starting"}
    self_info = payload.get("Self") if isinstance(payload.get("Self"), dict) else {}
    own_address = _first_address(self_info.get("TailscaleIPs") or [])
    if own_address:
        status["tailnet_ip"] = own_address
    status["dns_name"] = str(self_info.get("DNSName") or "").strip().rstrip(".")
    status["relay"] = str(self_info.get("Relay") or "").strip()
    status["tailscale_device_id"] = str(self_info.get("ID") or "")
    current_tailnet = payload.get("CurrentTailnet") if isinstance(payload.get("CurrentTailnet"), dict) else {}
    status["tailnet_name"] = str(current_tailnet.get("Name") or "").strip()
    status["magic_dns_suffix"] = str(
        current_tailnet.get("MagicDNSSuffix") or payload.get("MagicDNSSuffix") or ""
    ).strip().rstrip(".")
    raw_health = payload.get("Health")
    if isinstance(raw_health, list):
        status["health"] = [str(item).strip() for item in raw_health if str(item or "").strip()]
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


def ensure_tailnet_networking(settings: Optional[Any] = None) -> None:
    """Apply the Batocera-compatible netfilter preference, best effort.

    Batocera's kernel omits the iptables filter modules expected by Tailscale.
    Re-applying this at Drone startup also repairs already-enrolled nodes whose
    persisted preference predates the installer/enrollment fix. Start the
    bundled daemon first when it is installed but no longer running; its
    service is a launcher rather than a long-lived supervisor, so a stale PID
    must not leave Tailnet recovery dependent on a reboot.

    Also (opt-in, see _maybe_disable_key_expiry) makes sure an already-enrolled
    node -- e.g. one hands-free-enrolled by the installer's TS_AUTHKEY before
    this Python process ever ran -- has key expiry disabled, so it doesn't
    strand itself at NeedsLogin months later with no one able to fix it.
    """
    if not TAILSCALE_CLI.exists():
        return
    if _start_daemon_if_needed():
        return
    try:
        _run_cli(["set", "--netfilter-mode=off"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return
    _maybe_disable_key_expiry(settings)


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


def _tailscale_api_request(
    url: str,
    *,
    method: str = "GET",
    data: Optional[bytes] = None,
    headers: Optional[dict] = None,
    timeout: float = TAILSCALE_API_TIMEOUT_SECONDS,
) -> dict:
    """Minimal stdlib JSON client for Tailscale's admin API. Only ever used for
    the OAuth token exchange and the device key-expiry update below."""
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def _tailscale_oauth_access_token(client_id: str, client_secret: str) -> str:
    body = urllib.parse.urlencode(
        {"client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials"}
    ).encode("utf-8")
    payload = _tailscale_api_request(
        f"{TAILSCALE_API_BASE}/oauth/token",
        method="POST",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    token = str(payload.get("access_token") or "")
    if not token:
        raise RuntimeError("Tailscale OAuth token exchange did not return an access token")
    return token


def disable_key_expiry(client_id: str, client_secret: str) -> dict:
    """Ask the Tailscale admin API to disable key expiry for this device.

    This is Tailscale's own documented approach for servers/headless nodes
    that can't do an interactive re-login when their node key eventually
    expires (see https://tailscale.com/kb/1028/key-expiry) -- it makes this
    device's tailnet session permanent until someone manually re-enables
    expiry or removes the device from the admin console, rather than an
    unattended Drone silently falling back to NeedsLogin with no one able to
    paste a fresh auth key.

    ``client_id``/``client_secret`` are an OAuth client from the Tailscale
    admin console (Settings -> OAuth clients). Unlike the enrollment auth key
    (single-use, spent immediately by `tailscale up`), this credential is held
    long-term by every Drone it's configured on -- scope it to just the
    `devices:core:write` permission and tag it to this fleet's devices so a
    compromised Drone can't use it to touch anything outside that scope.

    Raises RuntimeError (never containing the secret) on failure; callers
    decide whether that's fatal -- see _maybe_disable_key_expiry, which treats
    it as best-effort and retries on the next enroll/rotate/restart.
    """
    device_id = tailnet_status().get("tailscale_device_id") or ""
    if not device_id:
        raise RuntimeError("could not determine this device's Tailscale ID (is it enrolled?)")
    token = _tailscale_oauth_access_token(client_id, client_secret)
    _tailscale_api_request(
        f"{TAILSCALE_API_BASE}/device/{urllib.parse.quote(device_id, safe='')}/key",
        method="POST",
        data=json.dumps({"keyExpiryDisabled": True}).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    return {"device_id": device_id, "key_expiry_disabled": True}


def _maybe_disable_key_expiry(settings: Optional[Any]) -> None:
    """Best-effort, silent no-op unless a Tailscale OAuth client is configured
    (opt-in) and this device is actually enrolled. Never raises -- a failure
    here must never break enrollment/rotation/startup, which already
    succeeded on the tailnet side by the time this runs."""
    if settings is None:
        return
    client_id = getattr(settings, "tailscale_oauth_client_id", None)
    client_secret = getattr(settings, "tailscale_oauth_client_secret", None)
    if not client_id or not client_secret:
        return
    if not tailnet_status().get("enrolled"):
        return
    try:
        disable_key_expiry(client_id, client_secret)
    except Exception as error:  # noqa: BLE001 - best-effort, log and move on
        print(
            f"Tailnet key-expiry auto-disable failed (will retry next enroll/restart): {_format_http_error(error)}",
            file=sys.stderr,
            flush=True,
        )


def tailnet_enroll(auth_key: str, settings: Optional[Any] = None) -> dict:
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
    _maybe_disable_key_expiry(settings)
    return tailnet_status()


def tailnet_rotate_auth_key(auth_key: str, settings: Optional[Any] = None) -> dict:
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
        return tailnet_enroll(key, settings)
    except RuntimeError as error:
        raise RuntimeError(
            "Tailnet auth token rotation disconnected this Drone but re-enrollment failed: " + str(error).replace(key, "[redacted]")
        ) from error
