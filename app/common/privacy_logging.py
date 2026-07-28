"""Privacy-preserving helpers for request and security logs."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit


_LOG_PSEUDONYM_KEY = secrets.token_bytes(32)


def pseudonymize_ip(value: object, *, day: Optional[str] = None) -> str:
    """Return a short, process-local daily pseudonym instead of a raw IP address."""
    text = str(value or "-").split("%", 1)[0].strip()
    if not text or text == "-":
        return "ip#unknown"
    try:
        canonical = ipaddress.ip_address(text).compressed
    except ValueError:
        canonical = text
    bucket = day or datetime.now(timezone.utc).date().isoformat()
    digest = hmac.new(
        _LOG_PSEUDONYM_KEY,
        f"{bucket}\0{canonical}".encode("utf-8", errors="replace"),
        hashlib.sha256,
    ).hexdigest()[:12]
    return f"ip#{digest}"


def sanitize_request_line(value: object) -> str:
    """Remove query strings/fragments, which commonly contain tokens or search terms."""
    line = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    parts = line.split()
    if len(parts) < 2:
        return line[:2048]
    target = parts[1]
    try:
        parsed = urlsplit(target)
        target = parsed.path or "/"
    except ValueError:
        target = target.split("?", 1)[0].split("#", 1)[0]
    parts[1] = target
    return " ".join(parts)[:2048]
