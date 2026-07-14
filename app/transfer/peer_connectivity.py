"""Drone<->peer connectivity: cert trust/pinning, peer HTTP client, health, pairing.

Extracted from ``drone_api.py``. Fetches/pins peer mTLS certificates, builds the
per-peer SSL trust store, performs authenticated GETs against a peer, resolves peer
addresses/ports, runs the local-network pairing handshake, and probes peer health.
"""

import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

try:
    from ..common.settings import Settings
    from ..overmind.overmind_client import (
        _drone_client_ssl_context,
        _format_overmind_error,
        _overmind_get_json,
    )
    from ..storage.state_store import database_path as _state_database_path
    from ..storage.state_store import save_payload as _save_state_payload
    from . import local_network as _local_network
    from ..transport.tailnet import get_tailnet_ip, is_tailnet_address
    from .drone_network import _certificate_pem_fingerprint, _drone_advertised_api_port
    from .drone_tls import DroneCertificateManager
    from .network_identity import drone_scheme as _drone_scheme
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from overmind.overmind_client import (  # type: ignore
        _drone_client_ssl_context,
        _format_overmind_error,
        _overmind_get_json,
    )
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import save_payload as _save_state_payload  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from transport.tailnet import get_tailnet_ip, is_tailnet_address  # type: ignore
    from transfer.drone_network import _certificate_pem_fingerprint, _drone_advertised_api_port  # type: ignore
    from transfer.drone_tls import DroneCertificateManager  # type: ignore
    from transfer.network_identity import drone_scheme as _drone_scheme  # type: ignore

# Local copy of the peer-request timeout (drone_api keeps its own for peer_download,
# still resident there); both read the same env var, so the value is identical.
PEER_CHECK_TIMEOUT_SECONDS = float(os.environ.get("DRONE_PEER_CHECK_TIMEOUT_SECONDS", "3"))


def _public_local_peer(peer: dict) -> dict:
    return {key: value for key, value in dict(peer or {}).items() if key not in {"certificate_path"}}


def _save_local_peer_certificate(settings: Settings, peer_id: str, certificate_pem: str) -> Tuple[Path, str]:
    if "BEGIN CERTIFICATE" not in str(certificate_pem or ""):
        raise ValueError("peer certificate is required")
    fingerprint = _certificate_pem_fingerprint(certificate_pem)
    cert_path = _local_peer_cert_cache_path(settings, peer_id)
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_text(certificate_pem, encoding="utf-8")
    try:
        cert_path.chmod(0o600)
    except OSError:
        pass
    return cert_path, fingerprint


def _peer_cert_cache_dir(settings: Settings) -> Path:
    return (settings.userdata_root / "system" / "drone-app" / "peer-certs").resolve()


def _local_peer_cert_cache_path(settings: Settings, peer_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", peer_id)
    return (settings.userdata_root / "system" / "drone-app" / "local-peer-certs" / f"{safe}.crt").resolve()


def _peer_cert_cache_path(settings: Settings, peer_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", peer_id)
    return _peer_cert_cache_dir(settings) / f"{safe}.crt"


def _peer_cert_meta_path(settings: Settings, peer_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", peer_id)
    return _peer_cert_cache_dir(settings) / f"{safe}.json"


def _peer_trust_cafile(
    settings: Settings,
    peer_id: Optional[str] = None,
    config: Optional[dict] = None,
    refresh_cert: bool = False,
) -> Optional[Path]:
    if (
        _local_network.is_local_mode(settings)
        and (
            str((config or {}).get("network_mode") or "") == "local_network"
            or not _local_network.is_overmind_mode(settings)
        )
    ):
        if not peer_id:
            return None
        local_cached = _local_peer_cert_cache_path(settings, peer_id)
        return local_cached if local_cached.exists() else None
    if settings.drone_mtls_ca_file and settings.drone_mtls_ca_file.exists():
        if peer_id and refresh_cert and config:
            _fetch_peer_certificate(settings, config, peer_id)
        return settings.drone_mtls_ca_file
    if not peer_id:
        return None
    if refresh_cert and config:
        return _fetch_peer_certificate(settings, config, peer_id)
    cached = _peer_cert_cache_path(settings, peer_id)
    if cached.exists():
        return cached
    return _fetch_peer_certificate(settings, config or {}, peer_id) if config else None


def _peer_ssl_diagnostic(url: str, cafile: Optional[Path], error: BaseException) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    ca_configured = bool(cafile)
    ca_path = str(cafile) if cafile else "none"
    reason = str(error).strip() or error.__class__.__name__
    hint = "certificate validation failed"
    lower = reason.lower()
    if "hostname" in lower or "not valid for" in lower or "ip address mismatch" in lower:
        hint = "hostname/SAN mismatch"
    elif "self-signed" in lower or "unknown ca" in lower or "unable to get local issuer" in lower:
        hint = "missing or incorrect trusted CA bundle"
    elif "expired" in lower or "not yet valid" in lower:
        hint = "expired or not-yet-valid certificate"
    return f"{hint}: peer_url={url} hostname={host or 'unknown'} ca_configured={str(ca_configured).lower()} cafile={ca_path} error={reason}"


def _is_ssl_url_error(error: URLError) -> bool:
    reason = getattr(error, "reason", None)
    return isinstance(reason, ssl.SSLError)


# Overmind HTTP client (_overmind_post/get/delete_json, _drone_client_ssl_context,
# _format_overmind_error) now lives in overmind/overmind_client.py (re-exported above).


# Drone self-update (_download_latest_drone_app, _overlay_drone_release_tree,
# _restart_drone_process_soon, _drone_work_dir) now lives in common/self_update.py
# (re-exported above).


# _save_overmind_runtime_config now lives in overmind/overmind_config.py.


# Overmind token register/claim/reclaim + action-completion reporting now live in
# overmind/registration.py (re-exported above).


def _fetch_peer_certificate(settings: Settings, config: dict, peer_id: str) -> Optional[Path]:
    base_url = str(config.get("overmind_url") or "").strip().rstrip("/")
    token = str(config.get("overmind_token") or "").strip()
    if not base_url or not token or not peer_id:
        return None
    try:
        payload = _overmind_get_json(
            f"{base_url}/api/devices/{quote(settings.overmind_device_id, safe='')}/peer-certificate/{quote(peer_id, safe='')}",
            token=token,
            settings=settings,
        )
        pem = str(payload.get("certificate_pem") or "")
        if "BEGIN CERTIFICATE" not in pem:
            return None
        cert_path = _peer_cert_cache_path(settings, peer_id)
        cert_path.parent.mkdir(parents=True, exist_ok=True)
        cert_path.write_text(pem, encoding="utf-8")
        meta = dict(payload.get("metadata") or {})
        meta["peer_drone_id"] = peer_id
        meta["fetched_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        meta["source_overmind_url"] = base_url
        _save_state_payload(
            _state_database_path(settings.userdata_root),
            "peer_certificate_metadata",
            meta,
            state_key=peer_id,
        )
        _peer_cert_meta_path(settings, peer_id).unlink(missing_ok=True)
        print(f"Fetched peer certificate for {peer_id}", file=sys.stdout, flush=True)
        return cert_path
    except Exception as error:
        print(f"Failed to fetch peer certificate for {peer_id}: {_format_overmind_error(error)}", file=sys.stderr, flush=True)
        return None


def _peer_get_json(url: str, settings: Settings, peer_id: Optional[str] = None, config: Optional[dict] = None, refresh_cert: bool = False, timeout: float = PEER_CHECK_TIMEOUT_SECONDS) -> dict:
    cafile = _peer_trust_cafile(settings, peer_id=peer_id, config=config, refresh_cert=refresh_cert)
    if url.startswith("https://") and peer_id and not cafile:
        raise ssl.SSLError(f"no trusted certificate cached for peer {peer_id}")
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "batocera-drone-peer/1.0"})
    try:
        with urlopen(request, timeout=timeout, context=_drone_client_ssl_context(settings, url, verify=bool(cafile), cafile=cafile)) as response:
            raw = response.read()
    except ssl.SSLError as error:
        raise ssl.SSLError(_peer_ssl_diagnostic(url, cafile, error)) from error
    except URLError as error:
        reason = getattr(error, "reason", None)
        if isinstance(reason, ssl.SSLError):
            raise URLError(_peer_ssl_diagnostic(url, cafile, reason)) from error
        raise
    parsed = json.loads(raw.decode("utf-8")) if raw else {}
    return parsed if isinstance(parsed, dict) else {}


def _local_pair_peer(
    settings: Settings,
    peer: dict,
    pairing_code: str,
    *,
    tailnet_auto_pair: bool = False,
) -> dict:
    if not _local_network.is_local_mode(settings):
        raise ValueError("Drone is not in local network mode")
    peer_id = str(peer.get("drone_id") or peer.get("device_id") or "").strip()
    address = _peer_address(peer)
    if not peer_id or not address:
        raise ValueError("discovered peer has no reachable address")
    certificate = DroneCertificateManager(settings).ensure_certificate()
    certificate_pem = str(certificate.get("public_certificate") or "")
    if not certificate_pem:
        raise RuntimeError("local Drone certificate is unavailable")
    own_discovery = _local_network.discovery_payload(settings, str(certificate.get("fingerprint") or ""))
    payload = {
        "pairing_code": str(pairing_code or "").strip(),
        "tailnet_auto_pair": bool(tailnet_auto_pair),
        "drone_id": settings.overmind_device_id,
        "name": socket.gethostname(),
        "hostname": socket.gethostname(),
        "scheme": _drone_scheme(settings),
        "api_port": _drone_advertised_api_port(settings),
        "reachable_url": own_discovery.get("reachable_url"),
        "tailnet_ip": str(own_discovery.get("tailnet_ip") or ""),
        "certificate_pem": certificate_pem,
        "certificate_fingerprint": str(certificate.get("fingerprint") or ""),
    }
    request = Request(
        f"{address.rstrip('/')}/v1/api/peer/pair",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "batocera-drone-local-pair/1.0"},
        method="POST",
    )
    context = ssl._create_unverified_context() if address.startswith("https://") else None
    with urlopen(request, timeout=10, context=context) as response:
        raw = response.read()
    result = json.loads(raw.decode("utf-8")) if raw else {}
    if not isinstance(result, dict) or str(result.get("status") or "") != "paired":
        raise RuntimeError("peer did not accept pairing request")
    remote_id = str(result.get("drone_id") or "").strip()
    if remote_id != peer_id:
        raise RuntimeError("paired peer identity did not match discovered peer")
    remote_pem = str(result.get("certificate_pem") or "")
    cert_path, fingerprint = _save_local_peer_certificate(settings, peer_id, remote_pem)
    expected = str(peer.get("certificate_fingerprint") or "").strip().lower()
    returned = str(result.get("certificate_fingerprint") or "").strip().lower()
    if (expected and expected != fingerprint.lower()) or (returned and returned != fingerprint.lower()):
        cert_path.unlink(missing_ok=True)
        raise RuntimeError("paired peer certificate fingerprint did not match discovery")
    stored = _local_network.save_paired_peer(
        settings,
        {
            **peer,
            "name": str(result.get("name") or peer.get("name") or peer_id),
            "reachable_url": address,
            "advertised_reachable_url": str(result.get("reachable_url") or peer.get("advertised_reachable_url") or ""),
            "scheme": str(result.get("scheme") or peer.get("scheme") or "https"),
            "api_port": int(result.get("api_port") or peer.get("api_port") or 443),
            "tailnet_ip": str(result.get("tailnet_ip") or peer.get("tailnet_ip") or ""),
            "certificate_fingerprint": fingerprint,
            "certificate_path": str(cert_path),
            "pairing_source": "tailnet" if tailnet_auto_pair else str(peer.get("pairing_source") or "local_network"),
        },
    )
    return stored


def _normalize_peer_address(raw: str) -> str:
    """Normalize an operator-entered peer address to ``scheme://host[:port]``.

    Accepts a bare host/IP (``100.64.0.7``, ``drone-den.local``), ``host:port``,
    or a full ``http(s)://`` URL; defaults to https and omits default ports,
    matching how reachable_urls are built everywhere else.
    """
    address = str(raw or "").strip()
    if not address:
        raise ValueError("peer address is required")
    if "://" not in address:
        try:
            # A bare IPv6 literal must be bracketed before URL parsing, or its
            # colons are misread as a port separator.
            if ipaddress.ip_address(address).version == 6:
                address = f"[{address}]"
        except ValueError:
            pass
        address = f"https://{address}"
    address = address.rstrip("/")
    parsed = urlparse(address)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"invalid peer address port: {error}") from error
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("peer address must be host[:port] or http(s)://host[:port]")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is None or (port == 443 and parsed.scheme == "https") or (port == 80 and parsed.scheme == "http"):
        return f"{parsed.scheme}://{host}"
    return f"{parsed.scheme}://{host}:{port}"


def _fetch_peer_info(address: str, timeout: float = 5.0) -> dict:
    """Fetch a drone's open pairing-bootstrap identity (``GET /v1/api/peer/info``).

    This is how a peer is "discovered" across links the multicast announce
    cannot cross (e.g. a tailnet) -- by dialing its address directly. Same TOFU
    trust model as the pair POST itself: TLS is unverified because no
    certificate is pinned yet; current Tailnet membership or a pairing code
    authorizes the exchange, and ``_local_pair_peer`` pins the advertised
    certificate fingerprint before the peer can serve assets.
    """
    base = _normalize_peer_address(address)
    request = Request(
        f"{base}/v1/api/peer/info",
        headers={"Accept": "application/json", "User-Agent": "batocera-drone-local-pair/1.0"},
    )
    context = ssl._create_unverified_context() if base.startswith("https://") else None
    with urlopen(request, timeout=timeout, context=context) as response:
        raw = response.read()
    payload = json.loads(raw.decode("utf-8")) if raw else {}
    if not isinstance(payload, dict) or str(payload.get("service") or "") != _local_network.DISCOVERY_SERVICE:
        raise ValueError("address did not answer as a Batocera Drone peer")
    return payload


def _peer_health_url(address: str) -> str:
    return f"{str(address or '').strip().rstrip('/')}/health"


def _peer_api_port(peer: dict) -> int:
    try:
        return int(peer.get("api_port") or peer.get("port") or 443)
    except (TypeError, ValueError):
        return 443


def _peer_address(peer: dict) -> Optional[str]:
    public_reachable_url = str(peer.get("public_reachable_url") or "").strip().rstrip("/")
    if public_reachable_url:
        return public_reachable_url
    scheme = str(peer.get("scheme") or peer.get("protocol") or "https").strip() or "https"
    port = _peer_api_port(peer)
    reachable_url = str(peer.get("reachable_url") or "").strip().rstrip("/")
    if reachable_url:
        return reachable_url
    public_ip = str(peer.get("public_ip") or "").strip()
    if public_ip and peer.get("public_resolvable") is True:
        if ":" in public_ip and not public_ip.startswith("["):
            public_ip = f"[{public_ip}]"
        port_suffix = "" if port == 443 and scheme == "https" else f":{port}"
        return f"{scheme}://{public_ip}{port_suffix}"
    resolved = peer.get("resolved_network") if isinstance(peer.get("resolved_network"), dict) else {}
    for value in resolved.get("ipv4") or []:
        host = str(value or "").strip()
        if host:
            return f"{scheme}://{host}:{port}"
    for value in resolved.get("ipv6") or []:
        host = str(value or "").strip()
        if host:
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            return f"{scheme}://{host}:{port}"
    for key in ("local_ip", "private_ip"):
        value = peer.get(key)
        if isinstance(value, list):
            value = next((item for item in value if item), None)
        if value:
            host = str(value).strip()
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            return f"{scheme}://{host}:{port}"
    return None


def _check_peer(settings: Settings, peer: dict, config: Optional[dict] = None) -> dict:
    target_id = str(peer.get("drone_id") or peer.get("device_id") or peer.get("id") or "")
    peer_id = target_id
    address = _peer_address(peer)
    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    result = {
        "source_drone_id": settings.overmind_device_id,
        "target_drone_id": target_id,
        "target_address": address,
        "status": "fail",
        "latency_ms": None,
        "failure_reason": None,
        "checked_at": checked_at,
    }
    if not address:
        result["failure_reason"] = "no peer address available"
        return result
    started = time.monotonic()
    try:
        _peer_get_json(_peer_health_url(address), settings, peer_id=peer_id, config=config)
        result["status"] = "pass"
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
    except ssl.SSLError as error:
        message = str(error)
        if config and any(term in message.lower() for term in ("unknown ca", "certificate", "cert")):
            try:
                _peer_get_json(_peer_health_url(address), settings, peer_id=peer_id, config=config, refresh_cert=True)
                result["status"] = "pass"
                result["latency_ms"] = int((time.monotonic() - started) * 1000)
                return result
            except Exception as retry_error:
                result["failure_reason"] = f"{message}; retry after cert refresh failed: {retry_error}"
                return result
        result["failure_reason"] = message
    except Exception as error:
        result["failure_reason"] = str(error)
    return result


# System telemetry collectors (_sample_speed, _collect_gpu_info,
# _collect_performance_metrics, _collect_mounted_disk_metrics, _read_text_file, ...) now
# live in device/system_metrics.py (re-exported above). The aggregator below stays here
# because it also reads automation/rom-cache/network status.
