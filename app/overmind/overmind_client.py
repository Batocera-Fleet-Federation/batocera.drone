"""Outbound HTTP client for the Overmind control plane.

Extracted from ``drone_api.py``. Thin JSON-over-HTTPS helpers (POST/GET/DELETE,
with and without status), the drone's client-side SSL context builder (cert-pinned
mTLS when configured, unverified otherwise), and the shared HTTPError/URLError ->
readable-string formatter used across the app. Calls are gated on Overmind mode
being enabled.
"""

import json
import ssl
from pathlib import Path
from typing import Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from ..common.settings import Settings
    from ..transfer import local_network as _local_network
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from transfer import local_network as _local_network  # type: ignore


def _drone_client_ssl_context(settings: Settings, url: str, verify: bool = False, cafile: Optional[Path] = None) -> Optional[ssl.SSLContext]:
    if not url.startswith("https://"):
        return None
    context = ssl.create_default_context(cafile=str(cafile) if cafile else None) if verify else ssl._create_unverified_context()
    configured_ca = settings.drone_mtls_ca_file
    uses_configured_ca = bool(configured_ca and configured_ca.exists() and cafile and cafile.resolve() == configured_ca.resolve())
    uses_peer_pin = bool(verify and cafile and not uses_configured_ca)
    if uses_peer_pin:
        # The pinned peer certificate came from Overmind; its routed NAT address need not appear in the SAN.
        context.check_hostname = False
    if (settings.drone_mtls_enabled or _local_network.is_local_mode(settings)) and settings.drone_cert_file.exists() and settings.drone_key_file.exists():
        context.load_cert_chain(certfile=str(settings.drone_cert_file), keyfile=str(settings.drone_key_file))
    return context


def _overmind_post_json(url: str, payload: dict, token: Optional[str] = None, settings: Optional[Settings] = None) -> dict:
    if settings is not None and not _local_network.is_overmind_mode(settings):
        raise RuntimeError("Overmind integration is disabled")
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )
    context = _drone_client_ssl_context(settings, url) if settings else (ssl._create_unverified_context() if url.startswith("https://") else None)
    with urlopen(request, timeout=10, context=context) as response:
        raw = response.read()
    if not raw:
        return {}
    parsed = json.loads(raw.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def _overmind_post_json_with_status(
    url: str,
    payload: dict,
    token: Optional[str] = None,
    settings: Optional[Settings] = None,
    timeout_seconds: int = 10,
) -> Tuple[int, dict]:
    if settings is not None and not _local_network.is_overmind_mode(settings):
        raise RuntimeError("Overmind integration is disabled")
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=body, headers=headers, method="POST")
    context = _drone_client_ssl_context(settings, url) if settings else (ssl._create_unverified_context() if url.startswith("https://") else None)
    with urlopen(request, timeout=timeout_seconds, context=context) as response:
        status_code = int(getattr(response, "status", 200) or 200)
        raw = response.read()
    parsed = json.loads(raw.decode("utf-8")) if raw else {}
    return status_code, parsed if isinstance(parsed, dict) else {}


def _overmind_get_json(url: str, token: Optional[str] = None, settings: Optional[Settings] = None) -> dict:
    if settings is not None and not _local_network.is_overmind_mode(settings):
        raise RuntimeError("Overmind integration is disabled")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers, method="GET")
    context = _drone_client_ssl_context(settings, url) if settings else (ssl._create_unverified_context() if url.startswith("https://") else None)
    with urlopen(request, timeout=10, context=context) as response:
        raw = response.read()
    parsed = json.loads(raw.decode("utf-8")) if raw else {}
    return parsed if isinstance(parsed, dict) else {}


def _overmind_delete_json(url: str, token: Optional[str] = None, settings: Optional[Settings] = None) -> dict:
    if settings is not None and not _local_network.is_overmind_mode(settings):
        raise RuntimeError("Overmind integration is disabled")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers, method="DELETE")
    context = _drone_client_ssl_context(settings, url) if settings else (ssl._create_unverified_context() if url.startswith("https://") else None)
    with urlopen(request, timeout=10, context=context) as response:
        raw = response.read()
    parsed = json.loads(raw.decode("utf-8")) if raw else {}
    return parsed if isinstance(parsed, dict) else {}


def _format_overmind_error(error: BaseException) -> str:
    if isinstance(error, HTTPError):
        detail = ""
        try:
            raw = error.read()
            detail = raw.decode("utf-8", errors="replace").strip() if raw else ""
        except Exception:
            detail = ""
        if len(detail) > 500:
            detail = detail[:500] + "..."
        suffix = f" body={detail}" if detail else ""
        return f"HTTPError status={error.code} reason={error.reason or error.msg or 'unknown'} url={error.geturl()}{suffix}"
    if isinstance(error, URLError):
        reason = getattr(error, "reason", None)
        return f"URLError reason={reason!r}" if reason else f"URLError {error!r}"
    message = str(error).strip()
    if message:
        return f"{error.__class__.__name__}: {message}"
    return repr(error)
