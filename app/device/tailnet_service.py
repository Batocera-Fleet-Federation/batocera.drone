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
import select
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    from ..common.http_errors import _format_http_error
    from ..common.settings import Settings
    from ..storage.state_store import database_path as _state_database_path
    from ..storage.state_store import load_payload as _load_state_payload
    from ..storage.state_store import save_payload as _save_state_payload
    from ..transport.tailnet import get_tailnet_ip
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.http_errors import _format_http_error  # type: ignore
    from common.settings import Settings  # type: ignore
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import load_payload as _load_state_payload  # type: ignore
    from storage.state_store import save_payload as _save_state_payload  # type: ignore
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
    except RuntimeError as error:
        raise RuntimeError(
            "Tailnet auth token rotation disconnected this Drone but re-enrollment failed: " + str(error).replace(key, "[redacted]")
        ) from error


_LOGIN_URL_PATTERN = re.compile(rb"https://login\.tailscale\.com/\S+")


def tailnet_enroll_interactive(wait_seconds: float = 20.0) -> str:
    """Start passwordless enrollment and return the one-time login URL
    Tailscale prints, without waiting for a human to actually approve it.

    Unlike ``tailnet_enroll()``, no secret is received or transmitted here:
    running ``tailscale up`` with no ``--authkey`` makes tailscaled contact
    Tailscale's own coordination servers and print an interactive
    ``https://login.tailscale.com/a/...`` URL for a human to open in *any*
    browser, anywhere -- approval happens entirely on Tailscale's side, not
    by handing this device anything sensitive. This is the primitive the
    GitHub-mailbox enrollment flow (``device/enrollment_mailbox.py``) is
    built on: it lets a Drone with no reachable admin UI ask to join the
    tailnet without a secret ever having to reach it.

    Deliberately no ``--timeout`` flag (unlike ``tailnet_enroll()``'s
    ``--timeout=45s``): that flag would make the CLI give up and tear down
    the pending login after N seconds, but approval can genuinely take
    anywhere from seconds to days. tailscaled keeps the pending request
    alive on its own regardless of whether this short-lived CLI process is
    still attached, so the process is deliberately **not** waited on to
    completion here -- only read from just long enough (``wait_seconds``)
    to capture the URL it prints, then left to exit on its own once the
    login resolves (approved, expired, or superseded by a later attempt).
    Detached via ``start_new_session=True``, the same self-daemonizing
    pattern ``vpn_manager`` uses for the ``openvpn`` process.

    Raises RuntimeError (mirrors ``tailnet_enroll()``'s error contract) if
    Tailscale is not installed/running, or no URL appears within
    ``wait_seconds`` -- e.g. because this device is already enrolled, in
    which case there is nothing to approve and no URL is ever printed.
    """
    if not TAILSCALE_CLI.exists():
        raise RuntimeError(
            "Tailscale is not installed on this Drone. Re-run the Drone installer "
            "(batocera_install.sh) once to add it, then try again."
        )
    daemon_error = _start_daemon_if_needed()
    if daemon_error:
        raise RuntimeError(daemon_error)
    hostname = socket.gethostname().lower()
    try:
        process = subprocess.Popen(
            [
                str(TAILSCALE_CLI),
                f"--socket={TAILSCALE_SOCKET}",
                "up",
                f"--hostname={hostname}",
                "--accept-dns=false",
                "--netfilter-mode=off",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as error:
        raise RuntimeError(f"Tailnet interactive enrollment could not start: {error}") from error
    buffer = b""
    url = ""
    deadline = time.monotonic() + max(1.0, wait_seconds)
    # select() + read1() (not readline()) deliberately -- a text-mode
    # readline() can block past the deadline waiting for a newline that
    # tailscale hasn't flushed yet; read1() returns as soon as any bytes at
    # all are available, so the deadline is actually honored.
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            ready, _, _ = select.select([process.stdout], [], [], max(0.0, min(1.0, remaining)))
        except (OSError, ValueError):
            break
        if not ready:
            if process.poll() is not None:
                break
            continue
        chunk = process.stdout.read1(4096)
        if not chunk:
            if process.poll() is not None:
                break
            continue
        buffer += chunk
        match = _LOGIN_URL_PATTERN.search(buffer)
        if match:
            url = match.group(0).decode("utf-8", errors="replace")
            break
    if url:
        return url
    exit_code = process.poll()
    if exit_code not in (None, 0):
        detail = buffer.decode("utf-8", errors="replace").strip().splitlines()
        raise RuntimeError(
            "Tailnet interactive enrollment failed: " + (detail[-1] if detail else f"exit code {exit_code}")
        )
    raise RuntimeError(
        "Tailnet did not print a login URL in time; it may already be enrolled, "
        "or the tailnet daemon is unresponsive."
    )


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
TAILNET_SHARING_CHECK_INTERVAL_SECONDS = float(os.environ.get("DRONE_TAILNET_SHARING_CHECK_INTERVAL_SECONDS", "300"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_sharing_state(settings: Settings) -> dict:
    stored = _load_state_payload(_state_database_path(settings.userdata_root), TAILNET_SHARING_STATE_NAMESPACE, {})
    stored = stored if isinstance(stored, dict) else {}
    return {
        "auth_key": str(stored.get("auth_key") or ""),
        "sharing_enabled": bool(stored.get("sharing_enabled", False)),
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
    returning the VPN password), just whether one is on file to share."""
    state = _load_sharing_state(settings)
    return {
        "sharing_enabled": state["sharing_enabled"],
        "has_shared_key": bool(state["auth_key"]),
        "source_peer_id": state["source_peer_id"],
        "source_peer_name": state["source_peer_name"],
        "revoked_reason": state["revoked_reason"],
        "revoked_at": state["revoked_at"],
    }


def set_tailnet_sharing_enabled(settings: Settings, enabled: bool) -> dict:
    """Mirrors ``vpn_manager.set_sharing_enabled()`` exactly: an imported key
    can never be re-shared -- only the drone that originally enrolled with
    it (a real paste into ``tailnet_enroll()``, not an import) can turn
    sharing on. Enforced here *and* independently in
    ``export_tailnet_payload()``, same belt-and-suspenders reasoning as
    VPN/SMTP -- don't remove either check on the assumption the other
    covers it."""
    if enabled and _load_sharing_state(settings)["source_peer_id"]:
        raise ValueError(
            "This Tailscale auth key was imported from another drone and cannot be re-shared. "
            "Only the drone that originally connected with it can share it with the swarm."
        )
    state = _save_sharing_state(settings, sharing_enabled=bool(enabled))
    return {"sharing_enabled": state["sharing_enabled"]}


def export_tailnet_payload(settings: Settings) -> Optional[dict]:
    """This drone's Tailscale auth key for a paired peer to pull.

    ``None`` means "don't share" -- the caller (``GET /peer/tailnet/config``)
    turns that into a 404. Mirrors ``vpn_manager.export_payload()``: only
    ever served over the cert-pinned mTLS ``/peer/*`` channel, gated by
    pairing (checked by the caller) plus ``sharing_enabled`` here, never
    returned to a browser. The redundant ``source_peer_id`` check is kept
    here too even though ``set_tailnet_sharing_enabled()`` already refuses
    to enable sharing on an imported key -- this is the actual point the
    key would leave the drone, so it's the right place to enforce
    single-hop-only even if a future bug ever let ``sharing_enabled`` get
    set some other way.
    """
    state = _load_sharing_state(settings)
    if not state["sharing_enabled"] or not state["auth_key"] or state["source_peer_id"]:
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
    by ``set_tailnet_sharing_enabled()``/``export_tailnet_payload()`` to
    enforce single-hop-only sharing.
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
    ``smtp_manager.maybe_bootstrap_smtp()``. Never raises."""
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
    _save_sharing_state(settings, auth_key="", sharing_enabled=False, revoked_reason=reason, revoked_at=_now_iso())


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
            from ..transfer.peer_connectivity import _peer_get_json_for_peer
        except ImportError:  # pragma: no cover - direct script execution fallback
            from transfer import local_network as _local_network  # type: ignore
            from transfer.peer_connectivity import _peer_get_json_for_peer  # type: ignore
        peer = _local_network.get_paired_peer(settings, source_peer_id)
        if not peer:
            _revoke_local_sharing(settings, _SHARING_REVOKED_PEER_GONE)
            return True
        try:
            _peer_get_json_for_peer(peer, "/v1/api/peer/tailnet/config", settings, peer_id=source_peer_id)
            return False
        except urllib.error.HTTPError as error:
            if error.code == 404:
                _revoke_local_sharing(settings, _SHARING_REVOKED_PEER_OFF)
                return True
            return False
        except Exception:
            return False
    except Exception as error:
        print(f"Tailnet sharing revocation check failed: {error.__class__.__name__}: {error}", file=sys.stderr, flush=True)
        return False


def run_tailnet_sharing_revocation_poller(settings: Settings) -> None:
    """Forever-loop checking whether an imported auth key's sharing was
    revoked -- mirrors ``vpn_manager.run_sharing_revocation_poller()``
    exactly. Started as its own daemon thread from ``create_server()``."""
    interval = max(30.0, TAILNET_SHARING_CHECK_INTERVAL_SECONDS)
    while True:
        time.sleep(interval)
        check_tailnet_sharing_revocation(settings)
