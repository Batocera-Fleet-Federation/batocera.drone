"""Drone-specific network-identity wrappers.

Extracted from ``drone_api.py``. Thin adapters that call the pure ``network_identity``
helpers with the Drone's concrete dependencies (subprocess/socket/urllib) + Settings,
producing the report host, reachable URL, and advertised network payload.
"""

import hashlib
import socket
import ssl
import subprocess
from typing import List, Optional
from urllib.request import Request, urlopen

try:
    from ..common.settings import Settings
    from . import local_network as _local_network
    from .network_identity import (
        drone_network_payload as _build_drone_network_payload,
        drone_reachable_url as _build_drone_reachable_url,
        drone_report_host as _build_drone_report_host,
        get_local_certificate_ips as _build_local_certificate_ips,
        get_local_ip_addresses as _build_local_ip_addresses,
        get_router_ip_address as _build_router_ip_address,
    )
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from transfer.network_identity import (  # type: ignore
        drone_network_payload as _build_drone_network_payload,
        drone_reachable_url as _build_drone_reachable_url,
        drone_report_host as _build_drone_report_host,
        get_local_certificate_ips as _build_local_certificate_ips,
        get_local_ip_addresses as _build_local_ip_addresses,
        get_router_ip_address as _build_router_ip_address,
    )


def _get_router_ip_address() -> Optional[str]:
    return _build_router_ip_address(run_command=subprocess.run)


def _get_local_ip_addresses() -> dict:
    return _build_local_ip_addresses(
        socket_module=socket,
        gateway_loader=_get_router_ip_address,
        open_url=urlopen,
        request_factory=Request,
    )


def _get_local_certificate_ips() -> List[str]:
    return _build_local_certificate_ips(socket_module=socket)


def _drone_report_host(settings: Settings, network: Optional[dict] = None) -> str:
    return _build_drone_report_host(settings, network, network_loader=_get_local_ip_addresses)


def _drone_reachable_url(settings: Settings, network: Optional[dict] = None) -> str:
    return _build_drone_reachable_url(settings, network, report_host=_drone_report_host)


def _drone_network_payload(settings: Settings) -> dict:
    return _build_drone_network_payload(settings, network_loader=_get_local_ip_addresses)


def _drone_advertised_api_port(settings: Settings) -> int:
    return int(settings.advertised_api_port or settings.https_port)


def _drone_advertised_peer_mtls_port(settings: Settings) -> int:
    return int(settings.advertised_peer_mtls_port or settings.peer_mtls_port)


def _network_mode(settings: Settings) -> str:
    return _local_network.get_mode(settings)


def _certificate_pem_fingerprint(pem: str) -> str:
    certificate_pem = str(pem or "")
    # A CA file can contain many certificates, but a pairing response represents
    # one peer identity and must pin exactly the certificate we fingerprint.
    begin_marker = "-----BEGIN CERTIFICATE-----"
    end_marker = "-----END CERTIFICATE-----"
    if certificate_pem.count(begin_marker) != 1 or certificate_pem.count(end_marker) != 1:
        raise ValueError("peer certificate must contain exactly one X.509 certificate")
    # PEM_cert_to_DER_cert only decodes the PEM envelope.  It accepts arbitrary
    # nonempty DER bytes, which must never be treated as a peer trust anchor.
    # OpenSSL's CA loader performs the X.509 parse without requiring a network
    # connection or a chain verification.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    try:
        context.load_verify_locations(cadata=certificate_pem)
    except (ssl.SSLError, TypeError, ValueError) as error:
        raise ValueError("peer certificate is not a valid X.509 certificate") from error
    der = ssl.PEM_cert_to_DER_cert(certificate_pem)
    if not der:
        raise ValueError("peer certificate is empty")
    return hashlib.sha256(der).hexdigest()
