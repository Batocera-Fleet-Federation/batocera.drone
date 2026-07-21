"""Network identity discovery and advertised Drone endpoint construction."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
import sys
import time
from typing import Any, Callable, List, Optional
from urllib.request import Request, urlopen

try:
    from ..transport.tailnet import get_tailnet_ip, is_tailnet_address
except ImportError:  # pragma: no cover - direct script execution fallback
    from transport.tailnet import get_tailnet_ip, is_tailnet_address  # type: ignore


_LOCAL_NETWORK_CACHE: dict = {"at": 0.0, "value": {}}
_LOCAL_NETWORK_SNAPSHOT_TTL_SECONDS = 120.0


def drone_scheme(settings: Any) -> str:
    return "http" if settings.http_only else "https"


def hostname_override_values(settings: Any) -> List[str]:
    value = settings.hostname_override or ""
    return [item.strip() for item in re.split(r"[,;\s]+", value) if item.strip()]


def is_ip_literal(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip("[]"))
        return True
    except ValueError:
        return False


def is_advertisable_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(str(value or "").strip("[]"))
    except ValueError:
        return False
    return not (
        address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
    )


def first_advertisable(values: list) -> Optional[str]:
    for value in values:
        text = str(value or "").split("%", 1)[0].strip()
        if text and is_advertisable_ip(text):
            return text
    return None


def drone_report_host(
    settings: Any,
    network: Optional[dict] = None,
    *,
    network_loader: Optional[Callable[[], dict]] = None,
) -> str:
    overrides = hostname_override_values(settings)
    if overrides:
        return overrides[0]
    network = network if isinstance(network, dict) else (network_loader or get_local_ip_addresses)()
    ipv4 = network.get("ipv4") if isinstance(network.get("ipv4"), list) else []
    ipv6 = network.get("ipv6") if isinstance(network.get("ipv6"), list) else []
    ipv4_host = first_advertisable(ipv4)
    if ipv4_host:
        return ipv4_host
    ipv6_host = first_advertisable(ipv6)
    if ipv6_host:
        return ipv6_host
    if ipv4:
        return str(ipv4[0])
    if ipv6:
        return str(ipv6[0])
    return "127.0.0.1"


def drone_reachable_url(
    settings: Any,
    network: Optional[dict] = None,
    *,
    report_host: Optional[Callable[[Any, Optional[dict]], str]] = None,
) -> str:
    host = (report_host or drone_report_host)(settings, network)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    scheme = drone_scheme(settings)
    port = int(getattr(settings, "advertised_api_port", None) or settings.https_port)
    port_suffix = "" if scheme == "https" and port == 443 else f":{port}"
    return f"{scheme}://{host}{port_suffix}"


def drone_network_payload(settings: Any, *, network_loader: Optional[Callable[[], dict]] = None) -> dict:
    network = (network_loader or get_local_ip_addresses)()
    public_ip_override = str(getattr(settings, "public_ip_override", None) or "").strip()
    if public_ip_override:
        network["public_ip"] = public_ip_override
    network["hostname_override"] = settings.hostname_override or None
    network["hostname_overrides"] = hostname_override_values(settings)
    network["reachable_url"] = drone_reachable_url(settings, network)
    return network


def get_router_ip_address(*, run_command: Optional[Callable[..., Any]] = None) -> Optional[str]:
    """Return the default gateway IP used to reach the local router."""
    commands = (
        ["sh", "-c", "ip route show default 2>/dev/null | awk '{print $3; exit}'"],
        ["sh", "-c", "route -n 2>/dev/null | awk '$1 == \"0.0.0.0\" {print $2; exit}'"],
    )
    execute = run_command or subprocess.run
    for command in commands:
        try:
            result = execute(command, capture_output=True, text=True, timeout=2)
            gateway_ip = (result.stdout or "").strip()
            if gateway_ip:
                return gateway_ip
        except Exception:
            continue
    return None


def get_local_ip_addresses(
    *,
    socket_module: Any = None,
    gateway_loader: Optional[Callable[[], Optional[str]]] = None,
    open_url: Optional[Callable[..., Any]] = None,
    request_factory: Optional[Callable[..., Any]] = None,
) -> dict:
    """Resolve this Drone's own local IPv4/IPv6 addresses (self-report + pairing payloads)."""
    ipv4: List[str] = []
    ipv6: List[str] = []

    def add(value: str) -> None:
        value = str(value or "").split("%", 1)[0].strip()
        if not value:
            return
        target = ipv6 if ":" in value else ipv4
        if value not in target:
            target.append(value)

    socket_api = socket_module or socket
    try:
        hostname = socket_api.gethostname()
        for info in socket_api.getaddrinfo(hostname, None):
            add(info[4][0])
    except OSError as error:
        print(f"Drone network resolution failed for hostname: {error}", file=sys.stderr, flush=True)

    try:
        with socket_api.socket(socket_api.AF_INET, socket_api.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            add(probe.getsockname()[0])
    except OSError as error:
        print(f"Drone IPv4 route resolution failed: {error}", file=sys.stderr, flush=True)

    try:
        with socket_api.socket(socket_api.AF_INET6, socket_api.SOCK_DGRAM) as probe6:
            probe6.connect(("2001:4860:4860::8888", 80))
            add(probe6.getsockname()[0])
    except OSError as error:
        if os.environ.get("DRONE_DEBUG_NETWORK", "").strip().lower() in {"1", "true", "yes", "on"}:
            print(f"Drone IPv6 route unavailable; skipping IPv6 detection: {error}", file=sys.stderr, flush=True)

    if "127.0.0.1" not in ipv4:
        ipv4.append("127.0.0.1")
    gateway_ip = (gateway_loader or get_router_ip_address)()
    # Mesh-VPN overlay address (None off-tailnet) -- kept out of the ipv4 list so
    # host/report selection is unchanged; consumers read the dedicated field.
    tailnet_ip = get_tailnet_ip(socket_module=socket_api)
    public_ip = None
    try:
        request = (request_factory or Request)("https://api.ipify.org", headers={"User-Agent": "batocera-drone-app/4.0"})
        with (open_url or urlopen)(request, timeout=3) as response:
            public_ip = response.read().decode("utf-8", errors="replace").strip() or None
    except Exception:
        public_ip = None
    print(f"Drone network resolved ipv4={ipv4} ipv6={ipv6} gateway={gateway_ip} public={public_ip} tailnet={tailnet_ip}", file=sys.stdout, flush=True)
    return {"ipv4": ipv4, "ipv6": ipv6, "gateway_ip": gateway_ip, "public_ip": public_ip, "tailnet_ip": tailnet_ip}


def local_network_snapshot() -> dict:
    """This drone's network info (public_ip + LAN ipv4), cached briefly.

    Used by the LAN-direct transport to detect same-LAN peers (peers behind the
    same NAT report the same public IP). Cached because resolving the public IP
    makes a network call; brief staleness is harmless -- a wrong guess just fails
    the LAN attempt and the selector falls back to the next transport.
    """
    now = time.monotonic()
    cache = _LOCAL_NETWORK_CACHE
    if cache["value"] and now - cache["at"] < _LOCAL_NETWORK_SNAPSHOT_TTL_SECONDS:
        return cache["value"]
    try:
        value = get_local_ip_addresses()
    except Exception:
        value = {}
    cache["at"] = now
    cache["value"] = value
    return value


def get_local_certificate_ips(*, socket_module: Any = None) -> List[str]:
    socket_api = socket_module or socket
    ips = ["127.0.0.1"]
    try:
        hostname = socket_api.gethostname()
        for info in socket_api.getaddrinfo(hostname, None):
            value = str(info[4][0] or "").split("%", 1)[0].strip()
            if value and ":" not in value and value not in ips:
                ips.append(value)
    except OSError:
        pass
    try:
        with socket_api.socket(socket_api.AF_INET, socket_api.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            value = probe.getsockname()[0]
            if value and value not in ips:
                ips.append(value)
    except OSError:
        pass
    # Include the tailnet address so certs generated while on a tailnet carry
    # it as a SAN (only a managed/CA-bundle trust mode checks hostnames; pinned
    # local pairing sets check_hostname=False and doesn't need it).
    tailnet_ip = get_tailnet_ip(socket_module=socket_api)
    if tailnet_ip and tailnet_ip not in ips:
        ips.append(tailnet_ip)
    return ips
