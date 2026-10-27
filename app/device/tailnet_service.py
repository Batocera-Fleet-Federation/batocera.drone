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
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Iterable, Optional

try:
    from ..common.http_errors import _format_http_error
    from ..common.poller_interval import poller_interval_from_env
    from ..common.runtime_state import poller_generation, poller_retired
    from ..common.settings import Settings
    from ..storage.state_store import database_path as _state_database_path
    from ..storage.state_store import load_payload as _load_state_payload
    from ..storage.state_store import save_payload as _save_state_payload
    from ..transport.tailnet import get_tailnet_ip
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.http_errors import _format_http_error  # type: ignore
    from common.poller_interval import poller_interval_from_env  # type: ignore
    from common.runtime_state import poller_generation, poller_retired  # type: ignore
    from common.settings import Settings  # type: ignore
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import load_payload as _load_state_payload  # type: ignore
    from storage.state_store import save_payload as _save_state_payload  # type: ignore
    from transport.tailnet import get_tailnet_ip  # type: ignore

# DRONE_TAILSCALE_DIR / DRONE_TAILSCALE_SOCKET are escape hatches for
# tests/ops, matching DRONE_VPN_DIR's precedent in vpn_manager.py -- never
# exposed in the admin UI. On a real device TAILSCALE_DIR is always
# /userdata/system/tailscale (where batocera_install.sh vendors the static
# binaries) and TAILSCALE_SOCKET is always the daemon socket path the
# installer's tailscaled uses. The overrides exist purely so local dev
# (e.g. on a Mac, where that /userdata path can't exist) can point at a
# real locally-installed `tailscale` CLI for testing.
#
# DRONE_TAILSCALE_SOCKET defaults to unset -> the real device's fixed
# tailscaled.sock path, same as before. Set it to a real path to point at a
# different daemon socket, or to the *empty string* to omit --socket
# entirely -- required on macOS: the Tailscale.app GUI client doesn't run a
# Linux-style tailscaled with a socket at a fixed path at all (it talks to
# the OS via NetworkExtension), so passing any --socket value makes even a
# running, enrolled macOS Tailscale answer "failed to connect to local
# Tailscale service" -- confirmed by running the exact CLI invocation this
# module makes, with and without --socket, against a real local install.
TAILSCALE_DIR = Path(os.environ.get("DRONE_TAILSCALE_DIR") or "/userdata/system/tailscale")
TAILSCALE_CLI = TAILSCALE_DIR / "bin" / "tailscale"
TAILNET_SERVICE = Path("/userdata/system/services/DRONE_TAILNET")
_TAILSCALE_SOCKET_ENV = os.environ.get("DRONE_TAILSCALE_SOCKET")
TAILSCALE_SOCKET = "/var/run/tailscale/tailscaled.sock" if _TAILSCALE_SOCKET_ENV is None else _TAILSCALE_SOCKET_ENV

# Tailscale's admin API, used only to disable key expiry for this device (see
# disable_key_expiry() below) -- never for anything else, and only when an
# OAuth client is configured (opt-in).
TAILSCALE_API_BASE = "https://api.tailscale.com/api/v2"
TAILSCALE_API_TIMEOUT_SECONDS = 15.0

_NETWORK_REPAIR_LOCK = Lock()
_NETWORK_REPAIR_LAST = float("-inf")


def _kernel_tailnet_failure(payload: dict) -> Optional[str]:
    """Inspect the kernel data path; daemon pings bypass this path entirely."""
    if payload.get("BackendState") != "Running":
        return None
    # Only supervise our kernel-mode daemon, never userspace networking or an
    # intentionally logged-out node. Missing/unreadable process state is unknown.
    try:
        pid = int(Path("/tmp/drone-tailscaled.pid").read_text().strip())
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except (OSError, ValueError):
        return None
    if b"--tun=tailscale0" not in args:
        return None
    addresses = payload.get("TailscaleIPs") or []
    if not addresses:
        return None
    result = subprocess.run(["ip", "-j", "address", "show", "dev", "tailscale0"],
                            capture_output=True, text=True, timeout=5)
    if result.returncode:
        return "tailscale0 is missing"
    links = json.loads(result.stdout)
    if not links or "UP" not in links[0].get("flags", []):
        return "tailscale0 is down"
    assigned = {row.get("local") for row in links[0].get("addr_info", [])}
    if not set(addresses).issubset(assigned):
        return "tailscale0 has lost its assigned address"
    for peer in (payload.get("Peer") or {}).values():
        # Offline peers can retain addresses in status while Linux quite
        # correctly resolves them through the default route. They do not prove
        # that the live tailnet data path is unhealthy and must not create a
        # periodic restart loop.
        if peer.get("Online") is not True:
            continue
        for address in peer.get("TailscaleIPs") or []:
            if ":" in address:
                continue
            result = subprocess.run(["ip", "-j", "route", "get", address],
                                    capture_output=True, text=True, timeout=5)
            routes = json.loads(result.stdout) if result.returncode == 0 else []
            if not routes or routes[0].get("dev") != "tailscale0":
                return "a peer route no longer uses tailscale0"
    return None


def _repair_tailnet_data_path() -> None:
    global _NETWORK_REPAIR_LAST
    if not sys.platform.startswith("linux") or not TAILNET_SERVICE.is_file():
        return
    if not _NETWORK_REPAIR_LOCK.acquire(blocking=False):
        return
    try:
        if time.monotonic() - _NETWORK_REPAIR_LAST < 300:
            return
        proc = _run_cli(["status", "--json"], timeout=5)
        if proc.returncode:
            return
        reason = _kernel_tailnet_failure(json.loads(proc.stdout))
        if not reason:
            return
        _NETWORK_REPAIR_LAST = time.monotonic()
        print(f"Tailnet recovery: {reason}; restarting DRONE_TAILNET", flush=True)
        result = subprocess.run(["sh", str(TAILNET_SERVICE), "restart"],
                                capture_output=True, text=True, timeout=45)
        if result.returncode:
            print(f"Tailnet recovery restart failed: exit {result.returncode}", flush=True)
            return
        _run_cli(["set", "--netfilter-mode=off"], timeout=10)
        proc = _run_cli(["status", "--json"], timeout=5)
        remaining = _kernel_tailnet_failure(json.loads(proc.stdout)) if proc.returncode == 0 else "daemon unavailable"
        print(f"Tailnet recovery: {remaining or 'completed'}", flush=True)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Tailnet recovery check failed: {type(error).__name__}: {error}", flush=True)
    finally:
        _NETWORK_REPAIR_LOCK.release()


def _run_cli(
    args: list, timeout: float, extra_env: Optional[dict] = None
) -> "subprocess.CompletedProcess[str]":
    cli_args = [str(TAILSCALE_CLI)]
    if TAILSCALE_SOCKET:
        cli_args.append(f"--socket={TAILSCALE_SOCKET}")
    cli_args.extend(args)
    run_kwargs: dict = {"capture_output": True, "text": True, "timeout": timeout}
    if extra_env:
        # Secrets go here, never in argv (visible in ps and /proc/<pid>/cmdline).
        run_kwargs["env"] = {**os.environ, **extra_env}
    return subprocess.run(cli_args, **run_kwargs)


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


def _installer_script() -> Path:
    """Path to the bundled app/install_tailscale.sh.

    Resolved from this module rather than a fixed /userdata path so the
    versioned bundle's own copy always wins -- the point of shipping the
    installer inside the app bundle is that it updates with every release.
    """
    override = os.environ.get("DRONE_TAILSCALE_INSTALLER")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "install_tailscale.sh"


def tailscale_install_status() -> dict:
    """Installed / running / version for the Controls + Swarm install UI.

    Deliberately cheap and offline: it never reaches out to
    pkgs.tailscale.com, because this runs on every Controls page render.
    Discovering the latest release is the install/update action's job.
    """
    installer = _installer_script()
    payload = {
        "installed": TAILSCALE_CLI.exists(),
        "version": "",
        "running": False,
        "service_installed": TAILNET_SERVICE.exists(),
        "installer_available": installer.is_file(),
        "install_dir": str(TAILSCALE_DIR),
    }
    if not payload["installed"] or not payload["installer_available"]:
        return payload
    try:
        proc = subprocess.run(
            ["sh", str(installer), "status"],
            capture_output=True,
            text=True,
            timeout=15,
            env=_installer_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return payload
    for token in (proc.stdout or "").strip().split():
        key, _, value = token.partition("=")
        if key == "version" and value:
            payload["version"] = value
        elif key == "running":
            payload["running"] = value == "1"
    return payload


def _installer_env() -> dict:
    """Pass the module's dev/test path overrides through to the shell script."""
    env = dict(os.environ)
    env["DRONE_TAILSCALE_DIR"] = str(TAILSCALE_DIR)
    env["DRONE_TAILNET_SERVICE"] = str(TAILNET_SERVICE)
    if TAILSCALE_SOCKET:
        env["DRONE_TAILSCALE_SOCKET"] = TAILSCALE_SOCKET
    return env


def install_tailscale(mode: str = "ensure", *, timeout: float = 420.0) -> dict:
    """Run the bundled installer. ``mode`` is ensure | install | update.

    Raises RuntimeError with a user-facing message on failure. The script is
    responsible for staging, rollback and the daemon restart; this is only the
    Python entry point onto it (web button, self-update hook).
    """
    normalized = str(mode or "ensure").strip().lower()
    if normalized not in {"ensure", "install", "update"}:
        raise ValueError("mode must be ensure, install, or update")
    installer = _installer_script()
    if not installer.is_file():
        raise RuntimeError(
            "This Drone's app bundle does not include install_tailscale.sh. "
            "Update the Drone app, then try again."
        )
    try:
        proc = subprocess.run(
            ["sh", str(installer), normalized],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_installer_env(),
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Tailscale install timed out; check this Drone's internet access.") from error
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"Tailscale install could not run: {error}") from error
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        detail = [line for line in output.splitlines() if line.strip()]
        raise RuntimeError(
            "Tailscale install failed: " + (detail[-1] if detail else f"exit code {proc.returncode}")
        )
    return {"status": "ok", "mode": normalized, "log": output, **tailscale_install_status()}


def ensure_tailscale_installed(*, upgrade: bool = True) -> dict:
    """Self-update hook: guarantee Tailscale is present and current.

    Called after every successful Drone app update so a drone that was
    installed before the mesh existed (or with the binaries skipped) repairs
    itself with no manual step. Never raises -- a mesh problem must not be
    able to fail or roll back an app update -- and returns a dict describing
    what happened for the update log.
    """
    try:
        before = tailscale_install_status()
        result = install_tailscale("ensure" if upgrade else "install")
        return {
            "ok": True,
            "previous_version": before.get("version") or "",
            "version": result.get("version") or "",
            "installed_now": not before.get("installed"),
        }
    except Exception as error:  # noqa: BLE001 - best effort by contract
        return {"ok": False, "error": str(error) or error.__class__.__name__}


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
    _repair_tailnet_data_path()


def _tailscaled_answering() -> bool:
    try:
        return _run_cli(["status", "--json"], timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _stop_tailscaled() -> bool:
    """Stop the local daemon via its service script; True once it no longer answers."""
    if not _tailscaled_answering():
        return True
    if TAILNET_SERVICE.exists():
        try:
            subprocess.run(
                ["sh", str(TAILNET_SERVICE), "stop"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    for _ in range(5):
        if not _tailscaled_answering():
            return True
        time.sleep(1)
    return False


def reset_local_node_identity() -> dict:
    """Make a cloned disk stop presenting the source Drone's Tailscale node identity.

    Only the *local* daemon state is removed (daemon stopped first, so it cannot
    rewrite the old node key on exit). Deliberately no ``tailscale logout``: that
    would deregister the node key server-side, and a clone shares that key with the
    original Drone, whose tailnet authorization must stay untouched. The daemon is
    restarted afterwards and comes up unenrolled (NeedsLogin); enrolling requires a
    fresh auth key from an administrator and is never attempted automatically.

    Returns ``{"status": ...}`` where status is one of ``not_installed``,
    ``reset`` (state removed, enrollment required), ``held`` (the daemon could not
    be stopped or the state not fully removed; the node was taken down with
    ``tailscale down`` and the reset is retried on the next start) or ``error``
    (neither a reset nor a confirmed hold was possible).
    """
    if not TAILSCALE_CLI.exists():
        return {"status": "not_installed"}
    state_dir = TAILSCALE_DIR / "state"
    stopped = _stop_tailscaled()
    if not stopped:
        if not hold_local_node():
            # Neither cleared nor offline: the inherited node may still be live.
            return {"status": "error", "detail": "tailscaled could not be stopped or taken down"}
        return {"status": "held", "detail": "tailscaled could not be stopped; node taken down"}
    removed = 0
    try:
        entries = list(state_dir.iterdir()) if state_dir.is_dir() else []
    except OSError:
        entries = []
    for entry in entries:
        try:
            if entry.is_dir() and not entry.is_symlink():
                for child in entry.rglob("*"):
                    if child.is_file() or child.is_symlink():
                        child.unlink()
                removed += 1
            else:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    remaining = []
    try:
        remaining = [entry for entry in state_dir.iterdir() if entry.is_file()] if state_dir.is_dir() else []
    except OSError:
        pass
    if remaining:
        if not hold_local_node():
            return {"status": "error", "detail": "tailscale state could not be removed or taken down"}
        return {"status": "held", "detail": "tailscale state could not be fully removed; node taken down"}
    daemon_error = _start_daemon_if_needed()
    return {
        "status": "reset",
        "state_entries_removed": removed,
        "enrollment_required": True,
        "daemon": daemon_error or "started",
    }


def hold_local_node() -> bool:
    """Take the (possibly inherited) node offline without logging it out of the tailnet."""
    if not TAILSCALE_CLI.exists():
        return False
    try:
        if _run_cli(["down"], timeout=15).returncode == 0:
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    # A failed `down` must not leave a copied node key serving traffic during
    # recovery. Stopping only the local daemon does not revoke the original.
    return _stop_tailscaled()


def release_local_node() -> bool:
    """Bring a node previously taken down by ``hold_local_node`` back up (no re-auth)."""
    if not TAILSCALE_CLI.exists():
        return False
    try:
        return _run_cli(
            [
                "up",
                f"--hostname={socket.gethostname().lower()}",
                "--accept-dns=false",
                "--netfilter-mode=off",
                "--timeout=30s",
            ],
            timeout=45,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _start_daemon_if_needed() -> Optional[str]:
    """Best-effort DRONE_TAILNET service start; returns an error string or None."""
    try:
        if _run_cli(["status", "--json"], timeout=5).returncode == 0:
            return None
    except (OSError, subprocess.SubprocessError):
        pass
    if not TAILNET_SERVICE.exists():
        return (
            "The DRONE_TAILNET service is not installed. Use Install Tailscale on the "
            "Controls page to add the mesh daemon, then try again."
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
    the OAuth token exchange and the device key-expiry update below.

    A non-2xx response is re-raised as a RuntimeError carrying Tailscale's own
    rejection reason (via _format_http_error, which reads the response body --
    e.g. "tags are required for keys created by an OAuth client") instead of
    letting the bare urllib.error.HTTPError escape uncaught. Every caller here
    only ever catches RuntimeError; without this, a 4xx/5xx from Tailscale's
    API surfaced as a bare "HTTP Error 400: Bad Request" with the actual
    reason discarded, both to the admin UI and to this process's own logs.
    """
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except (urllib.error.HTTPError, urllib.error.URLError) as error:
        raise RuntimeError(f"Tailscale API request failed: {_format_http_error(error)}") from error
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
    client_id, client_secret, _tag = _resolve_oauth_client(settings)
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


_AUTH_URL_RE = re.compile(r"https://login\.tailscale\.com/a/\S+")


class TailnetApprovalRequired(RuntimeError):
    """`tailscale up` is waiting on a human to approve this device.

    Some Tailscale accounts (device approval turned on tenant-wide, or an
    auth key created without "Pre-authenticated") never auto-authorize a new
    device from the key alone: the control server issues a one-time approval
    URL instead, and `up --timeout=...` just times out waiting for a state
    that can't be reached without a human visiting it. Without this, that
    URL was only ever visible by SSHing in and reading tailscaled's own log
    -- surfaced here as a structured field so the admin UI can render it as
    a real link instead of a dead-end "timed out" message.
    """

    def __init__(self, message: str, auth_url: str) -> None:
        super().__init__(message)
        self.auth_url = auth_url


def _find_tailscale_auth_url(*texts: str) -> str:
    for text in texts:
        match = _AUTH_URL_RE.search(text or "")
        if match:
            return match.group(0)
    return ""


def tailnet_enroll(auth_key: str, settings: Optional[Any] = None) -> dict:
    """Enroll this device in the tailnet with an auth key from the admin console.

    Raises ValueError for bad input, TailnetApprovalRequired when the control
    server is waiting on a human to approve the device (carries the approval
    URL), and RuntimeError with a user-facing message (never containing the
    key) for every other failure.
    """
    key = str(auth_key or "").strip()
    if not key:
        raise ValueError("auth key is required")
    if not TAILSCALE_CLI.exists():
        raise RuntimeError(
            "Tailscale is not installed on this Drone. Use Install Tailscale on the "
            "Controls page (or the Swarm page's Tailnet card), then paste the key again."
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
            # tailscale up reads TS_AUTHKEY when --authkey is absent, which
            # keeps the reusable key out of the process command line.
            extra_env={"TS_AUTHKEY": key},
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Tailnet enrollment timed out; check the key and try again.") from error
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"Tailnet enrollment could not run: {error}") from error
    if proc.returncode != 0:
        # A pending-approval device never reaches Running within --timeout,
        # so `up` reports its own generic timeout line as the failure -- the
        # actually useful fact (a one-time approval URL) was emitted earlier
        # in the same output and would otherwise be discarded by only ever
        # keeping the last line. Found looking for exactly this URL, never
        # the key, so the "never log/echo the key" contract is unaffected.
        auth_url = _find_tailscale_auth_url(proc.stdout or "", proc.stderr or "")
        if auth_url:
            raise TailnetApprovalRequired(
                "This Tailscale account requires a human to approve new devices. "
                "Open the approval link to finish enrolling this Drone.",
                auth_url,
            )
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(
            "Tailnet enrollment failed: " + (detail[-1] if detail else f"exit code {proc.returncode}")
        )
    _maybe_disable_key_expiry(settings)
    if settings is not None:
        # A direct enroll (a human pasting a key, or tailnet_rotate_auth_key
        # -- which calls this function internally) is always a fresh,
        # self-owned key: reset sharing provenance to empty, same "fresh
        # write = clean provenance" rule vpn_manager.save_uploaded_config()
        # follows. import_tailnet_from_peer() calls this function first
        # (to actually enroll), then re-applies the real peer provenance
        # immediately after in its own follow-up call -- get that ordering
        # backwards and every import would look self-owned and pass the
        # single-hop sharing gate it's meant to fail.
        _save_sharing_state(
            settings, auth_key=key, source_peer_id="", source_peer_name="",
            revoked_reason="", revoked_at=None,
        )
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
    except TailnetApprovalRequired:
        # Re-raise unchanged: the caller needs auth_url, which a generic
        # RuntimeError wrapper below would discard.
        raise
    except RuntimeError as error:
        raise RuntimeError(
            "Tailnet auth token rotation disconnected this Drone but re-enrollment failed: " + str(error).replace(key, "[redacted]")
        ) from error


# ------------------------------------------------------------ invite keys
#
# "Invite Drone to Swarm": the swarm owner mints a fresh, ready-to-use auth
# key from this Drone's own admin UI and copies it to whoever is setting up
# a new machine -- that person never visits Tailscale's site at all. This
# needs an OAuth client (Tailscale admin console -> Settings -> OAuth
# clients) configured once, the same credential _maybe_disable_key_expiry()
# already supports via settings.tailscale_oauth_client_id/_secret (env
# vars, set at deploy time) -- but minting keys interactively from the
# browser needs a credential an operator can paste in after the fact, so
# this adds a second, persisted storage path (_load_oauth_client_state/
# set_tailnet_oauth_client, mirrors _load_sharing_state's shape exactly) and
# _resolve_oauth_client() prefers that persisted value, falling back to the
# env vars so a Drone already configured that way keeps working unchanged.

TAILNET_OAUTH_CLIENT_STATE_NAMESPACE = "tailnet_oauth_client.json"


_TAG_RE = re.compile(r"^tag:[a-zA-Z][a-zA-Z0-9-]*$")


def _load_oauth_client_state(settings: Settings) -> dict:
    stored = _load_state_payload(_state_database_path(settings.userdata_root), TAILNET_OAUTH_CLIENT_STATE_NAMESPACE, {})
    stored = stored if isinstance(stored, dict) else {}
    return {
        "client_id": str(stored.get("client_id") or ""),
        "client_secret": str(stored.get("client_secret") or ""),
        "tag": str(stored.get("tag") or ""),
    }


def set_tailnet_oauth_client(settings: Settings, client_id: str, client_secret: str, tag: str) -> dict:
    """Persist an OAuth client for minting invite keys. Never returns the
    secret back (mirrors every other credential store in this app).

    ``tag`` is required: Tailscale rejects a key created by an OAuth client
    with no tag ("tailnet-owned auth key must have tags set") -- found live
    against a real account. It must already exist in this tailnet's own ACL
    policy (the ``tagOwners`` section in Tailscale's admin console); this
    app has no way to create or validate that policy entry, only to check
    the ``tag:name`` shape before sending it anywhere.
    """
    client_id = str(client_id or "").strip()
    client_secret = str(client_secret or "").strip()
    tag = str(tag or "").strip()
    if not client_id or not client_secret or not tag:
        raise ValueError("the OAuth client ID, client secret, and tag are all required")
    if not _TAG_RE.match(tag):
        raise ValueError('tag must look like "tag:something" (letters, digits, and hyphens only)')
    _save_state_payload(
        _state_database_path(settings.userdata_root),
        TAILNET_OAUTH_CLIENT_STATE_NAMESPACE,
        {"client_id": client_id, "client_secret": client_secret, "tag": tag},
    )
    return oauth_client_status(settings)


def clear_tailnet_oauth_client(settings: Settings) -> dict:
    _save_state_payload(
        _state_database_path(settings.userdata_root),
        TAILNET_OAUTH_CLIENT_STATE_NAMESPACE,
        {"client_id": "", "client_secret": "", "tag": ""},
    )
    return oauth_client_status(settings)


def _resolve_oauth_client(settings: Settings) -> tuple[str, str, str]:
    # The persisted-state read is best-effort: a caller passing a bare
    # mock/stub settings object (or a genuinely unreadable state DB) must
    # still fall back to the env-var pair cleanly, not raise, since this
    # whole path backs an already best-effort feature
    # (_maybe_disable_key_expiry never raises either). There is no env-var
    # fallback for the tag (it's new, invite-only) -- a legacy env-var-only
    # setup still works for _maybe_disable_key_expiry, just not for minting
    # invite keys, which raises its own clear error when the tag is blank.
    try:
        stored = _load_oauth_client_state(settings)
    except Exception:
        stored = {"client_id": "", "client_secret": "", "tag": ""}
    if stored["client_id"] and stored["client_secret"]:
        return stored["client_id"], stored["client_secret"], stored["tag"]
    return (
        str(getattr(settings, "tailscale_oauth_client_id", "") or ""),
        str(getattr(settings, "tailscale_oauth_client_secret", "") or ""),
        "",
    )


def oauth_client_status(settings: Settings) -> dict:
    """Never includes the secret -- just enough for the admin UI to show
    whether inviting is available and, if so, which client ID/tag are active."""
    client_id, client_secret, tag = _resolve_oauth_client(settings)
    return {"configured": bool(client_id and client_secret and tag), "client_id": client_id, "tag": tag}


def create_invite_auth_key(settings: Settings) -> dict:
    """Mint a fresh, reusable, pre-authorized Tailscale auth key for inviting
    a new Drone into this tailnet.

    Deliberately short-lived (``DRONE_TAILNET_INVITE_KEY_EXPIRY_SECONDS``,
    default 1 hour, floor 5 minutes) -- unlike the OAuth client secret itself
    (held long-term), this key is meant to be copied, sent to someone, and
    used once shortly after. ``preauthorized: true`` means the recipient's
    device skips the interactive device-approval step entirely (see
    ``TailnetApprovalRequired``) -- the owner approved it implicitly by
    choosing to hand it out. ``reusable: true`` so one invite can still cover
    more than one new machine if the owner reuses it within the expiry
    window, matching how a manually-created key is normally used here.

    Raises RuntimeError (never containing either secret) if no OAuth client
    (with a tag) is configured or Tailscale's API rejects the request.
    Tailscale requires a tag on any key an OAuth client creates -- found
    live ("tailnet-owned auth key must have tags set") against a real
    account; an empty tags list is rejected outright, so the configured tag
    is always included.
    """
    client_id, client_secret, tag = _resolve_oauth_client(settings)
    if not client_id or not client_secret or not tag:
        raise RuntimeError(
            "No Tailscale OAuth client (with a tag) is configured. Add one, including a tag "
            "that already exists in this tailnet's ACL policy, on the Tailnet card first."
        )
    token = _tailscale_oauth_access_token(client_id, client_secret)
    expiry_seconds = max(300, int(os.environ.get("DRONE_TAILNET_INVITE_KEY_EXPIRY_SECONDS", "3600")))
    payload = _tailscale_api_request(
        f"{TAILSCALE_API_BASE}/tailnet/-/keys",
        method="POST",
        data=json.dumps(
            {
                "capabilities": {
                    "devices": {"create": {"reusable": True, "ephemeral": False, "preauthorized": True, "tags": [tag]}}
                },
                "expirySeconds": expiry_seconds,
                "description": "Drone swarm invite",
            }
        ).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    key = str(payload.get("key") or "")
    if not key:
        raise RuntimeError("Tailscale did not return a new auth key")
    return {"key": key, "expires": payload.get("expires") or "", "expiry_seconds": expiry_seconds}


# ------------------------------------------------------------ peer sharing
#
# Mirrors vpn_manager.py's peer-sharing section closely (single-hop-only
# provenance, share-revocation, default-off swarm bootstrap) -- see that
# module and the drone-vpn-management skill for the shared design rationale.
# One deliberate difference: revocation here never touches the live tailnet
# connection (see _revoke_local_sharing()'s docstring) -- Tailscale
# enrollment is a one-time join this drone may depend on for its own
# unrelated P2P networking, unlike VPN's credentials, which are what keeps
# an active tunnel up in the first place.

TAILNET_SHARING_STATE_NAMESPACE = "tailnet_sharing.json"
try:
    TAILNET_SHARING_CHECK_INTERVAL_SECONDS = poller_interval_from_env("DRONE_TAILNET_SHARING_CHECK_INTERVAL_SECONDS", 300.0)
except Exception:
    TAILNET_SHARING_CHECK_INTERVAL_SECONDS = 300.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_sharing_state(settings: Settings) -> dict:
    stored = _load_state_payload(_state_database_path(settings.userdata_root), TAILNET_SHARING_STATE_NAMESPACE, {})
    stored = stored if isinstance(stored, dict) else {}
    return {
        "auth_key": str(stored.get("auth_key") or ""),
        "source_peer_id": str(stored.get("source_peer_id") or ""),
        "source_peer_name": str(stored.get("source_peer_name") or ""),
        "revoked_reason": str(stored.get("revoked_reason") or ""),
        "revoked_at": stored.get("revoked_at"),
    }


def _save_sharing_state(settings: Settings, **updates) -> dict:
    state = _load_sharing_state(settings)
    state.update(updates)
    _save_state_payload(_state_database_path(settings.userdata_root), TAILNET_SHARING_STATE_NAMESPACE, state)
    return state


def tailnet_sharing_status(settings: Settings) -> dict:
    """Sharing/provenance fields for the admin status payload -- never
    includes the raw ``auth_key`` (mirrors ``vpn_manager.status()`` never
    returning the VPN password), just whether one is on file to share.

    There is no on/off switch: a self-owned key is always offered to paired
    peers (``shareable``), and an imported key never is, regardless of any
    stored preference -- single-hop-only sharing is a property of the key's
    provenance, not an operator toggle. ``shareable`` is computed fresh here
    from the same two fields ``export_tailnet_payload()`` checks, so the two
    can never drift apart the way a separately-stored flag could.
    """
    state = _load_sharing_state(settings)
    return {
        "shareable": bool(state["auth_key"]) and not state["source_peer_id"],
        "has_shared_key": bool(state["auth_key"]),
        "source_peer_id": state["source_peer_id"],
        "source_peer_name": state["source_peer_name"],
        "revoked_reason": state["revoked_reason"],
        "revoked_at": state["revoked_at"],
    }


def export_tailnet_payload(settings: Settings) -> Optional[dict]:
    """This drone's Tailscale auth key for a paired peer to pull.

    ``None`` means "don't share" -- the caller (``GET /peer/tailnet/config``)
    turns that into a 404. Mirrors ``vpn_manager.export_payload()``: only
    ever served over the cert-pinned mTLS ``/peer/*`` channel, never returned
    to a browser. A self-owned key (``source_peer_id`` empty) is always
    offered, with no operator toggle to forget to flip -- single-hop-only
    sharing (an imported key is never re-offered) is the actual security
    boundary and is enforced unconditionally right here, the one place the
    key would actually leave the drone.
    """
    state = _load_sharing_state(settings)
    if not state["auth_key"] or state["source_peer_id"]:
        return None
    return {"auth_key": state["auth_key"], "enrolled": tailnet_status().get("enrolled")}


def import_tailnet_from_peer(settings: Settings, payload: dict, *, source_peer_id: str, source_peer_name: str = "") -> dict:
    """Adopt a peer's shared Tailscale auth key: enroll with it (reusing
    ``tailnet_enroll()``, the same real enrollment path a human pasting a
    key uses -- not a separate write), then re-apply the real provenance
    immediately after. ``tailnet_enroll()`` resets provenance to
    "self-owned" as part of its own fresh-enrollment semantics, so the
    ordering here matters exactly like
    ``vpn_manager.import_from_peer()``'s does.

    ``source_peer_id`` is supplied by the *caller* (the drone_id it just
    successfully mTLS-authenticated against), never trusted from the wire
    payload -- this becomes the key's permanent provenance marker, checked
    by ``export_tailnet_payload()`` to enforce single-hop-only sharing.
    """
    source_peer_id = str(source_peer_id or "").strip()
    if not source_peer_id:
        raise ValueError("source_peer_id is required to import a peer's Tailscale auth key")
    payload = payload if isinstance(payload, dict) else {}
    auth_key = str(payload.get("auth_key") or "")
    if not auth_key:
        raise ValueError("peer did not return a Tailscale auth key")
    result = tailnet_enroll(auth_key, settings)
    _save_sharing_state(settings, source_peer_id=source_peer_id, source_peer_name=str(source_peer_name or "").strip())
    return result


def bootstrap_tailnet_from_swarm(settings: Settings) -> bool:
    """Adopt a paired peer's actively-shared, currently-working Tailscale
    enrollment as our own -- mirrors ``vpn_manager.bootstrap_vpn_from_swarm()``
    closely (the ``enrolled`` check on the peer's payload plays the same
    role as VPN's ``connected`` check: only adopt from a peer that is
    demonstrably actually on the tailnet right now, not merely configured
    to share).

    Only ever called when this drone is **not currently enrolled** -- never
    overrides an existing enrollment. Tries every paired peer, in
    ``local_network.paired_peers()``'s own order, stopping at the first one
    that is both sharing and enrolled right now. Per-peer failures (offline,
    not sharing, malformed payload) are silently skipped, not errors --
    there may be many paired peers and only one needs to work.
    """
    try:
        from ..transfer import local_network as _local_network
        from ..transfer.peer_connectivity import _peer_get_json_for_peer
    except ImportError:  # pragma: no cover - direct script execution fallback
        from transfer import local_network as _local_network  # type: ignore
        from transfer.peer_connectivity import _peer_get_json_for_peer  # type: ignore
    for peer in _local_network.paired_peers(settings):
        peer_id = str(peer.get("drone_id") or peer.get("device_id") or peer.get("id") or "").strip()
        if not peer_id:
            continue
        try:
            payload, _address = _peer_get_json_for_peer(peer, "/v1/api/peer/tailnet/config", settings, peer_id=peer_id)
        except Exception:
            continue  # offline, not paired on this address, sharing off, no key, etc.
        if not isinstance(payload, dict) or not payload.get("auth_key") or not payload.get("enrolled"):
            continue
        peer_name = str(peer.get("name") or peer.get("hostname") or peer_id)
        try:
            import_tailnet_from_peer(settings, payload, source_peer_id=peer_id, source_peer_name=peer_name)
        except Exception as error:
            print(
                f"Swarm Tailnet bootstrap: failed to adopt the auth key shared by {peer_name}: "
                f"{error.__class__.__name__}: {error}",
                file=sys.stderr, flush=True,
            )
            continue
        print(f"Swarm Tailnet bootstrap: enrolled using the auth key shared by {peer_name}", file=sys.stdout, flush=True)
        return True
    return False


def maybe_bootstrap_tailnet(settings: Settings) -> None:
    """Best-effort, called once from ``create_server()`` startup, mirroring
    ``smtp_manager.maybe_bootstrap_smtp()``. Never raises. A Drone that is
    still not enrolled afterwards is retried by ``peer_workers``' bootstrap
    retry thread (``DRONE_TAILNET_BOOTSTRAP_INTERVAL_SECONDS``, default 300)."""
    try:
        if not tailnet_status().get("enrolled"):
            bootstrap_tailnet_from_swarm(settings)
    except Exception as error:
        print(f"Tailnet swarm bootstrap failed: {error.__class__.__name__}: {error}", file=sys.stderr, flush=True)


_SHARING_REVOKED_PEER_OFF = "The peer sharing this Tailscale auth key turned off sharing, so it was removed from this drone."
_SHARING_REVOKED_PEER_GONE = "The peer that shared this Tailscale auth key is no longer paired, so it was removed from this drone."


def _revoke_local_sharing(settings: Settings, reason: str) -> None:
    """Clear the imported auth key and stop sharing -- deliberately does
    **not** call ``tailscale logout`` or otherwise touch the live tailnet
    connection. Unlike VPN's revocation (which disconnects an active tunnel
    that the credentials themselves keep alive), Tailscale enrollment is a
    one-time join: this drone may depend on that tailnet membership for its
    own unrelated P2P networking regardless of whether the auth key that
    enrolled it is still shareable. Revoking here only means "this drone
    can no longer claim to have a live, working copy of that key to hand to
    further peers" -- the actual authority over whether a device stays in
    the tailnet is Tailscale's own admin console, not this app.

    ``source_peer_id`` deliberately survives, same reasoning as
    ``vpn_manager._revoke_local_credentials()``: wiping it would let this
    now-orphaned entry pass the single-hop sharing gate as if it were
    self-owned. Only a genuine fresh ``tailnet_enroll()`` call clears it.
    """
    _save_sharing_state(settings, auth_key="", revoked_reason=reason, revoked_at=_now_iso())


def check_tailnet_sharing_revocation(settings: Settings) -> bool:
    """If this auth key was imported from a peer, verify that peer still
    shares it; revoke (clear the stored key, never the live connection) if
    not. Returns True iff a revocation just happened. Mirrors
    ``vpn_manager.check_sharing_revocation()`` exactly -- see that
    function's docstring for why only "peer gone" / "peer 404s" count as
    revocation, and every other outcome (unreachable, timeout, any other
    HTTP status) changes nothing: a flaky or briefly-offline peer must
    never strip a working setup. Never raises (intended to run unattended
    on a background poller)."""
    try:
        state = _load_sharing_state(settings)
        source_peer_id = state["source_peer_id"]
        if not source_peer_id or not state["auth_key"]:
            return False
        try:
            from ..transfer import local_network as _local_network
            from ..transfer.peer_connectivity import _peer_get_json_for_peer, peer_http_error_is_authenticated
        except ImportError:  # pragma: no cover - direct script execution fallback
            from transfer import local_network as _local_network  # type: ignore
            from transfer.peer_connectivity import _peer_get_json_for_peer, peer_http_error_is_authenticated  # type: ignore
        peer = _local_network.get_paired_peer(settings, source_peer_id)
        if not peer:
            _revoke_local_sharing(settings, _SHARING_REVOKED_PEER_GONE)
            return True
        try:
            _peer_get_json_for_peer(peer, "/v1/api/peer/tailnet/config", settings, peer_id=source_peer_id)
            return False
        except urllib.error.HTTPError as error:
            # A 404 over plain http is unauthenticated and forgeable: treat it as transient.
            if error.code == 404 and peer_http_error_is_authenticated(error):
                _revoke_local_sharing(settings, _SHARING_REVOKED_PEER_OFF)
                return True
            return False
        except Exception:
            return False
    except Exception as error:
        _log_poller_failure("Tailnet sharing revocation check failed", error)
        return False


def _poller_error_detail(error: Exception) -> str:
    """Format an unexpected poller exception without trusting its __str__."""
    try:
        detail = str(error)
    except Exception:
        detail = "<unprintable exception>"
    return f"{error.__class__.__name__}: {detail}"


def _log_poller_failure(message: str, error: Exception) -> None:
    """Report a poller tick failure without letting the report stop the poller."""
    try:
        text = f"{message}: {_poller_error_detail(error)}"
        text = text.encode("utf-8", "backslashreplace").decode("utf-8")
        print(text, file=sys.stderr, flush=True)
    except Exception:
        pass


def run_tailnet_sharing_revocation_poller(settings: Settings, generation: int | None = None) -> None:
    """Forever-loop checking whether an imported auth key's sharing was
    revoked -- mirrors ``vpn_manager.run_sharing_revocation_poller()``
    exactly, including the per-tick Exception guard and
    ``_log_poller_failure`` isolation. Started as its own daemon thread
    from ``create_server()``."""
    try:
        interval = poller_interval_from_env("DRONE_TAILNET_SHARING_CHECK_INTERVAL_SECONDS", 300.0)
    except Exception:
        interval = 300.0
    if generation is None:
        generation = poller_generation()
    while True:
        time.sleep(interval)
        if poller_retired(generation):
            return
        try:
            check_tailnet_sharing_revocation(settings)
        except Exception as error:
            _log_poller_failure("Tailnet sharing revocation check failed", error)
