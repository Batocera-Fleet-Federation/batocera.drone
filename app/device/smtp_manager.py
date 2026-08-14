"""Central SMTP configuration, durable mail queue, and backend dispatcher.

Every producer—including API actions initiated by the Web or Ports UI—only
persists an outbound-mail job. The SMTP-owning worker is the sole consumer and
the only runtime path that opens ``smtplib``. Imported satellites relay jobs
and audit events to that owner over the existing paired mTLS channel, so UI
lifetime and client implementation cannot alter delivery behavior.

The peer credential-sharing model mirrors VPN (see ``device/vpn_manager.py``):
configure once, share opt-in, single-hop provenance, auto-revoke if the sharing
peer turns it off, and default-on auto-pull for a newly set up drone. Settings,
*including the password*, live entirely in one JSON blob via the same
``storage/state_store.py`` mechanism VPN's whole state dict already uses --
there is no OpenVPN-style ``auth-user-pass <file>`` requirement forcing a
separate on-disk credentials file here, so there isn't one. This repo has no
crypto library available either way (stdlib-only), so this is the same
plaintext-on-disk tradeoff VPN already makes and documents.

Notification-type toggles and the master ``smtp_enabled`` switch are
local-only and never travel with the shared/exported payload. They are
enforced by the drone that owns the SMTP configuration. An imported client
keeps its local switch off and relays audit events regardless of that switch;
the owner's worker combines the whole fleet into its normal digest cadence.
UI processes never own delivery timing or SMTP credentials.

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
import tempfile
import time
from datetime import datetime, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from threading import Lock
from typing import Optional
from urllib.error import HTTPError
from urllib.parse import quote

try:
    from ..common.settings import Settings
    from ..storage.state_store import database_path as _state_database_path
    from ..storage.state_store import load_payload as _load_state_payload
    from ..storage.state_store import save_payload as _save_state_payload
    from ..storage import audit_store as _audit_store
    from ..storage import mail_queue_store as _mail_store
    from . import notifications as _notifications
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import load_payload as _load_state_payload  # type: ignore
    from storage.state_store import save_payload as _save_state_payload  # type: ignore
    from storage import audit_store as _audit_store  # type: ignore
    from storage import mail_queue_store as _mail_store  # type: ignore
    from device import notifications as _notifications  # type: ignore

SMTP_STATE_NAMESPACE = "smtp_manager.json"
SMTP_SEND_TIMEOUT_SECONDS = float(os.environ.get("DRONE_SMTP_SEND_TIMEOUT_SECONDS", "15"))
# An attachment (base64-inflated ~33%) can take far longer to transmit than a
# plain-text digest/test email over a modest home upload link -- give it real
# room rather than reusing the short default meant for a few KB of text.
SMTP_ATTACHMENT_SEND_TIMEOUT_SECONDS = float(os.environ.get("DRONE_SMTP_ATTACHMENT_SEND_TIMEOUT_SECONDS", "120"))
SMTP_SHARING_CHECK_INTERVAL_SECONDS = float(os.environ.get("DRONE_SMTP_SHARING_CHECK_INTERVAL_SECONDS", "300"))
# Default/fallback seed for the user-configurable digest_interval_seconds
# setting below (1 minute - 24 hours, default 5 minutes) -- once a drone has
# saved its own value via the admin UI, that stored value always wins over
# this env var; this only seeds a fresh drone that has never saved one.
AUDIT_EMAIL_POLL_INTERVAL_SECONDS = float(os.environ.get("DRONE_AUDIT_EMAIL_INTERVAL_SECONDS", "300"))
DIGEST_INTERVAL_MIN_SECONDS = 60
DIGEST_INTERVAL_MAX_SECONDS = 86400
# How often the poller thread wakes to check whether the user's configured
# interval has elapsed -- independent of that interval itself, so a change
# saved in the admin UI takes effect within one tick instead of waiting out
# whatever (possibly much longer) interval was previously in effect.
DIGEST_POLLER_TICK_SECONDS = 5.0
AUDIT_EMAIL_MAX_ITEMS_PER_DIGEST = 200
AUDIT_EMAIL_RELAY_MAX_ITEMS = 200
OUTBOUND_MAIL_RELAY_MAX_ITEMS = 20
OUTBOUND_MAIL_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024

_DIGEST_SEND_LOCK = Lock()
_OUTBOUND_MAIL_LOCK = Lock()

# Fields carried in the shared/exported payload (peer-to-peer, mirrors VPN's
# config+credentials). Local-only fields (smtp_enabled, notify.*, last_*) are
# deliberately excluded -- see the module docstring.
_SHARED_FIELDS = (
    "host", "port", "use_starttls", "use_ssl", "username", "password",
    "from_address", "recipient_email",
)


class SmtpSendError(Exception):
    """Raised by low-level delivery; the worker persists it for retry/status."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _clamp_digest_interval(value) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = int(AUDIT_EMAIL_POLL_INTERVAL_SECONDS)
    return max(DIGEST_INTERVAL_MIN_SECONDS, min(DIGEST_INTERVAL_MAX_SECONDS, seconds))


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
        "digest_interval_seconds": _clamp_digest_interval(
            stored.get("digest_interval_seconds", AUDIT_EMAIL_POLL_INTERVAL_SECONDS)
        ),
        "notify": {
            event_type: bool(notify_stored.get(event_type, True)) for event_type in _notifications.EVENT_TYPES
        },
        "last_test_result": stored.get("last_test_result"),
        "last_test_at": stored.get("last_test_at"),
        "last_digest_sent_at": stored.get("last_digest_sent_at"),
        "last_digest_attempt_at": stored.get("last_digest_attempt_at"),
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
    sanitized["delivery_mode"] = "relay" if sanitized.get("source_peer_id") else "local"
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
    current = _load_state(settings)
    # A satellite is permanently relay-only while its SMTP configuration is
    # imported. Keeping this false also prevents an older/misleading UI from
    # suggesting that it can re-enable a second digest sender.
    effective = False if current["source_peer_id"] else bool(enabled)
    state = _save_state(settings, smtp_enabled=effective)
    return {"smtp_enabled": state["smtp_enabled"]}


def update_notification_toggles(settings: Settings, payload: dict) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    merged = dict(_load_state(settings)["notify"])
    for event_type in _notifications.EVENT_TYPES:
        if event_type in payload:
            merged[event_type] = bool(payload[event_type])
    state = _save_state(settings, notify=merged)
    return {"notify": state["notify"]}


def update_digest_interval(settings: Settings, seconds) -> dict:
    """Minimum interval between digest attempts by an SMTP-owning worker.

    Satellites relay their events promptly; this owner-side setting is what
    controls when the combined fleet digest is sent. It is local-only, 1
    minute to 24 hours, and defaults to 5 minutes
    (``AUDIT_EMAIL_POLL_INTERVAL_SECONDS``).
    """
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        raise ValueError("Digest interval must be a whole number of seconds")
    if not DIGEST_INTERVAL_MIN_SECONDS <= value <= DIGEST_INTERVAL_MAX_SECONDS:
        raise ValueError(
            f"Digest interval must be between {DIGEST_INTERVAL_MIN_SECONDS} seconds (1 minute) and "
            f"{DIGEST_INTERVAL_MAX_SECONDS} seconds (24 hours)"
        )
    state = _save_state(settings, digest_interval_seconds=value)
    return {"digest_interval_seconds": state["digest_interval_seconds"]}


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
    update_settings(settings, update_payload)
    state = _save_state(
        settings,
        source_peer_id=source_peer_id,
        source_peer_name=str(source_peer_name or "").strip(),
        # Imported clients never send notification digests themselves. The
        # relay path intentionally does not consult this owner-only switch.
        smtp_enabled=False,
    )
    return _sanitized(state)


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
        state = _load_state(settings)
        if state["source_peer_id"] and state["smtp_enabled"]:
            # One-time migration for imported configurations created by an
            # older release, where every satellite still ran its own SMTP
            # sender. Relay does not depend on this owner-only switch.
            _save_state(settings, smtp_enabled=False)
        elif not state["has_config"]:
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
    """Low-level SMTP delivery used only by the outbound-mail worker."""
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
    no new dependency). Called only by the outbound-mail worker."""
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


def queue_test_email(settings: Settings) -> dict:
    """Persist a test-email request for the centralized worker.

    The API response confirms queueing, never SMTP delivery. Closing either
    UI immediately after this returns cannot cancel the email.
    """
    if not _load_state(settings)["has_config"]:
        return {"status": "not_configured", "error": "SMTP is not configured on this drone."}
    label = _drone_label(settings)
    subject = f"Batocera Drone [{label}]: test email"
    body = f"This is a test email queued through your Batocera Drone API.\n\nDrone: {label}\nQueued at {_now_iso()}."
    job = _mail_store.enqueue(settings, kind="test", subject=subject, body=body)
    result = {"status": "queued", "job_id": job["id"], "queued_at": job["created_at"]}
    _save_state(settings, last_test_result=result, last_test_at=_now_iso())
    return result


def queue_mail(
    settings: Settings,
    *,
    kind: str,
    subject: str,
    body: str,
    attachment_path: Optional[Path] = None,
    attachment_filename: str = "",
    metadata: Optional[dict] = None,
) -> dict:
    """Queue a non-digest email from another backend feature."""
    if not _load_state(settings)["has_config"]:
        return {"status": "not_configured"}
    job = _mail_store.enqueue(
        settings,
        kind=kind,
        subject=subject,
        body=body,
        attachment_path=attachment_path,
        attachment_filename=attachment_filename,
        metadata=metadata,
    )
    return {"status": "queued", "job_id": job["id"], "queued_at": job["created_at"]}


def _compose_digest(items: list, settings: Settings) -> tuple:
    drone_label = _drone_label(settings)
    subject = f"Batocera Drone [{drone_label}]: {len(items)} new notification{'s' if len(items) != 1 else ''}"
    lines = [f"Drone: {drone_label}", "", f"{len(items)} new item(s) since the last digest:", ""]
    for item in items:
        label = _notifications.EVENT_TYPE_LABELS.get(item["event_type"], item["event_type"])
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        source_name = str(details.get("source_drone_name") or "").strip()
        source_suffix = f" on {source_name}" if source_name else ""
        lines.append(f"- [{item['created_at']}] {label}{source_suffix}: {item['title']}")
        # Indent every line of the message, not just the first -- a
        # drone_updated item's message can carry embedded newlines (the
        # release notes commit list), and only indenting the first line
        # would leave the rest looking like a new top-level digest entry.
        for message_line in str(item.get("message") or "").splitlines():
            lines.append(f"    {message_line}")
    return subject, "\n".join(lines)


def _digest_interval_elapsed(state: dict) -> bool:
    """True when this owner may attempt another digest delivery.

    The persisted attempt timestamp makes the cadence authoritative in the
    API worker itself, rather than in a UI or a particular poller-thread
    lifetime. It also prevents overlapping callers from sending the same
    audit rows more than once inside the configured interval.
    """
    anchor = state.get("last_digest_attempt_at") or state.get("last_digest_sent_at")
    if not anchor:
        return True
    try:
        parsed = datetime.fromisoformat(str(anchor).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return True
    return elapsed >= int(state.get("digest_interval_seconds") or AUDIT_EMAIL_POLL_INTERVAL_SECONDS)


def ingest_relayed_notifications(
    settings: Settings,
    events: list,
    *,
    source_drone_id: str,
    source_drone_name: str = "",
) -> dict:
    """Accept an idempotent event batch from a paired SMTP client.

    Only the self-owned, explicitly shared SMTP configuration is a valid
    aggregation target. Pair/mTLS authorization is enforced by the HTTP
    handler before this business-logic layer is called.
    """
    state = _load_state(settings)
    if not state["has_config"] or not state["sharing_enabled"] or state["source_peer_id"]:
        raise PermissionError("This drone is not accepting SMTP notification relays")
    source_drone_id = str(source_drone_id or "").strip()
    source_drone_name = str(source_drone_name or source_drone_id).strip()
    if not source_drone_id:
        raise ValueError("source_drone_id is required")
    if not isinstance(events, list):
        raise ValueError("events must be a list")
    if len(events) > AUDIT_EMAIL_RELAY_MAX_ITEMS:
        raise ValueError(f"A relay batch may contain at most {AUDIT_EMAIL_RELAY_MAX_ITEMS} events")

    accepted = []
    rejected = []
    for raw in events:
        if not isinstance(raw, dict):
            continue
        source_event_id = str(raw.get("source_event_id") or raw.get("id") or "").strip()
        event_type = str(raw.get("event_type") or "").strip()
        if not source_event_id or event_type not in _notifications.EVENT_TYPES:
            if source_event_id:
                rejected.append(source_event_id)
            continue
        details = dict(raw.get("details")) if isinstance(raw.get("details"), dict) else {}
        details.update({
            "source_drone_id": source_drone_id,
            "source_drone_name": source_drone_name,
            "relayed": True,
        })
        _audit_store.insert_relayed_event(
            settings,
            event_type,
            str(raw.get("title") or event_type)[:240],
            source_drone_id=source_drone_id,
            source_event_id=source_event_id,
            message=str(raw.get("message") or "")[:8000],
            details=details,
            created_at=str(raw.get("created_at") or "").strip() or None,
        )
        accepted.append(source_event_id)
    return {
        "status": "accepted",
        "accepted_event_ids": accepted,
        "accepted_count": len(accepted),
        "rejected_event_ids": rejected,
    }


def relay_notifications_to_source(settings: Settings) -> dict:
    """Relay this satellite's pending audit events to its SMTP owner.

    Successful acknowledgement marks local rows handled so retries are
    bounded. A failed/offline owner leaves them unsent for the next worker
    tick. This function never raises because it runs unattended.
    """
    try:
        state = _load_state(settings)
        source_peer_id = str(state.get("source_peer_id") or "").strip()
        if not source_peer_id:
            return {"status": "skipped", "reason": "SMTP configuration is self-owned"}
        if not state["has_config"]:
            return {"status": "skipped", "reason": "SMTP relay is not configured"}
        items = _audit_store.list_unsent_events(
            settings,
            _notifications.EVENT_TYPES,
            limit=AUDIT_EMAIL_RELAY_MAX_ITEMS,
        )
        if not items:
            return {"status": "skipped", "reason": "nothing new"}

        try:
            from ..transfer import local_network as _local_network
            from ..transfer.peer_connectivity import _peer_post_json_for_peer
        except ImportError:  # pragma: no cover - direct script execution fallback
            from transfer import local_network as _local_network  # type: ignore
            from transfer.peer_connectivity import _peer_post_json_for_peer  # type: ignore
        peer = _local_network.get_paired_peer(settings, source_peer_id)
        if not peer:
            return {"status": "error", "error": "SMTP owner is no longer a paired peer"}
        payload = {
            "source_drone_id": settings.device_id,
            "events": [
                {
                    "source_event_id": str(item["id"]),
                    "event_type": item["event_type"],
                    "title": item["title"],
                    "message": item.get("message") or "",
                    "details": item.get("details"),
                    "created_at": item.get("created_at"),
                }
                for item in items
            ],
        }
        response, _address = _peer_post_json_for_peer(
            peer,
            "/v1/api/peer/smtp/notifications",
            payload,
            settings,
            peer_id=source_peer_id,
        )
        accepted = {str(value) for value in (response.get("accepted_event_ids") or [])}
        handled_ids = [item["id"] for item in items if str(item["id"]) in accepted]
        _audit_store.mark_events_emailed(settings, handled_ids)
        return {
            "status": "relayed",
            "item_count": len(handled_ids),
            "pending_count": len(items) - len(handled_ids),
        }
    except Exception as error:
        print(
            f"SMTP notification relay failed: {error.__class__.__name__}: {error}",
            file=sys.stderr,
            flush=True,
        )
        return {"status": "error", "error": str(error)}


def ingest_relayed_mail_jobs(
    settings: Settings,
    jobs: list,
    *,
    source_drone_id: str,
    source_drone_name: str = "",
) -> dict:
    """Idempotently accept test/attachment mail jobs from a satellite."""
    state = _load_state(settings)
    if not state["has_config"] or not state["sharing_enabled"] or state["source_peer_id"]:
        raise PermissionError("This drone is not accepting outbound mail relays")
    source_drone_id = str(source_drone_id or "").strip()
    source_drone_name = str(source_drone_name or source_drone_id).strip()
    if not source_drone_id:
        raise ValueError("source_drone_id is required")
    if not isinstance(jobs, list):
        raise ValueError("jobs must be a list")
    if len(jobs) > OUTBOUND_MAIL_RELAY_MAX_ITEMS:
        raise ValueError(f"A mail relay batch may contain at most {OUTBOUND_MAIL_RELAY_MAX_ITEMS} jobs")

    accepted = []
    rejected = []
    for raw in jobs:
        if not isinstance(raw, dict):
            continue
        source_job_id = str(raw.get("source_job_id") or raw.get("id") or "").strip()
        kind = str(raw.get("kind") or "").strip()
        attachment_filename = str(raw.get("attachment_filename") or "").strip()
        valid_attachment = (
            kind == "test" and not attachment_filename
        ) or (
            kind == "config_backup"
            and bool(attachment_filename)
            and len(attachment_filename) <= 255
            and "/" not in attachment_filename
            and "\\" not in attachment_filename
            and "\r" not in attachment_filename
            and "\n" not in attachment_filename
            and not attachment_filename.startswith(".")
        )
        if not source_job_id or kind not in {"test", "config_backup"} or not valid_attachment:
            if source_job_id:
                rejected.append(source_job_id)
            continue
        metadata = dict(raw.get("metadata")) if isinstance(raw.get("metadata"), dict) else {}
        metadata.update({
            "source_drone_id": source_drone_id,
            "source_drone_name": source_drone_name,
            "remote_attachment": bool(attachment_filename),
        })
        _mail_store.enqueue(
            settings,
            kind=kind,
            subject=str(raw.get("subject") or "").replace("\r", " ").replace("\n", " ")[:1000],
            body=str(raw.get("body") or "")[:100000],
            source_drone_id=source_drone_id,
            source_job_id=source_job_id,
            attachment_filename=attachment_filename,
            metadata=metadata,
        )
        accepted.append(source_job_id)
    return {
        "status": "accepted",
        "accepted_job_ids": accepted,
        "accepted_count": len(accepted),
        "rejected_job_ids": rejected,
    }


def relay_mail_jobs_to_source(settings: Settings) -> dict:
    """Relay queued non-digest mail to the SMTP owner without opening SMTP."""
    try:
        state = _load_state(settings)
        source_peer_id = str(state.get("source_peer_id") or "").strip()
        if not source_peer_id:
            return {"status": "skipped", "reason": "SMTP configuration is self-owned"}
        jobs = [
            job for job in _mail_store.pending(settings, OUTBOUND_MAIL_RELAY_MAX_ITEMS)
            if job.get("kind") in {"test", "config_backup"}
        ]
        if not jobs:
            return {"status": "skipped", "reason": "nothing new"}
        try:
            from ..transfer import local_network as _local_network
            from ..transfer.peer_connectivity import _peer_post_json_for_peer
        except ImportError:  # pragma: no cover - direct script execution fallback
            from transfer import local_network as _local_network  # type: ignore
            from transfer.peer_connectivity import _peer_post_json_for_peer  # type: ignore
        peer = _local_network.get_paired_peer(settings, source_peer_id)
        if not peer:
            return {"status": "error", "error": "SMTP owner is no longer a paired peer"}
        payload = {
            "source_drone_id": settings.device_id,
            "jobs": [
                {
                    "source_job_id": str(job["id"]),
                    "kind": job["kind"],
                    "subject": job["subject"],
                    "body": job["body"],
                    "attachment_filename": job.get("attachment_filename") or "",
                    "metadata": job.get("metadata") or {},
                    "created_at": job.get("created_at"),
                }
                for job in jobs
            ],
        }
        response, _address = _peer_post_json_for_peer(
            peer,
            "/v1/api/peer/smtp/mail",
            payload,
            settings,
            peer_id=source_peer_id,
        )
        accepted = {str(value) for value in (response.get("accepted_job_ids") or [])}
        handled_ids = [job["id"] for job in jobs if str(job["id"]) in accepted]
        _mail_store.mark_relayed(settings, handled_ids)
        if any(job.get("kind") == "test" and job["id"] in handled_ids for job in jobs):
            relayed_at = _now_iso()
            _save_state(
                settings,
                last_test_result={"status": "relayed", "relayed_at": relayed_at},
                last_test_at=relayed_at,
            )
        return {
            "status": "relayed",
            "item_count": len(handled_ids),
            "pending_count": len(jobs) - len(handled_ids),
        }
    except Exception as error:
        print(
            f"SMTP mail-job relay failed: {error.__class__.__name__}: {error}",
            file=sys.stderr,
            flush=True,
        )
        return {"status": "error", "error": str(error)}


def _remote_attachment_for_job(settings: Settings, job: dict) -> Optional[Path]:
    attachment_filename = str(job.get("attachment_filename") or "").strip()
    if not attachment_filename:
        return None
    local_path = Path(str(job.get("attachment_path") or ""))
    if str(job.get("attachment_path") or "") and local_path.is_file():
        return local_path

    source_drone_id = str(job.get("source_drone_id") or "").strip()
    if not source_drone_id or source_drone_id == settings.device_id:
        raise FileNotFoundError(f"Queued attachment no longer exists: {attachment_filename}")
    try:
        from ..transfer import local_network as _local_network
        from ..transfer.peer_connectivity import _peer_download_file_for_peer
    except ImportError:  # pragma: no cover - direct script execution fallback
        from transfer import local_network as _local_network  # type: ignore
        from transfer.peer_connectivity import _peer_download_file_for_peer  # type: ignore
    peer = _local_network.get_paired_peer(settings, source_drone_id)
    if not peer:
        raise FileNotFoundError("The Drone providing this email attachment is no longer paired")
    spool = settings.userdata_root / "system" / "drone-app" / "mail-spool"
    spool.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="mail-", suffix=".attachment", dir=str(spool))
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        _peer_download_file_for_peer(
            peer,
            f"/v1/api/peer/config-backups/{quote(attachment_filename, safe='')}",
            temporary_path,
            settings,
            peer_id=source_drone_id,
            timeout=SMTP_ATTACHMENT_SEND_TIMEOUT_SECONDS,
            max_bytes=OUTBOUND_MAIL_ATTACHMENT_MAX_BYTES,
        )
        return temporary_path
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _process_outbound_mail_queue_unlocked(settings: Settings) -> dict:
    """Worker implementation; caller owns ``_OUTBOUND_MAIL_LOCK``."""
    state = _load_state(settings)
    if state.get("source_peer_id"):
        return {"status": "skipped", "reason": "mail is relayed to the SMTP owner"}
    if not state["has_config"]:
        return {"status": "skipped", "reason": "SMTP is not configured"}
    sent = 0
    failed = 0
    for job in _mail_store.ready(settings, limit=10):
        if job.get("kind") == "digest" and not _load_state(settings)["smtp_enabled"]:
            continue
        attachment_path: Optional[Path] = None
        remove_attachment = False
        try:
            attachment_path = _remote_attachment_for_job(settings, job)
            remove_attachment = bool(
                attachment_path
                and not str(job.get("attachment_path") or "")
            )
            if attachment_path:
                send_mail_with_attachment(
                    settings,
                    job["subject"],
                    job["body"],
                    attachment_path,
                    job.get("attachment_filename") or attachment_path.name,
                )
            else:
                send_mail(settings, job["subject"], job["body"])
            metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
            if job.get("kind") == "digest":
                event_ids = [int(value) for value in (metadata.get("audit_event_ids") or [])]
                _audit_store.mark_events_emailed(settings, event_ids)
                sent_at = _now_iso()
                _save_state(
                    settings,
                    last_digest_sent_at=sent_at,
                    last_digest_attempt_at=sent_at,
                    last_digest_error="",
                )
            elif job.get("kind") == "test" and job.get("source_drone_id") == settings.device_id:
                result = {"status": "ok", "sent_at": _now_iso(), "job_id": job["id"]}
                _save_state(settings, last_test_result=result, last_test_at=result["sent_at"])
            _mail_store.mark_sent(settings, job["id"])
            sent += 1
        except Exception as error:
            message = f"{error.__class__.__name__}: {error}"
            _mail_store.mark_failed(settings, job["id"], message)
            if job.get("kind") == "digest":
                _save_state(settings, last_digest_error=message)
            elif job.get("kind") == "test" and job.get("source_drone_id") == settings.device_id:
                _save_state(
                    settings,
                    last_test_result={"status": "error", "error": message, "job_id": job["id"]},
                    last_test_at=_now_iso(),
                )
            print(f"Outbound mail worker failed job {job['id']}: {message}", file=sys.stderr, flush=True)
            failed += 1
        finally:
            if remove_attachment and attachment_path:
                attachment_path.unlink(missing_ok=True)
    return {"status": "processed", "sent_count": sent, "failed_count": failed}


def process_outbound_mail_queue(settings: Settings) -> dict:
    """Deliver queued email from the sole SMTP-owning backend consumer."""
    with _OUTBOUND_MAIL_LOCK:
        return _process_outbound_mail_queue_unlocked(settings)


def send_digest_if_needed(settings: Settings) -> dict:
    """Queue one digest when due; never opens SMTP in this call.

    Kept under its historical name for internal compatibility. The dedicated
    outbound-mail consumer performs delivery and marks the audit rows only
    after SMTP succeeds.
    """
    with _DIGEST_SEND_LOCK:
        try:
            state = _load_state(settings)
            if state.get("source_peer_id"):
                return {"status": "skipped", "reason": "notifications are relayed to the SMTP owner"}
            if not state["smtp_enabled"] or not state["has_config"]:
                return {"status": "skipped", "reason": "smtp not enabled or not configured"}
            if _mail_store.has_pending_kind(settings, "digest"):
                return {"status": "skipped", "reason": "a digest is already queued"}
            if not _digest_interval_elapsed(state):
                return {"status": "skipped", "reason": "digest interval has not elapsed"}
            enabled_types = [event_type for event_type, on in state["notify"].items() if on]
            if not enabled_types:
                return {"status": "skipped", "reason": "no notification types enabled"}
            items = _audit_store.list_unsent_events(settings, enabled_types, limit=AUDIT_EMAIL_MAX_ITEMS_PER_DIGEST)
            prune_result = _audit_store.prune_old_events(settings)
            if not items:
                return {"status": "skipped", "reason": "nothing new", **prune_result}
            subject, body = _compose_digest(items, settings)
            attempt_at = _now_iso()
            _save_state(settings, last_digest_attempt_at=attempt_at)
            job = _mail_store.enqueue(
                settings,
                kind="digest",
                subject=subject,
                body=body,
                metadata={"audit_event_ids": [item["id"] for item in items]},
            )
            return {"status": "queued", "job_id": job["id"], "item_count": len(items), **prune_result}
        except Exception as error:
            print(f"Audit email digest check failed: {error.__class__.__name__}: {error}", file=sys.stderr, flush=True)
            return {"status": "error", "error": str(error)}


def run_audit_email_digest_poller(settings: Settings) -> None:
    """Single backend loop for every outbound email and peer relay.
    There is no OS cron anywhere in this app; every periodic feature is an
    in-process daemon thread on this exact shape (see
    ``run_sharing_revocation_poller`` above, or ``vpn_manager``'s own
    pollers, all started once from ``create_server()``).

    Imported clients relay audit events plus explicit mail jobs to the owner
    and never touch ``smtplib``. The owner queues due digests, then the sole
    outbound-mail consumer delivers digests, tests, and attachments.
    """
    while True:
        time.sleep(DIGEST_POLLER_TICK_SECONDS)
        if _load_state(settings).get("source_peer_id"):
            relay_notifications_to_source(settings)
            relay_mail_jobs_to_source(settings)
        else:
            send_digest_if_needed(settings)
            process_outbound_mail_queue(settings)
