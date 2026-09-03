"""Bearer-token auth for the Drone MCP server.

The MCP endpoint (``POST /v1/api/mcp``) is the one Drone HTTP surface a remote AI
assistant (Claude, Codex, ...) talks to, and those clients speak
``Authorization: Bearer <token>`` -- not the browser session cookie the rest of
the admin API uses. So MCP gets its own single, user-generated token.

The plaintext token is shown to the user exactly once (right after they generate
it, on the API Access page); only its SHA-256 digest is persisted, in the shared
state DB (``storage/state_store.py``) under the ``mcp`` namespace. Regenerating
replaces the previous token; revoking clears it and disables the endpoint.

Stdlib only.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from typing import Optional

try:
    from ..storage import state_store as _state_store
except ImportError:  # pragma: no cover - direct script execution fallback
    from storage import state_store as _state_store  # type: ignore


STATE_NAMESPACE = "mcp"
TOKEN_PREFIX = "dmcp_"


def _db_path(settings):
    return _state_store.database_path(settings.userdata_root)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load(settings) -> dict:
    data = _state_store.load_payload(_db_path(settings), STATE_NAMESPACE, {})
    return data if isinstance(data, dict) else {}


def _digest(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def generate_token(settings, label: Optional[str] = None) -> dict:
    """Create (or replace) the MCP token. Returns metadata plus the one-time ``token``."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    record = {
        "token_sha256": _digest(token),
        "hint": f"{token[:9]}...{token[-4:]}",
        "label": (label or "").strip()[:120] or "MCP client",
        "created_at": _now_iso(),
    }
    _state_store.save_payload(_db_path(settings), STATE_NAMESPACE, record)
    return {**_public(record), "token": token}


def revoke_token(settings) -> None:
    _state_store.save_payload(_db_path(settings), STATE_NAMESPACE, {})


def verify_token(settings, provided: Optional[str]) -> bool:
    provided = (provided or "").strip()
    if not provided:
        return False
    stored = str(_load(settings).get("token_sha256") or "")
    if not stored:
        return False
    return hmac.compare_digest(_digest(provided), stored)


def _public(record: dict) -> dict:
    return {
        "configured": bool(record.get("token_sha256")),
        "hint": record.get("hint"),
        "label": record.get("label"),
        "created_at": record.get("created_at"),
    }


def token_status(settings) -> dict:
    return _public(_load(settings))
