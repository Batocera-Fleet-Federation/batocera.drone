"""SMTP email configuration: send notification-digest and test emails
from the Drone admin UI, with the same peer-to-peer credential sharing model
VPN uses (see ``device/vpn_manager.py``) -- configure once, share opt-in,
single-hop provenance, auto-revoke if the sharing peer turns it off, and a
default-on auto-pull for a freshly-set-up drone with no config of its own.

Unlike VPN there is no persistent connection to manage (no process, no PID,
no self-heal) -- sending mail is a one-shot ``smtplib`` call made on demand
(the Test Email button) or from the ~5-minute digest poller. Settings,
*including the password*, live entirely in one JSON blob via the same
``storage/state_store.py`` mechanism VPN's whole state dict already uses --
there is no OpenVPN-style ``auth-user-pass <file>`` requirement forcing a
separate on-disk credentials file here, so there isn't one. This repo has no
crypto library available either way (stdlib-only), so this is the same
plaintext-on-disk tradeoff VPN already makes and documents.

Notification-type toggles and the master ``smtp_enabled`` switch are
local-only and never travel with the shared/exported payload: each drone
runs its own digest poller against its own ``audit_log`` using its own
(possibly-adopted) credentials, so "which of *my* events get emailed" is
inherently per-drone, unlike the connection settings themselves.

Every outgoing email identifies the sending drone (hostname + the same
unique ``device_id`` shown as "Machine ID" in the Debug tile) in the From
display name, subject line, and body -- a swarm owner receiving digests from
several drones needs to tell them apart at a glance, without opening the
message.
"""

from __future__ import annotations

import os
import smtplib
import socket
import sys
import time
from datetime import datetime, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError

try:
    from ..common.settings import Settings
    from ..storage.state_store import database_path as _state_database_path
    from ..storage.state_store import load_payload as _load_state_payload
    from ..storage.state_store import save_payload as _save_state_payload
    from ..storage import audit_store as _audit_store
    from . import notifications as _notifications
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import load_payload as _load_state_payload  # type: ignore
    from storage.state_store import save_payload as _save_state_payload  # type: ignore
    from storage import audit_store as _audit_store  # type: ignore
    from device import notifications as _notifications  # type: ignore

SMTP_STATE_NAMESPACE = "smtp_manager.json"
SMTP_SEND_TIMEOUT_SECONDS = float(os.environ.get("DRONE_SMTP_SEND_TIMEOUT_SECONDS", "15"))
# An attachment (base64-inflated ~33%) can take far longer to transmit than a
# plain-text digest/test email over a modest home upload link -- give it real
# room rather than reusing the short default meant for a few KB of text.
SMTP_ATTACHMENT_SEND_TIMEOUT_SECONDS = float(os.environ.get("DRONE_SMTP_ATTACHMENT_SEND_TIMEOUT_SECONDS", "120"))
SMTP_SHARING_CHECK_INTERVAL_SECONDS = float(os.environ.get("DRONE_SMTP_SHARING_CHECK_INTERVAL_SECONDS", "300"))
AUDIT_EMAIL_POLL_INTERVAL_SECONDS = float(os.environ.get("DRONE_AUDIT_EMAIL_INTERVAL_SECONDS", "300"))
AUDIT_EMAIL_MAX_ITEMS_PER_DIGEST = 200

# Fields carried in the shared/exported payload (peer-to-peer, mirrors VPN's
# config+credentials). Local-only fields (smtp_enabled, notify.*, last_*) are
# deliberately excluded -- see the module docstring.
_SHARED_FIELDS = (
    "host", "port", "use_starttls", "use_ssl", "username", "password",
    "from_address", "recipient_email",
)


class SmtpSendError(Exception):
    """Raised by send_mail() on delivery failure -- callers turn this into a
    user-facing error string; never silently swallowed at that layer."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_state(settings: Settings) -> dict:
    stored = _load_state_payload(_state_database_path(settings.userdata_root), SMTP_STATE_NAMESPACE, {})
    stored = stored if isinstance(stored, dict) else {}
    notify_stored = stored.get("notify") if isinstance(stored.get("notify"), dict) else {}
    return {
        "has_config": bool(stored.get("has_config", False)),
        "host": str(stored.get("host") or ""),
        "port": int(stored.get("port") or 587),
        "use_starttls": bool(stored.get("use_starttls", True)),
        "use_ssl": bool(stored.get("use_ssl", False)),
        "username": str(stored.get("username") or ""),
        "password": str(stored.get("password") or ""),
        "from_address": str(stored.get("from_address") or ""),
        "recipient_email": str(stored.get("recipient_email") or ""),
        # Sharing/provenance -- mirrors vpn_manager._load_state() exactly.
        "sharing_enabled": bool(stored.get("sharing_enabled", False)),
        "source_peer_id": str(stored.get("source_peer_id") or ""),
        "source_peer_name": str(stored.get("source_peer_name") or ""),
        "revoked_reason": str(stored.get("revoked_reason") or ""),
        "revoked_at": stored.get("revoked_at"),
        # Local-only -- never shared, see module docstring.
        "smtp_enabled": bool(stored.get("smtp_enabled", True)),
        "notify": {
            event_type: bool(notify_stored.get(event_type, True)) for event_type in _notifications.EVENT_TYPES
        },
        "last_test_result": stored.get("last_test_result"),
        "last_test_at": stored.get("last_test_at"),
        "last_digest_sent_at": stored.get("last_digest_sent_at"),
        "last_digest_error": str(stored.get("last_digest_error") or ""),
    }


def _save_state(settings: Settings, **updates) -> dict:
    state = _load_state(settings)
    state.update(updates)
    _save_state_payload(_state_database_path(settings.userdata_root), SMTP_STATE_NAMESPACE, state)
    return state


def _sanitized(state: dict) -> dict:
    """Strip secrets before this ever reaches an /admin/* response body --
    mirrors the VPN rule that the password never returns to a browser."""
    sanitized = dict(state)
    sanitized["has_password"] = bool(sanitized.pop("password", ""))
    return sanitized


# ------------------------------------------------------------------ config


def get_settings(settings: Settings) -> dict:
    return _sanitized(_load_state(settings))


def update_settings(settings: Settings, payload: dict) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    current = _load_state(settings)
    host = str(payload.get("host", current["host"]) or "").strip()
    from_address = str(payload.get("from_address", current["from_address"]) or "").strip()
    recipient_email = str(payload.get("recipient_email", current["recipient_email"]) or "").strip()
    if not host:
        raise ValueError("SMTP host is required")
    if not from_address:
        raise ValueError("From address is required")
    if not recipient_email:
        raise ValueError("Recipient email is required -- this is where test and digest emails are sent")
    try:
        port = int(payload.get("port", current["port"]))
    except (TypeError, ValueError):
        raise ValueError("SMTP port must be a number")
    if not 1 <= port <= 65535:
        raise ValueError("SMTP port must be between 1 and 65535")
    updates = {
        "host": host,
        "port": port,
        "use_starttls": bool(payload.get("use_starttls", current["use_starttls"])),
        "use_ssl": bool(payload.get("use_ssl", current["use_ssl"])),
        "username": str(payload.get("username", current["username"]) or "").strip(),
        "from_address": from_address,
        "recipient_email": recipient_email,
        "has_config": True,
        # A direct settings save is always "self-owned" -- clears any prior
        # peer-import provenance, exactly like VPN's save_uploaded_config().
        "source_peer_id": "",
        "source_peer_name": "",
        "revoked_reason": "",
        "revoked_at": None,
    }
    # Password is optional on update (blank = "keep the existing value"),
    # since the admin UI never echoes the stored password back into the form.
    if str(payload.get("password") or "").strip():
        updates["password"] = str(payload["password"])
    state = _save_state(settings, **updates)
    return _sanitized(state)


def set_smtp_enabled(settings: Settings, enabled: bool) -> dict:
    state = _save_state(settings, smtp_enabled=bool(enabled))
    return {"smtp_enabled": state["smtp_enabled"]}


def update_notification_toggles(settings: Settings, payload: dict) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    merged = dict(_load_state(settings)["notify"])
    for event_type in _notifications.EVENT_TYPES:
        if event_type in payload:
            merged[event_type] = bool(payload[event_type])
    state = _save_state(settings, notify=merged)
    return {"notify": state["notify"]}


# ------------------------------------------------------------ peer sharing


def set_sharing_enabled(settings: Settings, enabled: bool) -> dict:
    if enabled and _load_state(settings)["source_peer_id"]:
        raise ValueError(
            "This SMTP configuration was imported from another drone and cannot be re-shared. "
            "Only the drone that originally set it up can share it with the swarm."
        )
    state = _save_state(settings, sharing_enabled=bool(enabled))
    return {"sharing_enabled": state["sharing_enabled"]}


def export_payload(settings: Settings) -> Optional[dict]:
    """This drone's SMTP settings for a paired peer to pull.

    None means "don't share" -- the caller (``GET /peer/smtp/config``) turns
    that into a 404. Mirrors ``vpn_manager.export_payload()``: only ever
    served over the cert-pinned mTLS ``/peer/*`` channel, gated by pairing
    (checked by the caller) plus ``sharing_enabled`` here, never returned to
    a browser.
    """
    state = _load_state(settings)
    if not state["sharing_enabled"] or not state["has_config"] or state["source_peer_id"]:
        return None
    return {field: state[field] for field in _SHARED_FIELDS}


def import_from_peer(settings: Settings, payload: dict, *, source_peer_id: str, source_peer_name: str = "") -> dict:
    """Adopt a peer's exported SMTP settings as our own.

    Reuses ``update_settings()`` unchanged rather than writing state
    directly -- same reasoning as ``vpn_manager.import_from_peer()`` reusing
    ``save_uploaded_config()``: one validated write path regardless of
    whether the source was a browser form or a peer payload.
    ``update_settings()`` resets provenance to "self-owned" as part of its
    normal semantics, so it is called first and the real provenance is
    re-applied immediately after, in this same call.
    """
    source_peer_id = str(source_peer_id or "").strip()
    if not source_peer_id:
        raise ValueError("source_peer_id is required to import a peer's SMTP configuration")
    payload = payload if isinstance(payload, dict) else {}
    update_payload = {field: payload.get(field) for field in _SHARED_FIELDS if payload.get(field) not in (None, "")}
    result = update_settings(settings, update_payload)
    _save_state(settings, source_peer_id=source_peer_id, source_peer_name=str(source_peer_name or "").strip())
    return result


def bootstrap_smtp_from_swarm(settings: Settings) -> bool:
    """Adopt a paired peer's shared SMTP settings as our own.

    Only ever called when this drone has no usable SMTP config of its own
    (mirrors ``vpn_manager.bootstrap_vpn_from_swarm()``, called from the same
    kind of startup gate in ``create_server()``) -- never overrides an
    existing local configuration.

    Unlike VPN's swarm bootstrap, there is no "connected" concept to gate on
    here (SMTP has no persistent tunnel) -- any paired peer actively sharing
    a complete configuration qualifies; the first one found (in
    ``local_network.paired_peers()``'s own order) wins. Per-peer failures
    (offline, not sharing, malformed payload) are silently skipped, not
    errors -- there may be many paired peers and only one needs to work.
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
            payload, _address = _peer_get_json_for_peer(peer, "/v1/api/peer/smtp/config", settings, peer_id=peer_id)
        except Exception:
            continue  # offline, not paired on this address, sharing off, no config, etc.
        if not isinstance(payload, dict) or not payload.get("host"):
            continue
        peer_name = str(peer.get("name") or peer.get("hostname") or peer_id)
        try:
            import_from_peer(settings, payload, source_peer_id=peer_id, source_peer_name=peer_name)
        except Exception as error:
            print(
                f"Swarm SMTP bootstrap: failed to adopt the config shared by {peer_name}: "
                f"{error.__class__.__name__}: {error}",
                file=sys.stderr, flush=True,
            )
            continue
        print(f"Swarm SMTP bootstrap: adopted the SMTP configuration shared by {peer_name}", file=sys.stdout, flush=True)
        return True
    return False


def maybe_bootstrap_smtp(settings: Settings) -> None:
    """Best-effort, called once from ``create_server()`` startup, mirroring
    ``vpn_manager.maybe_auto_connect()``'s own bootstrap step. Never raises."""
    try:
        if not _load_state(settings)["has_config"]:
            bootstrap_smtp_from_swarm(settings)
    except Exception as error:
        print(f"SMTP swarm bootstrap failed: {error.__class__.__name__}: {error}", file=sys.stderr, flush=True)


_SHARING_REVOKED_PEER_OFF = "The peer sharing this SMTP configuration turned off sharing, so these credentials were removed."
_SHARING_REVOKED_PEER_GONE = "The peer that shared this SMTP configuration is no longer paired, so these credentials were removed."


def _revoke_local_credentials(settings: Settings, reason: str) -> None:
    """Wipe the imported SMTP password (not the rest of the config, not
    ``source_peer_id``) -- mirrors ``vpn_manager._revoke_local_credentials()``:
    leaving provenance in place keeps ``set_sharing_enabled()``/
    ``export_payload()`` refusing to ever let this now-orphaned config be
    re-shared. Only a genuine fresh ``update_settings()`` call (a real new
    save) clears provenance.
    """
    _save_state(settings, password="", revoked_reason=reason, revoked_at=_now_iso())


def check_sharing_revocation(settings: Settings) -> bool:
    """Mirrors ``vpn_manager.check_sharing_revocation()``: only an explicit
    peer 404 (sharing off / no config) or "no longer paired" revokes;
    anything else -- unreachable, timeout, any other HTTP status -- is
    transient and changes nothing. Never raises (runs unattended on a
    background poller). Returns True iff a revocation just happened.
    """
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
            _peer_get_json_for_peer(peer, "/v1/api/peer/smtp/config", settings, peer_id=source_peer_id)
            return False
        except HTTPError as error:
            if error.code == 404:
                _revoke_local_credentials(settings, _SHARING_REVOKED_PEER_OFF)
                return True
            return False
        except Exception:
            return False
    except Exception as error:
        print(f"SMTP sharing revocation check failed: {error.__class__.__name__}: {error}", file=sys.stderr, flush=True)
        return False


def run_sharing_revocation_poller(settings: Settings) -> None:
    """Forever-loop, mirrors ``vpn_manager.run_sharing_revocation_poller()``:
    started as its own daemon thread from ``create_server()``. A periodic
    background check is the only way to learn about revocation at all --
    Drones are outbound-only with no push channel.
    """
    interval = max(30.0, SMTP_SHARING_CHECK_INTERVAL_SECONDS)
    while True:
        time.sleep(interval)
        check_sharing_revocation(settings)


# --------------------------------------------------------------- sending


def _drone_label(settings: Settings) -> str:
    """Human-identifiable label for this drone: hostname + the same unique
    ``device_id`` shown as "Machine ID" in the Debug tile. Stamped on every
    outgoing email (From display name, subject, body) so a swarm owner
    receiving digests from several drones can tell them apart at a glance,
    without opening the message.
    """
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = ""
    device_id = settings.device_id or ""
    if hostname and device_id:
        return f"{hostname} ({device_id})"
    return device_id or hostname or "unknown drone"


def send_mail(settings: Settings, subject: str, body: str) -> None:
    """Stdlib-only send via ``smtplib`` -- raises ``SmtpSendError`` on any
    failure; callers decide how to surface it (a 4xx to the admin UI for
    Test Email, a log line for the digest poller)."""
    state = _load_state(settings)
    if not state["has_config"]:
        raise SmtpSendError("SMTP is not configured on this drone.")
    message = MIMEText(body, "plain", "utf-8")
    message["Subject"] = subject
    message["From"] = formataddr((f"Batocera Drone - {_drone_label(settings)}", state["from_address"]))
    message["To"] = state["recipient_email"]
    try:
        client_cls = smtplib.SMTP_SSL if state["use_ssl"] else smtplib.SMTP
        client = client_cls(state["host"], state["port"], timeout=SMTP_SEND_TIMEOUT_SECONDS)
        try:
            if state["use_starttls"] and not state["use_ssl"]:
                client.starttls()
            if state["username"] and state["password"]:
                client.login(state["username"], state["password"])
            client.send_message(message)
        finally:
            client.quit()
    except (OSError, smtplib.SMTPException) as error:
        raise SmtpSendError(f"{error.__class__.__name__}: {error}") from error


def send_mail_with_attachment(settings: Settings, subject: str, body: str, attachment_path: Path, attachment_filename: str) -> None:
    """Same stdlib-only ``smtplib`` send as ``send_mail``, plus one file
    attachment (``email.mime.multipart``/``email.mime.base``, both stdlib --
    no new dependency). Used only for config-backup tarballs today; the
    caller is responsible for any size cap (this function will happily try
    to send whatever it's given, and let the SMTP server itself reject an
    oversized message rather than guessing every provider's limit here)."""
    state = _load_state(settings)
    if not state["has_config"]:
        raise SmtpSendError("SMTP is not configured on this drone.")
    message = MIMEMultipart()
    message["Subject"] = subject
    message["From"] = formataddr((f"Batocera Drone - {_drone_label(settings)}", state["from_address"]))
    message["To"] = state["recipient_email"]
    message.attach(MIMEText(body, "plain", "utf-8"))
    part = MIMEBase("application", "gzip")
    with open(attachment_path, "rb") as handle:
        part.set_payload(handle.read())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{attachment_filename}"')
    message.attach(part)
    try:
        client_cls = smtplib.SMTP_SSL if state["use_ssl"] else smtplib.SMTP
        client = client_cls(state["host"], state["port"], timeout=SMTP_ATTACHMENT_SEND_TIMEOUT_SECONDS)
        try:
            if state["use_starttls"] and not state["use_ssl"]:
                client.starttls()
            if state["username"] and state["password"]:
                client.login(state["username"], state["password"])
            client.send_message(message)
        finally:
            client.quit()
    except (OSError, smtplib.SMTPException) as error:
        raise SmtpSendError(f"{error.__class__.__name__}: {error}") from error


def send_test_email(settings: Settings) -> dict:
    label = _drone_label(settings)
    subject = f"Batocera Drone [{label}]: test email"
    body = f"This is a test email from your Batocera Drone's SMTP settings.\n\nDrone: {label}\nSent at {_now_iso()}."
    try:
        send_mail(settings, subject, body)
        result = {"status": "ok", "sent_at": _now_iso()}
    except SmtpSendError as error:
        result = {"status": "error", "error": str(error)}
    _save_state(settings, last_test_result=result, last_test_at=_now_iso())
    return result


def _compose_digest(items: list, settings: Settings) -> tuple:
    drone_label = _drone_label(settings)
    subject = f"Batocera Drone [{drone_label}]: {len(items)} new notification{'s' if len(items) != 1 else ''}"
    lines = [f"Drone: {drone_label}", "", f"{len(items)} new item(s) since the last digest:", ""]
    for item in items:
        label = _notifications.EVENT_TYPE_LABELS.get(item["event_type"], item["event_type"])
        lines.append(f"- [{item['created_at']}] {label}: {item['title']}")
        # Indent every line of the message, not just the first -- a
        # drone_updated item's message can carry embedded newlines (the
        # release notes commit list), and only indenting the first line
        # would leave the rest looking like a new top-level digest entry.
        for message_line in str(item.get("message") or "").splitlines():
            lines.append(f"    {message_line}")
    return subject, "\n".join(lines)


def send_digest_if_needed(settings: Settings) -> dict:
    """No-op unless ``smtp_enabled`` + configured + at least one unsent,
    enabled-type audit item exists. Never raises -- intended to run
    unattended on a background poller, same convention as
    ``check_sharing_revocation``/``vpn_manager.check_and_self_heal``.
    """
    try:
        state = _load_state(settings)
        if not state["smtp_enabled"] or not state["has_config"]:
            return {"status": "skipped", "reason": "smtp not enabled or not configured"}
        enabled_types = [event_type for event_type, on in state["notify"].items() if on]
        if not enabled_types:
            return {"status": "skipped", "reason": "no notification types enabled"}
        items = _audit_store.list_unsent_events(settings, enabled_types, limit=AUDIT_EMAIL_MAX_ITEMS_PER_DIGEST)
        prune_result = _audit_store.prune_old_events(settings)
        if not items:
            return {"status": "skipped", "reason": "nothing new", **prune_result}
        subject, body = _compose_digest(items, settings)
        try:
            send_mail(settings, subject, body)
        except SmtpSendError as error:
            _save_state(settings, last_digest_error=str(error))
            print(f"SMTP digest send failed: {error}", file=sys.stderr, flush=True)
            return {"status": "error", "error": str(error), "item_count": len(items)}
        _audit_store.mark_events_emailed(settings, [item["id"] for item in items])
        _save_state(settings, last_digest_sent_at=_now_iso(), last_digest_error="")
        return {"status": "sent", "item_count": len(items), **prune_result}
    except Exception as error:
        print(f"Audit email digest check failed: {error.__class__.__name__}: {error}", file=sys.stderr, flush=True)
        return {"status": "error", "error": str(error)}


def run_audit_email_digest_poller(settings: Settings) -> None:
    """Forever-loop -- the codebase's 'cron style job every ~5 minutes'.
    There is no OS cron anywhere in this app; every periodic feature is an
    in-process daemon thread on this exact shape (see
    ``run_sharing_revocation_poller`` above, or ``vpn_manager``'s own
    pollers, all started once from ``create_server()``).
    """
    interval = max(60.0, AUDIT_EMAIL_POLL_INTERVAL_SECONDS)
    while True:
        time.sleep(interval)
        send_digest_if_needed(settings)
