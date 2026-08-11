"""GitHub-Issues "enrollment mailbox" -- notifies the fleet owner when an
unattended, cross-network Drone needs Tailscale approval, without any secret
ever crossing the network to that Drone.

The problem this solves: a Drone shipped to a location with no physical
access and no existing remote channel (e.g. a rented property 50 miles away)
has no way to receive a Tailscale auth key at all -- there's no admin UI to
paste one into, because reaching that admin UI cross-network is exactly what
Tailscale enrollment is needed for in the first place.

The way out is that Tailscale doesn't require a pre-shared secret to enroll:
``tailnet_service.tailnet_enroll_interactive()`` runs ``tailscale up`` with
no auth key, which prints a one-time ``https://login.tailscale.com/...`` URL
a human approves from *any* browser, anywhere. Nothing sensitive has to
reach the Drone for that to work -- the only remaining problem is getting
that URL *out* to the fleet owner from a Drone they otherwise can't reach.

This module is that "out" channel: every Drone already talks to github.com
outbound for its own release checks, so it's a connection guaranteed to
exist with zero per-device setup. A private GitHub repo (created once by the
fleet owner) acts as a mailbox; a narrowly-scoped GitHub token (Issues:
read/write on that one repo only -- nothing else) lets a Drone open an issue
containing the login URL. GitHub's own notification system (which the owner
already has, since this fleet's whole CI/CD lives on GitHub) does the actual
"tell a human" step -- no SMTP, no third-party webhook service, no new
always-on infrastructure.

The token is fleet-wide, not per-device, so it's configured the same way as
VPN/SMTP/Tailscale credentials: entered once into a Drone's admin UI while
it's still reachable (typically before a Drone ships anywhere), and follows
the same single-hop peer-sharing model as those three so a batch of Drones
set up together can propagate it without retyping. Its blast radius is
deliberately tiny even if it leaks: it can only create/edit/close issues in
one private repo the owner chose for this, nothing else on their GitHub
account.

State lives in the same plaintext-JSON state store already accepted for
VPN/SMTP/Tailscale secrets -- not a new, weaker storage tradeoff.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional

try:
    from ..common.http_errors import _format_http_error
    from ..common.settings import Settings
    from ..storage.state_store import database_path as _state_database_path
    from ..storage.state_store import load_payload as _load_state_payload
    from ..storage.state_store import save_payload as _save_state_payload
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.http_errors import _format_http_error  # type: ignore
    from common.settings import Settings  # type: ignore
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import load_payload as _load_state_payload  # type: ignore
    from storage.state_store import save_payload as _save_state_payload  # type: ignore

MAILBOX_STATE_NAMESPACE = "enrollment_mailbox.json"
MAILBOX_SHARING_CHECK_INTERVAL_SECONDS = float(os.environ.get("DRONE_MAILBOX_SHARING_CHECK_INTERVAL_SECONDS", "300"))
MAILBOX_POLL_INTERVAL_SECONDS = float(os.environ.get("DRONE_MAILBOX_POLL_INTERVAL_SECONDS", "900"))

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_TIMEOUT_SECONDS = 15.0
GITHUB_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# Fields carried in the shared/exported payload (single-hop peer-to-peer,
# mirrors smtp_manager.py's _SHARED_FIELDS). Local-only fields (tracked issue
# state, last login URL) are deliberately excluded -- a peer that pulls this
# token starts its own, independent tracking of its own issue.
_SHARED_FIELDS = ("github_token", "github_repo")


class MailboxNotifyError(Exception):
    """Raised internally by the GitHub API helpers; always caught and turned
    into a status dict by check_and_notify_if_needed(), never left to
    propagate to a poller thread."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_state(settings: Settings) -> dict:
    stored = _load_state_payload(_state_database_path(settings.userdata_root), MAILBOX_STATE_NAMESPACE, {})
    stored = stored if isinstance(stored, dict) else {}
    return {
        "has_config": bool(stored.get("has_config", False)),
        "github_repo": str(stored.get("github_repo") or ""),
        "github_token": str(stored.get("github_token") or ""),
        # Sharing/provenance -- mirrors smtp_manager._load_state() exactly.
        "sharing_enabled": bool(stored.get("sharing_enabled", False)),
        "source_peer_id": str(stored.get("source_peer_id") or ""),
        "source_peer_name": str(stored.get("source_peer_name") or ""),
        "revoked_reason": str(stored.get("revoked_reason") or ""),
        "revoked_at": stored.get("revoked_at"),
        # Local-only tracking of the currently-open issue for this device,
        # never shared -- an importing peer tracks its own issue independently.
        "tracked_issue_number": stored.get("tracked_issue_number"),
        "tracked_issue_url": str(stored.get("tracked_issue_url") or ""),
        "last_login_url": str(stored.get("last_login_url") or ""),
        "last_notified_at": stored.get("last_notified_at"),
        "last_check_status": str(stored.get("last_check_status") or ""),
        "last_check_error": str(stored.get("last_check_error") or ""),
        "last_check_at": stored.get("last_check_at"),
    }


def _save_state(settings: Settings, **updates) -> dict:
    state = _load_state(settings)
    state.update(updates)
    _save_state_payload(_state_database_path(settings.userdata_root), MAILBOX_STATE_NAMESPACE, state)
    return state


def _sanitized(state: dict) -> dict:
    """Strip the token before this ever reaches an /admin/* response body --
    mirrors the VPN/SMTP rule that a secret never returns to a browser."""
    sanitized = dict(state)
    sanitized["has_token"] = bool(sanitized.pop("github_token", ""))
    return sanitized


# ------------------------------------------------------------------ config


def get_settings(settings: Settings) -> dict:
    return _sanitized(_load_state(settings))


def update_settings(settings: Settings, payload: dict) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    current = _load_state(settings)
    repo = str(payload.get("github_repo", current["github_repo"]) or "").strip()
    if not repo:
        raise ValueError("GitHub repo is required (format: owner/repo)")
    if not GITHUB_REPO_PATTERN.match(repo):
        raise ValueError("GitHub repo must look like owner/repo")
    token = str(payload.get("github_token") or "").strip()
    if not token and not current["github_token"]:
        raise ValueError("A GitHub token is required the first time this is configured")
    updates = {
        "github_repo": repo,
        "has_config": True,
        # A direct settings save is always "self-owned" -- clears any prior
        # peer-import provenance, exactly like smtp_manager.update_settings().
        "source_peer_id": "",
        "source_peer_name": "",
        "revoked_reason": "",
        "revoked_at": None,
        # A changed repo/token invalidates any issue tracked against the old
        # mailbox -- don't try to close/reuse an issue number that may not
        # even exist in the new repo.
        "tracked_issue_number": None,
        "tracked_issue_url": "",
    }
    # Token is optional on update (blank = "keep the existing value"), same
    # rule as SMTP's password -- the admin UI never echoes the stored token
    # back into the form.
    if token:
        updates["github_token"] = token
    state = _save_state(settings, **updates)
    return _sanitized(state)


# ------------------------------------------------------------ peer sharing


def set_sharing_enabled(settings: Settings, enabled: bool) -> dict:
    if enabled and _load_state(settings)["source_peer_id"]:
        raise ValueError(
            "This mailbox configuration was imported from another drone and cannot be re-shared. "
            "Only the drone that originally set it up can share it with the swarm."
        )
    state = _save_state(settings, sharing_enabled=bool(enabled))
    return {"sharing_enabled": state["sharing_enabled"]}


def export_payload(settings: Settings) -> Optional[dict]:
    """This drone's GitHub mailbox token+repo for a paired peer to pull.

    None means "don't share" -- the caller (``GET /peer/mailbox/config``)
    turns that into a 404. Mirrors ``smtp_manager.export_payload()``: only
    ever served over the cert-pinned mTLS ``/peer/*`` channel, gated by
    pairing (checked by the caller) plus ``sharing_enabled`` here, never
    returned to a browser.
    """
    state = _load_state(settings)
    if not state["sharing_enabled"] or not state["has_config"] or state["source_peer_id"]:
        return None
    return {field: state[field] for field in _SHARED_FIELDS}


def import_from_peer(settings: Settings, payload: dict, *, source_peer_id: str, source_peer_name: str = "") -> dict:
    """Adopt a peer's exported mailbox token+repo as our own.

    Reuses ``update_settings()`` unchanged rather than writing state
    directly, same reasoning as ``smtp_manager.import_from_peer()``.
    """
    source_peer_id = str(source_peer_id or "").strip()
    if not source_peer_id:
        raise ValueError("source_peer_id is required to import a peer's mailbox configuration")
    payload = payload if isinstance(payload, dict) else {}
    update_payload = {field: payload.get(field) for field in _SHARED_FIELDS if payload.get(field)}
    result = update_settings(settings, update_payload)
    _save_state(settings, source_peer_id=source_peer_id, source_peer_name=str(source_peer_name or "").strip())
    return result


def bootstrap_mailbox_from_swarm(settings: Settings) -> bool:
    """Adopt a paired peer's shared mailbox config as our own.

    Only ever called when this drone has no usable config of its own
    (mirrors ``smtp_manager.bootstrap_smtp_from_swarm()``) -- never overrides
    an existing local configuration. Any paired peer actively sharing a
    complete config qualifies; the first one found (in
    ``local_network.paired_peers()``'s own order) wins.
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
            payload, _address = _peer_get_json_for_peer(peer, "/v1/api/peer/mailbox/config", settings, peer_id=peer_id)
        except Exception:
            continue  # offline, not paired on this address, sharing off, no config, etc.
        if not isinstance(payload, dict) or not payload.get("github_repo"):
            continue
        peer_name = str(peer.get("name") or peer.get("hostname") or peer_id)
        try:
            import_from_peer(settings, payload, source_peer_id=peer_id, source_peer_name=peer_name)
        except Exception as error:
            print(
                f"Swarm mailbox bootstrap: failed to adopt the config shared by {peer_name}: "
                f"{error.__class__.__name__}: {error}",
                file=sys.stderr, flush=True,
            )
            continue
        print(f"Swarm mailbox bootstrap: adopted the mailbox configuration shared by {peer_name}", file=sys.stdout, flush=True)
        return True
    return False


def maybe_bootstrap_mailbox(settings: Settings) -> None:
    """Best-effort, called once from ``create_server()`` startup, mirroring
    ``smtp_manager.maybe_bootstrap_smtp()``. Never raises."""
    try:
        if not _load_state(settings)["has_config"]:
            bootstrap_mailbox_from_swarm(settings)
    except Exception as error:
        print(f"Mailbox swarm bootstrap failed: {error.__class__.__name__}: {error}", file=sys.stderr, flush=True)


_SHARING_REVOKED_PEER_OFF = "The peer sharing this mailbox configuration turned off sharing, so it was removed from this drone."
_SHARING_REVOKED_PEER_GONE = "The peer that shared this mailbox configuration is no longer paired, so it was removed from this drone."


def _revoke_local_credentials(settings: Settings, reason: str) -> None:
    """Wipe the imported token (not the repo, not ``source_peer_id``) --
    mirrors ``smtp_manager._revoke_local_credentials()``: leaving provenance
    in place keeps ``set_sharing_enabled()``/``export_payload()`` refusing to
    ever let this now-orphaned config be re-shared."""
    _save_state(settings, github_token="", revoked_reason=reason, revoked_at=_now_iso())


def check_sharing_revocation(settings: Settings) -> bool:
    """Mirrors ``smtp_manager.check_sharing_revocation()`` exactly -- only an
    explicit peer 404 (sharing off / no config) or "no longer paired"
    revokes; anything else is transient and changes nothing. Never raises."""
    try:
        state = _load_state(settings)
        source_peer_id = state["source_peer_id"]
        if not source_peer_id or not state["has_config"]:
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
            _peer_get_json_for_peer(peer, "/v1/api/peer/mailbox/config", settings, peer_id=source_peer_id)
            return False
        except urllib.error.HTTPError as error:
            if error.code == 404:
                _revoke_local_credentials(settings, _SHARING_REVOKED_PEER_OFF)
                return True
            return False
        except Exception:
            return False
    except Exception as error:
        print(f"Mailbox sharing revocation check failed: {error.__class__.__name__}: {error}", file=sys.stderr, flush=True)
        return False


def run_sharing_revocation_poller(settings: Settings) -> None:
    """Forever-loop, mirrors ``smtp_manager.run_sharing_revocation_poller()``."""
    interval = max(30.0, MAILBOX_SHARING_CHECK_INTERVAL_SECONDS)
    while True:
        time.sleep(interval)
        check_sharing_revocation(settings)


# --------------------------------------------------------- GitHub REST API


def _github_request(token: str, method: str, path: str, *, params: Optional[dict] = None, json_body: Optional[dict] = None) -> object:
    """Minimal stdlib JSON client for GitHub's REST API -- only ever used to
    create/read/close issues in the one repo this token is scoped to."""
    url = f"{GITHUB_API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "batocera-drone-enrollment-mailbox",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=GITHUB_API_TIMEOUT_SECONDS) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def _device_label(settings: Settings) -> str:
    """Human-identifiable label for the GitHub issue title/body -- hostname +
    the same unique device_id shown as "Machine ID" in the Debug tile, same
    convention as smtp_manager._drone_label()."""
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = ""
    device_id = settings.device_id or ""
    if hostname and device_id:
        return f"{hostname} ({device_id})"
    return device_id or hostname or "unknown drone"


def _device_label_slug(settings: Settings) -> str:
    """A GitHub-label-safe identifier for this exact device, used to look up
    (and avoid duplicating) this device's own open issue. GitHub auto-creates
    a label the first time it's used on an issue -- no separate setup step."""
    device_id = str(settings.device_id or "unknown").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", device_id).strip("-") or "unknown"
    return f"enroll-{slug}"


def _issue_body(settings: Settings, login_url: str) -> str:
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = ""
    return (
        "This Drone is not yet connected to your tailnet and cannot be reached "
        "any other way.\n\n"
        f"Approve it here (any browser, anywhere):\n\n{login_url}\n\n"
        f"Hostname: {hostname}\n"
        f"Device ID: {settings.device_id}\n\n"
        "This issue closes automatically once the drone is enrolled. If the "
        "link above has expired, just close this issue -- a fresh one will be "
        "opened with a new link on the next check."
    )


def check_and_notify_if_needed(settings: Settings) -> dict:
    """Called periodically by the poller (and on-demand via the admin
    "check now" button). If this drone is not tailnet-enrolled and the
    mailbox is configured, ensures exactly one open GitHub issue exists with
    a working approval link. If it *is* enrolled and an issue is still
    tracked, closes it. Never raises -- every failure path returns a status
    dict instead, and is also recorded into local state for the admin UI.
    """
    result = {"status": "skipped", "reason": "not configured"}
    try:
        state = _load_state(settings)
        if not state["has_config"]:
            return result
        try:
            from .tailnet_service import tailnet_enroll_interactive, tailnet_status
        except ImportError:  # pragma: no cover - direct script execution fallback
            from device.tailnet_service import tailnet_enroll_interactive, tailnet_status  # type: ignore
        token, repo = state["github_token"], state["github_repo"]
        if tailnet_status().get("enrolled"):
            if state["tracked_issue_number"]:
                try:
                    _github_request(
                        token, "POST", f"/repos/{repo}/issues/{state['tracked_issue_number']}/comments",
                        json_body={"body": "This drone is now connected to the tailnet."},
                    )
                    _github_request(token, "PATCH", f"/repos/{repo}/issues/{state['tracked_issue_number']}", json_body={"state": "closed"})
                except Exception:
                    pass  # best-effort tidy-up; not enrolling again over this
                _save_state(settings, tracked_issue_number=None, tracked_issue_url="")
            result = {"status": "already_enrolled"}
        else:
            result = _ensure_open_issue(settings, state, token, repo, tailnet_enroll_interactive)
    except Exception as error:
        result = {"status": "error", "error": f"{error.__class__.__name__}: {error}"}
    _save_state(settings, last_check_status=result.get("status", ""), last_check_error=result.get("error", ""), last_check_at=_now_iso())
    return result


def _ensure_open_issue(settings: Settings, state: dict, token: str, repo: str, tailnet_enroll_interactive) -> dict:
    tracked_number = state["tracked_issue_number"]
    if tracked_number:
        try:
            issue = _github_request(token, "GET", f"/repos/{repo}/issues/{tracked_number}")
            if isinstance(issue, dict) and str(issue.get("state") or "") == "open":
                return {"status": "already_pending", "issue_number": tracked_number}
        except Exception:
            pass  # deleted, inaccessible, or a transient error -- fall through and re-create
        _save_state(settings, tracked_issue_number=None, tracked_issue_url="")
    label = _device_label_slug(settings)
    try:
        existing = _github_request(token, "GET", f"/repos/{repo}/issues", params={"state": "open", "labels": label})
    except Exception as error:
        return {"status": "error", "error": f"GitHub lookup failed: {_format_http_error(error)}"}
    if isinstance(existing, list) and existing:
        found = existing[0]
        _save_state(settings, tracked_issue_number=found.get("number"), tracked_issue_url=str(found.get("html_url") or ""))
        return {"status": "already_pending", "issue_number": found.get("number")}
    try:
        login_url = tailnet_enroll_interactive()
    except Exception as error:
        return {"status": "error", "error": f"tailscale enrollment could not start: {error}"}
    try:
        issue = _github_request(
            token, "POST", f"/repos/{repo}/issues",
            json_body={"title": f"Tailscale enrollment needed: {_device_label(settings)}", "body": _issue_body(settings, login_url), "labels": [label]},
        )
    except Exception as error:
        return {"status": "error", "error": f"GitHub issue create failed: {_format_http_error(error)}"}
    _save_state(
        settings,
        tracked_issue_number=issue.get("number") if isinstance(issue, dict) else None,
        tracked_issue_url=str(issue.get("html_url") or "") if isinstance(issue, dict) else "",
        last_login_url=login_url,
        last_notified_at=_now_iso(),
    )
    return {"status": "notified", "issue_number": issue.get("number") if isinstance(issue, dict) else None}


def run_mailbox_poller(settings: Settings) -> None:
    """Forever-loop -- the codebase's usual "cron style job" shape (see
    ``smtp_manager.run_audit_email_digest_poller`` for the same pattern).
    Started as its own daemon thread from ``create_server()``."""
    interval = max(60.0, MAILBOX_POLL_INTERVAL_SECONDS)
    while True:
        time.sleep(interval)
        check_and_notify_if_needed(settings)
