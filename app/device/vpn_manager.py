"""OpenVPN client management: upload a provider's .ovpn, store credentials,
connect/disconnect, and report live status -- entirely from the Drone admin
UI, no CLI/SSH required. Provider-agnostic (Proton VPN, NordVPN, PIA, ...):
nothing here is Proton-specific, it just rewrites whatever ``.ovpn`` is
uploaded to route through the credentials file this module manages.

Unlike TorrentManager, there is no background worker thread here: a VPN
connection is exactly one process, so ``status()`` recomputes it fresh on
every call (check /proc, tail the log, ask ``ip`` for the tunnel address) --
cheap enough to run on every admin-page poll, and far simpler than keeping a
cached state machine in sync with a thread. ``connect``/``disconnect`` are
guarded by a module-level lock only to stop two racing clicks from spawning
two openvpn processes; everything else is stateless.

Config/credentials/log live under ``<install root>/vpn/`` (see
``common/install_paths.py``) rather than ``/userdata/system/openvpn`` --
keeping every Drone-managed artifact under one root, consistent with the
Torrents feature's ``<install root>/torrents``.

Drone runs as root on-device (see ``service_bootstrap.sh``), so spawning and
signaling openvpn needs no privilege-escalation dance -- direct subprocess
calls, exactly like the wifi-recovery automation's ``batocera-wifi`` calls.

Connecting on boot does not create a separate OS service: the Drone app
itself already starts at boot (it's the ``DRONE_SERVER`` service), so
``maybe_auto_connect()`` runs during ``create_server()`` startup and connects
whenever the config is ready -- being configured (a saved ``.ovpn`` +
credentials) is the only condition, there is no separate opt-in toggle.
Retries a few times with a short delay so a transient boot-time condition
(network/mount not ready yet) self-heals without a human needing to notice
and manually reconnect. Simpler and more robust than juggling a second
boot-ordering-dependent service unit.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import List, Optional
from urllib.error import HTTPError

try:
    from ..common.install_paths import drone_install_root as _drone_install_root
    from ..common.logtail import _tail_lines
    from ..common.settings import Settings
    from ..storage.state_store import database_path as _state_database_path
    from ..storage.state_store import load_payload as _load_state_payload
    from ..storage.state_store import save_payload as _save_state_payload
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.install_paths import drone_install_root as _drone_install_root  # type: ignore
    from common.logtail import _tail_lines  # type: ignore
    from common.settings import Settings  # type: ignore
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import load_payload as _load_state_payload  # type: ignore
    from storage.state_store import save_payload as _save_state_payload  # type: ignore

VPN_STATE_NAMESPACE = "vpn_manager.json"
OPENVPN_CONFIG_FILENAME = "client.ovpn"
OPENVPN_AUTH_FILENAME = "auth.txt"
OPENVPN_LOG_FILENAME = "vpn.log"
VPN_UPLOAD_MAX_BYTES = 5 * 1024 * 1024
VPN_CONNECT_TIMEOUT_SECONDS = float(os.environ.get("DRONE_VPN_CONNECT_TIMEOUT_SECONDS", "15"))
VPN_AUTO_CONNECT_MAX_ATTEMPTS = int(os.environ.get("DRONE_VPN_AUTO_CONNECT_MAX_ATTEMPTS", "4"))
VPN_AUTO_CONNECT_RETRY_DELAY_SECONDS = float(os.environ.get("DRONE_VPN_AUTO_CONNECT_RETRY_DELAY_SECONDS", "15"))
VPN_DISCONNECT_GRACE_SECONDS = float(os.environ.get("DRONE_VPN_DISCONNECT_GRACE_SECONDS", "5"))
VPN_PUBLIC_IP_CHECK_TIMEOUT_SECONDS = float(os.environ.get("DRONE_VPN_PUBLIC_IP_TIMEOUT_SECONDS", "8"))
VPN_LOG_TAIL_LINES_FOR_DISPLAY = 200
VPN_LOG_TAIL_LINES_FOR_DETECTION = 400
VPN_LOG_TAIL_MAX_BYTES = 256 * 1024
VPN_STATUSES = ("disconnected", "connecting", "connected", "error")

# Self-heal watchdog: how often the background poller checks, how long it
# waits between actual reconnect actions, and the rolling-window cap that
# stops it from hammering a persistently-broken connection (or the VPN
# provider's server) forever. The cap ages out on its own as old attempts
# fall out of the window -- this is a temporary backoff, not a permanent
# give-up; see run_self_heal_poller()'s docstring.
VPN_SELF_HEAL_CHECK_INTERVAL_SECONDS = float(os.environ.get("DRONE_VPN_SELF_HEAL_CHECK_INTERVAL_SECONDS", "60"))
VPN_SELF_HEAL_MIN_INTERVAL_SECONDS = float(os.environ.get("DRONE_VPN_SELF_HEAL_MIN_INTERVAL_SECONDS", "120"))
VPN_SELF_HEAL_MAX_ATTEMPTS_PER_WINDOW = int(os.environ.get("DRONE_VPN_SELF_HEAL_MAX_ATTEMPTS_PER_WINDOW", "5"))
VPN_SELF_HEAL_WINDOW_SECONDS = float(os.environ.get("DRONE_VPN_SELF_HEAL_WINDOW_SECONDS", "1800"))

# A healthy, already-connected tunnel logs almost nothing -- openvpn only
# writes on events. So if the *most recent* log lines are dominated by this
# repeating warning, that's a strong signal something is actively wrong right
# now, without needing to parse log timestamps: a one-off blip a while ago
# would have scrolled out of such a short, recent window on its own.
_UNHEALTHY_LOG_PATTERNS = ("AEAD Decrypt error", "bad packet ID")
_UNHEALTHY_LOG_WINDOW_LINES = 40
_UNHEALTHY_LOG_MIN_MATCHES = 10

_CONNECT_LOCK = Lock()

# Substrings openvpn's own log uses; matched independent of version/provider
# wording differences beyond these fairly stable phrases.
_SUCCESS_MARKER = "Initialization Sequence Completed"
_FAILURE_MARKERS = (
    "AUTH_FAILED",
    "auth-failure",
    "TLS Error",
    "TLS handshake failed",
    "Cannot resolve host",
    "connection refused",
    "Connection reset, restarting",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def vpn_dir(settings: Settings) -> Path:
    # DRONE_VPN_DIR is an escape hatch for tests/ops (matching the
    # DRONE_STATE_DATABASE_FILE-style overrides elsewhere) -- the admin UI
    # itself never exposes this as a configurable field, per the feature
    # spec: OpenVPN artifacts always live under the Drone's own install dir.
    configured = os.environ.get("DRONE_VPN_DIR")
    if configured:
        return Path(configured).resolve()
    return _drone_install_root() / "vpn"


def config_path(settings: Settings) -> Path:
    return vpn_dir(settings) / OPENVPN_CONFIG_FILENAME


def auth_path(settings: Settings) -> Path:
    return vpn_dir(settings) / OPENVPN_AUTH_FILENAME


def log_path(settings: Settings) -> Path:
    return vpn_dir(settings) / OPENVPN_LOG_FILENAME


def _load_state(settings: Settings) -> dict:
    stored = _load_state_payload(_state_database_path(settings.userdata_root), VPN_STATE_NAMESPACE, {})
    stored = stored if isinstance(stored, dict) else {}
    return {
        "has_config": bool(stored.get("has_config", False)),
        "config_filename": str(stored.get("config_filename") or ""),
        "remotes": [str(item) for item in (stored.get("remotes") or []) if str(item or "").strip()],
        "has_credentials": bool(stored.get("has_credentials", False)),
        "username": str(stored.get("username") or ""),
        "connected_at": stored.get("connected_at"),
        "sharing_enabled": bool(stored.get("sharing_enabled", False)),
        "source_peer_id": str(stored.get("source_peer_id") or ""),
        "source_peer_name": str(stored.get("source_peer_name") or ""),
        "revoked_reason": str(stored.get("revoked_reason") or ""),
        "revoked_at": stored.get("revoked_at"),
        # Default True: self-healing is opt-out, not opt-in.
        "self_heal_enabled": bool(stored.get("self_heal_enabled", True)),
        "self_heal_last_at": stored.get("self_heal_last_at"),
        "self_heal_last_reason": str(stored.get("self_heal_last_reason") or ""),
        "self_heal_attempts": [str(item) for item in (stored.get("self_heal_attempts") or []) if str(item or "").strip()],
    }


def _save_state(settings: Settings, **updates) -> dict:
    state = _load_state(settings)
    state.update(updates)
    _save_state_payload(_state_database_path(settings.userdata_root), VPN_STATE_NAMESPACE, state)
    return state


# ------------------------------------------------------------------ config


def parsed_remotes(config_text: str) -> List[str]:
    """Extract ``remote <host> <port...>`` lines for display as "VPN Server"."""
    remotes = []
    for line in config_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("remote ") or stripped == "remote":
            remainder = stripped[len("remote") :].strip()
            if remainder:
                remotes.append(remainder)
    return remotes


def rewrite_ovpn_config(raw_text: str, auth_file_path: Path) -> str:
    """Apply the Drone-managed rewrites to an uploaded provider .ovpn:

    - ``auth-user-pass`` (with or without its own path argument) is pointed
      at our managed credentials file, so any embedded/relative reference a
      provider ships is overridden.
    - ``up``/``down update-resolv-conf`` hooks are dropped -- Batocera has no
      such script, and a missing hook binary would otherwise fail the tunnel.
    - ``auth-nocache`` is appended if not already present, so a rejected
      credential is never silently retried from a stale cache.

    Raises ``ValueError`` if the file does not look like an OpenVPN client
    config at all (no ``remote`` directive), so an unrelated upload is
    rejected before it ever reaches openvpn.
    """
    lines = raw_text.splitlines()
    rewritten = []
    has_auth_nocache = False
    saw_remote = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("auth-user-pass"):
            rewritten.append(f"auth-user-pass {auth_file_path}")
            continue
        if re.match(r"^(up|down)\s+.*update-resolv-conf", stripped):
            continue
        if stripped == "auth-nocache":
            has_auth_nocache = True
        if stripped.startswith("remote ") or stripped == "remote":
            saw_remote = True
        rewritten.append(line)
    if not saw_remote:
        raise ValueError("This doesn't look like a valid OpenVPN client config -- no 'remote' directive found.")
    if not has_auth_nocache:
        rewritten.append("auth-nocache")
    return "\n".join(rewritten) + "\n"


def save_uploaded_config(settings: Settings, filename: str, raw_bytes: bytes) -> dict:
    name = str(filename or "").strip()
    if not name.lower().endswith(".ovpn"):
        raise ValueError("only .ovpn files are accepted")
    if not raw_bytes:
        raise ValueError("uploaded file is empty")
    if len(raw_bytes) > VPN_UPLOAD_MAX_BYTES:
        raise ValueError("uploaded file is larger than 5 MB")
    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("uploaded file is not a valid text (.ovpn) config")
    directory = vpn_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    rewritten = rewrite_ovpn_config(raw_text, auth_path(settings))
    config_path(settings).write_text(rewritten, encoding="utf-8")
    remotes = parsed_remotes(rewritten)
    # A fresh config write is always "self-owned, clean slate" -- any prior
    # peer-import provenance and any stale revocation notice no longer apply.
    # import_from_peer() calls this function first, then re-applies its own
    # provenance immediately afterward (see below).
    state = _save_state(
        settings, has_config=True, config_filename=name, remotes=remotes,
        source_peer_id="", source_peer_name="", revoked_reason="", revoked_at=None,
    )
    return {"status": "ok", "config_filename": state["config_filename"], "remotes": remotes}


def save_credentials(settings: Settings, username: str, password: str) -> dict:
    username = str(username or "").strip()
    password = str(password or "")
    if not username:
        raise ValueError("username is required")
    if not password:
        raise ValueError("password is required")
    directory = vpn_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    path = auth_path(settings)
    path.write_text(f"{username}\n{password}\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    state = _save_state(settings, has_credentials=True, username=username)
    return {"status": "ok", "username": state["username"]}


# ---------------------------------------------------------------- process


def find_openvpn_binary() -> Optional[str]:
    return shutil.which("openvpn")


def _find_running_openvpn_pid(target_config_path: Path, proc_root: Path = Path("/proc")) -> Optional[int]:
    """Scan /proc for an openvpn process whose --config argument is ours.

    Matching on the exact config path (not just the process name) means this
    never touches an unrelated openvpn instance that happens to be running
    for some other reason on the same box -- more precise than ``killall
    openvpn``, which is why disconnect() uses this instead.
    """
    target = str(target_config_path)
    try:
        pid_dirs = [entry for entry in proc_root.iterdir() if entry.name.isdigit()]
    except OSError:
        return None
    for pid_dir in pid_dirs:
        try:
            raw = (pid_dir / "cmdline").read_bytes()
        except OSError:
            continue
        parts = [part.decode("utf-8", errors="ignore") for part in raw.split(b"\x00") if part]
        if not parts:
            continue
        if "openvpn" not in parts[0]:
            continue
        if target in parts:
            try:
                return int(pid_dir.name)
            except ValueError:
                continue
    return None


def validate_ready(settings: Settings) -> List[str]:
    """Return validation error strings; an empty list means Connect can proceed."""
    errors = []
    state = _load_state(settings)
    if not state["has_config"]:
        errors.append("An OpenVPN configuration (.ovpn) has not been uploaded yet.")
    if not state["has_credentials"]:
        errors.append("VPN username and password have not been set.")
    if find_openvpn_binary() is None:
        errors.append("openvpn is not installed on this system.")
    return errors


def connect(settings: Settings) -> dict:
    errors = validate_ready(settings)
    if errors:
        return {"status": "error", "errors": errors}
    with _CONNECT_LOCK:
        cfg = config_path(settings)
        if _find_running_openvpn_pid(cfg) is not None:
            return {"status": "already_running"}
        binary = find_openvpn_binary()
        vpn_log = log_path(settings)
        try:
            vpn_log.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            result = subprocess.run(
                [binary, "--config", str(cfg), "--daemon", "--log", str(vpn_log)],
                capture_output=True,
                text=True,
                timeout=VPN_CONNECT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return {"status": "error", "errors": [f"Failed to start openvpn: {error}"]}
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            message = detail[-1] if detail else f"openvpn exited with status {result.returncode}"
            return {"status": "error", "errors": [message]}
        _save_state(settings, connected_at=None)
        print("VPN connect requested: openvpn daemonized", file=sys.stdout, flush=True)
        return {"status": "connecting"}


def disconnect(settings: Settings) -> dict:
    with _CONNECT_LOCK:
        cfg = config_path(settings)
        pid = _find_running_openvpn_pid(cfg)
        if pid is None:
            _save_state(settings, connected_at=None)
            return {"status": "not_running"}
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            _save_state(settings, connected_at=None)
            return {"status": "not_running"}
        except OSError as error:
            return {"status": "error", "errors": [f"Failed to stop openvpn (pid {pid}): {error}"]}
        deadline = time.monotonic() + VPN_DISCONNECT_GRACE_SECONDS
        while time.monotonic() < deadline:
            if _find_running_openvpn_pid(cfg) is None:
                break
            time.sleep(0.25)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        _save_state(settings, connected_at=None)
        return {"status": "disconnected"}


def bootstrap_vpn_from_swarm(settings: Settings) -> bool:
    """Adopt a paired peer's actively-connected, shared VPN config as our own.

    Only ever called from ``maybe_auto_connect`` when this drone has **no
    usable config of its own** (``validate_ready()`` already failed) -- this
    never overrides an existing local configuration, deliberate or otherwise.

    Tries every paired peer, in ``local_network.paired_peers()``'s own order,
    stopping at the first one that is both sharing (``GET /peer/vpn/config``
    returns 200) *and* actively ``connected`` right now -- a peer that is
    merely configured-to-share but not currently connected is skipped, since
    a live tunnel is the signal its credentials genuinely work. This also
    avoids every drone in a freshly-booted fleet racing to adopt from an
    equally fresh, equally unconnected peer. Unreachable/offline peers and
    peers not sharing are silently skipped, not errors -- there may be many
    paired peers and only one needs to work. Returns True on the first
    successful import.
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
            payload, _address = _peer_get_json_for_peer(peer, "/v1/api/peer/vpn/config", settings, peer_id=peer_id)
        except Exception:
            continue  # offline, not paired on this address, sharing off, no config, etc.
        if not isinstance(payload, dict) or not payload.get("connected"):
            continue
        peer_name = str(peer.get("name") or peer.get("hostname") or peer_id)
        try:
            import_from_peer(settings, payload, source_peer_id=peer_id, source_peer_name=peer_name)
        except Exception as error:
            print(
                f"Swarm VPN bootstrap: failed to adopt the config shared by {peer_name}: {error.__class__.__name__}: {error}",
                file=sys.stderr, flush=True,
            )
            continue
        print(f"Swarm VPN bootstrap: adopted the VPN configuration shared by {peer_name}", file=sys.stdout, flush=True)
        return True
    return False


def maybe_auto_connect(settings: Settings) -> None:
    """Best-effort auto-connect on Drone startup whenever a ready-to-use
    configuration already exists (config + credentials + openvpn installed) --
    being configured is the only condition; there is no separate opt-in flag.

    If this drone has no usable config of its own, it first tries to adopt
    one shared by a paired peer that is actively connected right now (see
    ``bootstrap_vpn_from_swarm``) -- a single pass over paired peers, not a
    background search: this is a startup behavior, matching "start the VPN on
    startup" rather than an ongoing hunt. If nothing turns up (or this drone
    already has its own config), that step is a no-op either way.

    Retries a few times with a short delay: a fresh boot can hit transient
    conditions (network, ``/dev/net/tun``, or an external drive not mounted
    yet) that resolve moments later on their own. A single silent attempt
    previously meant a boot-time hiccup left VPN disconnected until a human
    noticed and manually reconnected -- the exact complaint that prompted this
    retry behavior. Every outcome (success after N attempts, or giving up) is
    logged, unlike the single-shot version this replaced, which discarded
    ``connect()``'s result entirely on both success and failure.

    Never raises -- a failed auto-connect must not prevent the Drone app
    (and therefore the rest of the admin UI) from starting.
    """
    try:
        if validate_ready(settings):
            bootstrap_vpn_from_swarm(settings)
        if validate_ready(settings):
            return
        result: dict = {}
        for attempt in range(1, VPN_AUTO_CONNECT_MAX_ATTEMPTS + 1):
            result = connect(settings)
            if result.get("status") in ("connecting", "already_running"):
                if attempt > 1:
                    print(
                        f"VPN auto-connect succeeded on attempt {attempt}/{VPN_AUTO_CONNECT_MAX_ATTEMPTS}",
                        file=sys.stdout, flush=True,
                    )
                return
            if attempt < VPN_AUTO_CONNECT_MAX_ATTEMPTS:
                time.sleep(VPN_AUTO_CONNECT_RETRY_DELAY_SECONDS)
        errors = "; ".join(result.get("errors") or ["unknown error"])
        print(
            f"VPN auto-connect on startup failed after {VPN_AUTO_CONNECT_MAX_ATTEMPTS} attempts: {errors}",
            file=sys.stderr, flush=True,
        )
    except Exception as error:
        print(f"VPN auto-connect on startup failed: {error.__class__.__name__}: {error}", file=sys.stderr, flush=True)


def set_sharing_enabled(settings: Settings, enabled: bool) -> dict:
    if enabled and _load_state(settings)["source_peer_id"]:
        raise ValueError(
            "This VPN configuration was imported from another drone and cannot be re-shared. "
            "Only the drone that originally uploaded it can share it with the swarm."
        )
    state = _save_state(settings, sharing_enabled=bool(enabled))
    return {"sharing_enabled": state["sharing_enabled"]}


def set_self_heal_enabled(settings: Settings, enabled: bool) -> dict:
    state = _save_state(settings, self_heal_enabled=bool(enabled))
    return {"self_heal_enabled": state["self_heal_enabled"]}


# ----------------------------------------------------------------- status


def _tunnel_ip(interface: str = "tun0") -> Optional[str]:
    tool = shutil.which("ip")
    if not tool:
        return None
    try:
        result = subprocess.run([tool, "-4", "addr", "show", interface], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", result.stdout or "")
    return match.group(1) if match else None


def check_public_ip() -> dict:
    """On-demand outbound check (not polled automatically) -- see module docstring."""
    tool = shutil.which("curl")
    if not tool:
        return {"error": "curl is not installed on this system"}
    try:
        result = subprocess.run(
            [tool, "-4", "-s", "--max-time", str(int(VPN_PUBLIC_IP_CHECK_TIMEOUT_SECONDS)), "https://ipinfo.io/ip"],
            capture_output=True,
            text=True,
            timeout=VPN_PUBLIC_IP_CHECK_TIMEOUT_SECONDS + 2,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"error": str(error)}
    ip = (result.stdout or "").strip()
    if result.returncode != 0 or not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
        return {"error": "Could not determine the public IP right now."}
    return {"ip": ip, "checked_at": _now_iso()}


def _log_lines(settings: Settings, count: int) -> List[str]:
    path = log_path(settings)
    if not path.is_file():
        return []
    return _tail_lines(path, count, max_bytes=VPN_LOG_TAIL_MAX_BYTES)


def status(settings: Settings) -> dict:
    state = _load_state(settings)
    binary = find_openvpn_binary()
    installed = binary is not None
    pid = _find_running_openvpn_pid(config_path(settings)) if installed else None
    detection_lines = _log_lines(settings, VPN_LOG_TAIL_LINES_FOR_DETECTION) if pid is not None else []
    detection_text = "\n".join(detection_lines)

    connected_at = state["connected_at"]
    message = ""
    if pid is None:
        connection_state = "disconnected"
        if connected_at is not None:
            state = _save_state(settings, connected_at=None)
            connected_at = None
    elif _SUCCESS_MARKER in detection_text:
        connection_state = "connected"
        if connected_at is None:
            connected_at = _now_iso()
            state = _save_state(settings, connected_at=connected_at)
    else:
        failure_line = next((line for line in reversed(detection_lines) if any(marker in line for marker in _FAILURE_MARKERS)), None)
        if failure_line:
            connection_state = "error"
            message = failure_line.strip()
        else:
            connection_state = "connecting"

    tunnel_ip = _tunnel_ip() if connection_state == "connected" else None
    connected_duration_seconds = None
    if connection_state == "connected" and connected_at:
        try:
            started = datetime.fromisoformat(connected_at)
            connected_duration_seconds = max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
        except ValueError:
            connected_duration_seconds = None

    return {
        "status": connection_state,
        "message": message,
        "installed": installed,
        "binary_path": binary,
        "pid": pid,
        "has_config": state["has_config"],
        "config_filename": state["config_filename"],
        "remotes": state["remotes"],
        "has_credentials": state["has_credentials"],
        "username": state["username"],
        "sharing_enabled": state["sharing_enabled"],
        "source_peer_id": state["source_peer_id"],
        "source_peer_name": state["source_peer_name"],
        "revoked_reason": state["revoked_reason"],
        "revoked_at": state["revoked_at"],
        "self_heal_enabled": state["self_heal_enabled"],
        "self_heal_last_at": state["self_heal_last_at"],
        "self_heal_last_reason": state["self_heal_last_reason"],
        "self_heal_recent_count": len(_prune_self_heal_attempts(state["self_heal_attempts"], datetime.now(timezone.utc))),
        "connected_at": connected_at,
        "connected_duration_seconds": connected_duration_seconds,
        "tunnel_ip": tunnel_ip,
        "tunnel_interface": "tun0",
        "validation_errors": validate_ready(settings),
        "log_available": log_path(settings).is_file(),
        "log_tail": _log_lines(settings, VPN_LOG_TAIL_LINES_FOR_DISPLAY),
    }


# ------------------------------------------------------------ peer sharing


def export_payload(settings: Settings) -> Optional[dict]:
    """This drone's config (+ credentials, if saved) for a paired peer to pull.

    Returns None when sharing is off or there is nothing to share yet -- the
    caller (the ``/peer/vpn/config`` handler) turns that into a 404. Unlike
    every other read path in this module, credentials are included here on
    purpose: this payload only ever travels Drone-to-Drone over the existing
    cert-pinned mTLS ``/peer/*`` channel (never to a browser), gated by the
    caller's own peer-pairing check plus ``sharing_enabled`` being explicitly
    turned on -- the receiving Drone re-applies the normal "never return the
    password to a browser" rule on its own admin API once imported.
    """
    state = _load_state(settings)
    # The source_peer_id check is redundant with set_sharing_enabled() refusing
    # to ever turn sharing on for an imported config -- kept here too since
    # this is the actual point data would leave the drone, and that is the
    # right place to enforce "only the original creator can share" even if a
    # future bug ever let sharing_enabled get set some other way.
    if not state["sharing_enabled"] or not state["has_config"] or state["source_peer_id"]:
        return None
    cfg_path = config_path(settings)
    if not cfg_path.is_file():
        return None
    payload = {
        "config_filename": state["config_filename"] or OPENVPN_CONFIG_FILENAME,
        "config_text": cfg_path.read_text(encoding="utf-8"),
        "remotes": state["remotes"],
        "has_credentials": False,
        # Whether *this* drone's own tunnel is actually up right now -- purely
        # additive: the serve gate above (sharing_enabled + has_config) is
        # unchanged, so the existing manual "Pull Configuration" flow behaves
        # exactly as before regardless of this value. It exists for
        # bootstrap_vpn_from_swarm() below, which only wants to adopt a config
        # from a peer that's proven to actually work right now, not merely
        # configured to share.
        "connected": status(settings).get("status") == "connected",
    }
    if state["has_credentials"]:
        auth_file = auth_path(settings)
        if auth_file.is_file():
            lines = auth_file.read_text(encoding="utf-8").splitlines()
            if len(lines) >= 2:
                payload.update(has_credentials=True, username=lines[0], password=lines[1])
    return payload


def import_from_peer(settings: Settings, payload: dict, *, source_peer_id: str, source_peer_name: str = "") -> dict:
    """Adopt a peer's exported VPN config (+ credentials, if included) as our own.

    Deliberately reuses ``save_uploaded_config``/``save_credentials`` unchanged
    rather than writing the peer's files directly: ``save_uploaded_config`` runs
    the peer's config text back through ``rewrite_ovpn_config`` against *our own*
    ``auth_path()``, so the peer's ``auth-user-pass`` line (pointing at *their*
    install path) is correctly re-pointed at ours -- correct regardless of
    whether the two Drones share the same install root, with no separate
    peer-import rewrite logic to keep in sync with the upload path.

    ``source_peer_id`` is supplied by the *caller* (the drone_id it just
    successfully mTLS-authenticated against), never trusted from the payload
    itself -- this becomes the config's permanent provenance marker, which
    ``set_sharing_enabled``/``export_payload`` check to enforce single-hop-only
    sharing: an imported config can never itself be re-shared, only the
    original creator's can. ``save_uploaded_config`` resets provenance to
    "self-owned" as part of its own "fresh write" semantics, so it must be
    called first and this function re-applies the real provenance immediately
    after, in the same call.
    """
    source_peer_id = str(source_peer_id or "").strip()
    if not source_peer_id:
        raise ValueError("source_peer_id is required to import a peer's VPN configuration")
    payload = payload if isinstance(payload, dict) else {}
    config_text = str(payload.get("config_text") or "")
    if not config_text.strip():
        raise ValueError("peer did not return a VPN configuration")
    filename = str(payload.get("config_filename") or OPENVPN_CONFIG_FILENAME)
    result = save_uploaded_config(settings, filename, config_text.encode("utf-8"))
    username = str(payload.get("username") or "")
    password = str(payload.get("password") or "")
    if payload.get("has_credentials") and username and password:
        save_credentials(settings, username, password)
        result = {**result, "credentials_imported": True}
    else:
        result = {**result, "credentials_imported": False}
    _save_state(settings, source_peer_id=source_peer_id, source_peer_name=str(source_peer_name or "").strip())
    return result


_SHARING_REVOKED_PEER_OFF = "The peer sharing this VPN configuration turned off sharing, so these credentials were removed."
_SHARING_REVOKED_PEER_GONE = "The peer that shared this VPN configuration is no longer paired, so these credentials were removed."


def _revoke_local_credentials(settings: Settings, reason: str) -> None:
    """Disconnect (if connected) and remove imported credentials.

    The config file and ``source_peer_id`` deliberately survive this: leaving
    provenance in place is what keeps ``set_sharing_enabled``/``export_payload``
    refusing to ever let this now-orphaned config be re-shared, even though it
    has no working credentials anymore. Only a genuine fresh
    ``save_uploaded_config`` call (a real new upload) can clear provenance.
    """
    disconnect(settings)
    try:
        auth_path(settings).unlink(missing_ok=True)
    except OSError:
        pass
    _save_state(settings, has_credentials=False, username="", revoked_reason=reason, revoked_at=_now_iso())


def check_sharing_revocation(settings: Settings) -> bool:
    """If this config was imported from a peer, verify that peer still shares
    it; revoke (disconnect + remove credentials) if not. Returns True iff a
    revocation just happened.

    Only two answers count as revocation: the source drone explicitly says
    "not sharing" (``/peer/vpn/config`` 404, whether because it turned sharing
    off or removed its own config), or it is no longer in this drone's own
    paired-peer list at all. Anything else -- unreachable, timeout, any other
    HTTP status -- is treated as transient and changes nothing: a flaky or
    temporarily offline peer must never strip a working VPN setup. Never
    raises (intended to run unattended on a background poller).
    """
    try:
        state = _load_state(settings)
        source_peer_id = state["source_peer_id"]
        if not source_peer_id or not state["has_credentials"]:
            return False
        try:
            from ..transfer import local_network as _local_network
            from ..transfer.peer_connectivity import _peer_get_json_for_peer
        except ImportError:  # pragma: no cover - direct script execution fallback
            from transfer import local_network as _local_network  # type: ignore
            from transfer.peer_connectivity import _peer_get_json_for_peer  # type: ignore
        peer = _local_network.get_paired_peer(settings, source_peer_id)
        if not peer:
            _revoke_local_credentials(settings, _SHARING_REVOKED_PEER_GONE)
            return True
        try:
            _peer_get_json_for_peer(peer, "/v1/api/peer/vpn/config", settings, peer_id=source_peer_id)
            return False
        except HTTPError as error:
            if error.code == 404:
                _revoke_local_credentials(settings, _SHARING_REVOKED_PEER_OFF)
                return True
            return False
        except Exception:
            return False
    except Exception as error:
        print(f"VPN sharing revocation check failed: {error.__class__.__name__}: {error}", file=sys.stderr, flush=True)
        return False


def run_sharing_revocation_poller(settings: Settings) -> None:
    """Forever-loop checking whether an imported config's sharing was revoked.

    Intended to run on its own daemon thread, started by the caller exactly
    like ``maybe_auto_connect`` is (see ``create_server()``) -- this function
    itself is threading-agnostic and never returns. A periodic *background*
    check is the only way to learn about revocation at all: Drones are
    outbound-only with no push channel, and unlike ``status()`` this cannot be
    a passive on-demand recompute, because the whole point is to disconnect
    and wipe credentials even if nobody is looking at the VPN page.
    """
    interval = max(30.0, float(os.environ.get("DRONE_VPN_SHARING_CHECK_INTERVAL_SECONDS", "300")))
    while True:
        time.sleep(interval)
        check_sharing_revocation(settings)


# ------------------------------------------------------------ self-heal


def reconnect(settings: Settings) -> dict:
    """Disconnect (if running) and connect fresh, as one operation.

    No extra locking here on top of what ``connect``/``disconnect`` already do
    individually -- ``_CONNECT_LOCK`` is a plain, non-reentrant ``Lock``, so
    wrapping both calls in it too would deadlock.
    """
    disconnect(settings)
    return connect(settings)


def _recent_log_flood(settings: Settings) -> Optional[str]:
    """Return a description if the most recent log lines are dominated by a
    repeating decrypt/replay error, else None.

    A healthy, already-connected tunnel logs almost nothing -- openvpn only
    writes on events -- so this only looks at a short, *recent* window rather
    than the whole log: a burst that already happened and stopped scrolls out
    of it on its own, exactly like the real incident that prompted this
    (498 replay errors accumulated over hours would otherwise permanently look
    "unhealthy" long after the fact if we scanned the whole log).
    """
    lines = _log_lines(settings, _UNHEALTHY_LOG_WINDOW_LINES)
    if not lines:
        return None
    matches = sum(1 for line in lines if any(pattern in line for pattern in _UNHEALTHY_LOG_PATTERNS))
    if matches >= _UNHEALTHY_LOG_MIN_MATCHES:
        return f"{matches} decrypt/replay errors in the last {len(lines)} log lines"
    return None


def _self_heal_reason(snapshot: dict, settings: Settings) -> Optional[str]:
    """Why (if at all) the current status warrants a self-heal reconnect.

    Deliberately never fires on "disconnected" -- that's either a user who
    disconnected on purpose, or the sharing-revocation poller's own intentional
    disconnect after credentials were wiped (see check_sharing_revocation);
    self-heal must not fight either of those. "connecting"/"connected" are
    both checked for the log-flood pattern because the real incident that
    prompted this feature showed as "connecting" (the flood had pushed the
    success marker out of the status detector's own tail window) rather than
    the "error" status a human might expect.
    """
    status_value = snapshot.get("status")
    if status_value == "disconnected":
        return None
    if status_value == "error":
        return str(snapshot.get("message") or "connection error")
    if status_value in ("connecting", "connected") and snapshot.get("pid"):
        return _recent_log_flood(settings)
    return None


def _prune_self_heal_attempts(attempts: List[str], now: datetime) -> List[str]:
    cutoff = now - timedelta(seconds=VPN_SELF_HEAL_WINDOW_SECONDS)
    kept = []
    for raw in attempts:
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if parsed >= cutoff:
            kept.append(raw)
    return kept


def check_and_self_heal(settings: Settings) -> Optional[dict]:
    """Periodic watchdog: reconnect if the VPN is unhealthy for any detected
    reason (see ``_self_heal_reason``). Self-healing is opt-out, not opt-in --
    ``self_heal_enabled`` defaults to True.

    Rate-limited two ways so this can never hammer the VPN provider or spin
    the CPU: at most one reconnect per ``VPN_SELF_HEAL_MIN_INTERVAL_SECONDS``,
    and at most ``VPN_SELF_HEAL_MAX_ATTEMPTS_PER_WINDOW`` within a rolling
    ``VPN_SELF_HEAL_WINDOW_SECONDS`` window. The window cap is a *temporary*
    backoff, not a permanent give-up -- old attempts age out on their own, so
    a connection that's still bad an hour from now gets tried again rather
    than staying disconnected forever; see the drone-vpn-management skill for
    why this is deliberately not smarter than that (e.g. it does not try a
    different server -- reconnecting to the same broken setup will keep
    re-triggering until the underlying cause is actually fixed).

    Never raises -- intended for an unattended background poller.
    """
    try:
        state = _load_state(settings)
        if not state["self_heal_enabled"]:
            return None
        if not state["has_config"] or not state["has_credentials"]:
            return None

        reason = _self_heal_reason(status(settings), settings)
        if reason is None:
            return None

        now = datetime.now(timezone.utc)
        last_at = state["self_heal_last_at"]
        if last_at:
            try:
                elapsed = (now - datetime.fromisoformat(last_at)).total_seconds()
                if elapsed < VPN_SELF_HEAL_MIN_INTERVAL_SECONDS:
                    return None
            except ValueError:
                pass

        attempts = _prune_self_heal_attempts(state["self_heal_attempts"], now)
        if len(attempts) >= VPN_SELF_HEAL_MAX_ATTEMPTS_PER_WINDOW:
            _save_state(settings, self_heal_attempts=attempts)
            print(
                f"VPN self-heal: pausing after {len(attempts)} reconnects in "
                f"{int(VPN_SELF_HEAL_WINDOW_SECONDS / 60)} min -- most recent cause: {reason}",
                file=sys.stderr, flush=True,
            )
            return {"action": "paused", "reason": reason}

        attempts.append(now.replace(microsecond=0).isoformat())
        result = reconnect(settings)
        _save_state(
            settings,
            self_heal_last_at=now.replace(microsecond=0).isoformat(),
            self_heal_last_reason=reason,
            self_heal_attempts=attempts,
        )
        print(f"VPN self-heal: reconnected after detecting: {reason}", file=sys.stdout, flush=True)
        return {"action": "reconnected", "reason": reason, "result": result}
    except Exception as error:
        print(f"VPN self-heal check failed: {error.__class__.__name__}: {error}", file=sys.stderr, flush=True)
        return None


def run_self_heal_poller(settings: Settings) -> None:
    """Forever-loop calling check_and_self_heal on a fixed cadence.

    Started as its own daemon thread from create_server(), same pattern as
    run_sharing_revocation_poller -- a separate, narrowly-scoped background
    loop per concern rather than one combined poller, matching this module's
    existing style.
    """
    while True:
        time.sleep(VPN_SELF_HEAL_CHECK_INTERVAL_SECONDS)
        check_and_self_heal(settings)
