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
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    from ..common.settings import Settings
    from ..storage.state_store import database_path as _state_database_path
    from ..storage.state_store import load_peer_route as _load_peer_route
    from ..storage.state_store import save_peer_route as _save_peer_route
    from . import local_network as _local_network
    from ..transport.tailnet import get_tailnet_ip, is_tailnet_address
    from .drone_network import _certificate_pem_fingerprint, _drone_advertised_api_port
    from .drone_tls import DroneCertificateManager
    from .network_identity import drone_scheme as _drone_scheme
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import load_peer_route as _load_peer_route  # type: ignore
    from storage.state_store import save_peer_route as _save_peer_route  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from transport.tailnet import get_tailnet_ip, is_tailnet_address  # type: ignore
    from transfer.drone_network import _certificate_pem_fingerprint, _drone_advertised_api_port  # type: ignore
    from transfer.drone_tls import DroneCertificateManager  # type: ignore
    from transfer.network_identity import drone_scheme as _drone_scheme  # type: ignore

# Local copy of the peer-request timeout (drone_api keeps its own for peer_download,
# still resident there); both read the same env var, so the value is identical.
PEER_CHECK_TIMEOUT_SECONDS = float(os.environ.get("DRONE_PEER_CHECK_TIMEOUT_SECONDS", "3"))


def _drone_client_ssl_context(settings: Settings, url: str, verify: bool = False, cafile: Optional[Path] = None) -> Optional[ssl.SSLContext]:
    """Build the client-side SSL context for an outbound peer/API call.

    Cert-pinned mTLS when a peer certificate is configured, unverified otherwise --
    callers decide ``verify``/``cafile`` based on what trust material (if any) they
    have for the destination.
    """
    if not url.startswith("https://"):
        return None
    context = ssl.create_default_context(cafile=str(cafile) if cafile else None) if verify else ssl._create_unverified_context()
    if verify and cafile:
        # The pinned peer certificate was captured directly at pairing time; its
        # routed NAT address need not appear in the SAN.
        context.check_hostname = False
    if (settings.drone_mtls_enabled or _local_network.is_local_mode(settings)) and settings.drone_cert_file.exists() and settings.drone_key_file.exists():
        context.load_cert_chain(certfile=str(settings.drone_cert_file), keyfile=str(settings.drone_key_file))
    return context


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


def _local_peer_cert_cache_path(settings: Settings, peer_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", peer_id)
    return (settings.userdata_root / "system" / "drone-app" / "local-peer-certs" / f"{safe}.crt").resolve()


def _peer_trust_cafile(
    settings: Settings,
    peer_id: Optional[str] = None,
    config: Optional[dict] = None,
    refresh_cert: bool = False,
) -> Optional[Path]:
    """Resolve the CA/cert file to trust for a peer request.

    Local-network pairing pins each peer's own certificate by fingerprint (captured
    at pairing time -- see ``_local_pair_peer``); there is no re-fetch path since
    there is no central authority to fetch a replacement from.
    """
    if not peer_id:
        return None
    local_cached = _local_peer_cert_cache_path(settings, peer_id)
    return local_cached if local_cached.exists() else None


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


# Drone self-update (_download_latest_drone_app, _overlay_drone_release_tree,
# _restart_drone_process_soon, _drone_work_dir) now lives in common/self_update.py
# (re-exported above).


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
        "drone_id": settings.device_id,
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


def _peer_route_kind(address: str) -> str:
    host = str(urlparse(str(address or "")).hostname or "").strip().strip("[]")
    if is_tailnet_address(host):
        return "tailnet"
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return "host"
    return "ip"


def _peer_address_candidates(
    peer: dict,
    *,
    settings: Optional[Settings] = None,
    peer_id: Optional[str] = None,
) -> list[str]:
    """Return trusted peer routes with a persisted successful route first.

    Without a cached success, stable Tailnet addressing wins, followed by the
    peer's advertised hostname and finally literal IP routes. Tailscale keeps a
    same-LAN Tailnet connection peer-to-peer, while this ordering avoids paying
    stale LAN-IP and mDNS timeouts whenever a peer moves between networks.
    """
    discovered: list[str] = []

    def add(address: object) -> None:
        value = str(address or "").strip().rstrip("/")
        if value and value not in discovered:
            discovered.append(value)

    scheme = str(peer.get("scheme") or peer.get("protocol") or "https").strip() or "https"
    port = _peer_api_port(peer)
    port_suffix = "" if (scheme == "https" and port == 443) or (scheme == "http" and port == 80) else f":{port}"

    def add_host(host_value: object) -> None:
        host = str(host_value or "").strip()
        if not host:
            return
        host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        add(f"{scheme}://{host}{port_suffix}")

    resolved = peer.get("resolved_network") if isinstance(peer.get("resolved_network"), dict) else {}
    tailnet_ip = str(peer.get("tailnet_ip") or resolved.get("tailnet_ip") or "").strip()
    if is_tailnet_address(tailnet_ip):
        add_host(tailnet_ip)
    for key in ("advertised_reachable_url", "public_reachable_url", "reachable_url"):
        add(peer.get(key))
    public_ip = str(peer.get("public_ip") or "").strip()
    if public_ip and peer.get("public_resolvable") is True:
        add_host(public_ip)
    for key in ("ipv4", "ipv6"):
        for value in resolved.get(key) or []:
            add_host(value)
    for key in ("local_ip", "private_ip"):
        values = peer.get(key)
        for value in values if isinstance(values, list) else [values]:
            add_host(value)

    route_order = {"tailnet": 0, "host": 1, "ip": 2}
    candidates = sorted(discovered, key=lambda value: route_order[_peer_route_kind(value)])
    normalized_peer_id = str(
        peer_id or peer.get("drone_id") or peer.get("device_id") or peer.get("id") or ""
    ).strip()
    if settings is not None and normalized_peer_id:
        try:
            cached = _load_peer_route(_state_database_path(settings.userdata_root), normalized_peer_id)
        except Exception:
            cached = None
        cached_address = str((cached or {}).get("address") or "").strip().rstrip("/")
        # Only reuse a route still present in the authorized peer metadata. The
        # cached result changes preference, never the set of trusted endpoints.
        if cached_address in candidates:
            candidates.remove(cached_address)
            candidates.insert(0, cached_address)
    return candidates


def _preferred_peer_address(
    peer: dict,
    *,
    settings: Optional[Settings] = None,
    peer_id: Optional[str] = None,
) -> Optional[str]:
    candidates = _peer_address_candidates(peer, settings=settings, peer_id=peer_id)
    return candidates[0] if candidates else None


def _remember_successful_peer_route(settings: Settings, peer_id: str, address: str) -> None:
    normalized_peer_id = str(peer_id or "").strip()
    if not normalized_peer_id:
        return
    try:
        _save_peer_route(
            _state_database_path(settings.userdata_root),
            normalized_peer_id,
            address,
            _peer_route_kind(address),
        )
    except Exception:
        # Route caching is an optimization; a successful authenticated peer
        # request must not fail merely because SQLite is temporarily unavailable.
        return


def _peer_get_json_for_peer(
    peer: dict,
    endpoint: str,
    settings: Settings,
    *,
    peer_id: Optional[str] = None,
    config: Optional[dict] = None,
    refresh_cert: bool = False,
    timeout: float = PEER_CHECK_TIMEOUT_SECONDS,
    overall_deadline: Optional[float] = None,
) -> tuple[dict, str]:
    """GET a peer endpoint using its cached route or Tailnet/host/IP order.

    Non-final candidates use the short connectivity timeout so a stale route
    cannot hold an inventory request for its full (potentially two minute)
    transfer timeout. Every attempt uses the same pinned peer identity and mTLS
    client; this is address failover, never a TLS downgrade.

    ``overall_deadline`` (a ``time.monotonic()`` cutoff) is an *additional*,
    opt-in cap across every candidate address combined -- without it, a fully
    unreachable peer with N candidates takes ``(N-1) * min(timeout,
    PEER_CHECK_TIMEOUT_SECONDS) + timeout`` seconds, not ``timeout``, since the
    per-candidate cap above only bounds each *individual* attempt. Callers that
    genuinely want the fallback chain to run to completion regardless of total
    wall-clock time (e.g. a large transfer, where address failover is more
    valuable than a tight deadline) should leave this unset -- unaffected by
    this parameter, matching prior behavior exactly. Callers with a real
    "this must feel fast" requirement (e.g. a fleet-overview probe fanned out
    across many peers) should pass it.
    """
    path = str(endpoint or "").strip()
    if not path.startswith("/"):
        raise ValueError("peer endpoint must start with /")
    normalized_peer_id = str(
        peer_id or peer.get("drone_id") or peer.get("device_id") or peer.get("id") or ""
    ).strip()
    addresses = _peer_address_candidates(peer, settings=settings, peer_id=normalized_peer_id)
    if not addresses:
        raise ValueError("no peer address available")
    last_error: Optional[Exception] = None
    for index, address in enumerate(addresses):
        if overall_deadline is not None:
            remaining = overall_deadline - time.monotonic()
            if remaining <= 0:
                # Budget already spent by earlier candidates; one more attempt
                # (even a fast-failing one) would only make a slow peer probe
                # slower for no benefit -- stop and report the last failure.
                break
            attempt_timeout = remaining if index == len(addresses) - 1 else min(remaining, PEER_CHECK_TIMEOUT_SECONDS)
        else:
            attempt_timeout = timeout
            if index < len(addresses) - 1:
                attempt_timeout = min(float(timeout), PEER_CHECK_TIMEOUT_SECONDS)
        try:
            payload = _peer_get_json(
                f"{address}{path}",
                settings,
                peer_id=peer_id,
                config=config,
                refresh_cert=refresh_cert,
                timeout=attempt_timeout,
            )
            _remember_successful_peer_route(settings, normalized_peer_id, address)
            return payload, address
        except HTTPError:
            # The peer answered and rejected the request; changing routes
            # cannot make an authorization or endpoint error succeed.
            raise
        except (OSError, URLError, ssl.SSLError) as error:
            last_error = error
        except Exception:
            # Malformed payloads and programming errors are not reachability
            # failures and should remain visible to the caller.
            raise
    if last_error is not None:
        raise last_error
    raise ValueError("no peer address available")


class PeerProxyResponse:
    """A relayed peer HTTP response: status/content-type/body, never JSON-parsed.

    Used for remote-admin proxying (see ``handlers_remote_admin.py``), where the
    proxied route may return plain text (logs) or raw file content, not just
    JSON -- the caller relays this verbatim rather than interpreting it.
    """

    __slots__ = ("status", "content_type", "body")

    def __init__(self, status: int, content_type: str, body: bytes) -> None:
        self.status = status
        self.content_type = content_type
        self.body = body


def _peer_proxy_request(
    peer: dict,
    method: str,
    endpoint: str,
    settings: Settings,
    *,
    body: Optional[bytes] = None,
    authorization: Optional[str] = None,
    content_type: Optional[str] = None,
    peer_id: Optional[str] = None,
    config: Optional[dict] = None,
    timeout: float = PEER_CHECK_TIMEOUT_SECONDS,
) -> PeerProxyResponse:
    """Forward one HTTP request to a paired peer's own admin surface and relay
    its raw response, for remote-administration proxying.

    Same address iteration (cached route -> Tailnet -> host -> IP) and pinned
    mTLS trust as ``_peer_get_json_for_peer``, generalized to an arbitrary
    method/body and an explicit ``Authorization`` header -- this is always the
    *target's own* credentials, supplied by the caller, never this Drone's own
    local-network config. A non-2xx response from the peer (bad
    credentials, unknown route, etc.) is relayed as a ``PeerProxyResponse``,
    not raised -- the peer answered, so trying another address cannot change
    the outcome (same reasoning as ``_peer_get_json_for_peer``'s HTTPError
    handling); only connection-level failures fall through to the next
    candidate address.

    Multi-address fallback is restricted to safe-to-retry ``GET`` requests.
    A mutating ``POST`` (many admin actions are not idempotent -- e.g.
    restarting EmulationStation) uses only the single best candidate address
    with the *full* timeout and never retries elsewhere: a timeout on that
    address is ambiguous (still processing vs. truly unreachable), and
    retrying via a second address could fire the same action twice on the
    peer. A POST that only has one candidate address behaves the same either
    way; this only changes behavior when there are several.
    """
    path = str(endpoint or "").strip()
    if not path.startswith("/"):
        raise ValueError("peer endpoint must start with /")
    normalized_peer_id = str(
        peer_id or peer.get("drone_id") or peer.get("device_id") or peer.get("id") or ""
    ).strip()
    addresses = _peer_address_candidates(peer, settings=settings, peer_id=normalized_peer_id)
    if not addresses:
        raise ValueError("no peer address available")
    is_mutating = method.upper() != "GET"
    if is_mutating:
        addresses = addresses[:1]
    headers = {"Accept": "application/json", "User-Agent": "batocera-drone-remote-admin/1.0"}
    if authorization:
        headers["Authorization"] = authorization
    if body is not None and content_type:
        headers["Content-Type"] = content_type
    last_error: Optional[Exception] = None
    for index, address in enumerate(addresses):
        attempt_timeout = timeout
        if not is_mutating and index < len(addresses) - 1:
            attempt_timeout = min(float(timeout), PEER_CHECK_TIMEOUT_SECONDS)
        url = f"{address}{path}"
        cafile = _peer_trust_cafile(settings, peer_id=normalized_peer_id, config=config)
        if url.startswith("https://") and normalized_peer_id and not cafile:
            last_error = ssl.SSLError(f"no trusted certificate cached for peer {normalized_peer_id}")
            continue
        request = Request(url, data=body, method=method.upper(), headers=headers)
        try:
            with urlopen(
                request,
                timeout=attempt_timeout,
                context=_drone_client_ssl_context(settings, url, verify=bool(cafile), cafile=cafile),
            ) as response:
                _remember_successful_peer_route(settings, normalized_peer_id, address)
                return PeerProxyResponse(
                    response.status,
                    response.headers.get("Content-Type", "application/json"),
                    response.read(),
                )
        except HTTPError as error:
            # The peer answered (auth rejection, unknown route, ...); relay it
            # as-is -- a different address cannot change an answered request.
            return PeerProxyResponse(
                error.code,
                (error.headers.get("Content-Type") if error.headers else None) or "application/json",
                error.read(),
            )
        except ssl.SSLError as error:
            last_error = ssl.SSLError(_peer_ssl_diagnostic(url, cafile, error))
        except URLError as error:
            reason = getattr(error, "reason", None)
            last_error = URLError(_peer_ssl_diagnostic(url, cafile, reason)) if isinstance(reason, ssl.SSLError) else error
        except OSError as error:
            last_error = error
    if last_error is not None:
        raise last_error
    raise ValueError("no peer address available")


def _check_peer(settings: Settings, peer: dict, config: Optional[dict] = None) -> dict:
    target_id = str(peer.get("drone_id") or peer.get("device_id") or peer.get("id") or "")
    peer_id = target_id
    addresses = _peer_address_candidates(peer, settings=settings, peer_id=target_id)
    address = addresses[0] if addresses else None
    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    result = {
        "source_drone_id": settings.device_id,
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
        _, address = _peer_get_json_for_peer(
            peer,
            "/health",
            settings,
            peer_id=peer_id,
            config=config,
        )
        result["target_address"] = address
        result["status"] = "pass"
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
    except ssl.SSLError as error:
        message = str(error)
        if config and any(term in message.lower() for term in ("unknown ca", "certificate", "cert")):
            try:
                _, address = _peer_get_json_for_peer(
                    peer,
                    "/health",
                    settings,
                    peer_id=peer_id,
                    config=config,
                    refresh_cert=True,
                )
                result["target_address"] = address
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
