"""Tailnet (mesh-VPN overlay) address detection, stdlib only.

When the device runs a Tailscale-style mesh daemon, every node gets a stable
address from the CGNAT range ``100.64.0.0/10`` (or the Tailscale IPv6 ULA
``fd7a:115c:a1e0::/48``) that is directly reachable from every other node on
the same tailnet regardless of NAT — which makes a cross-network peer look
exactly like a LAN peer to the transfer stack. This module is the canonical
"is that a tailnet address / what is mine" helper shared by the LAN-direct
transport (same package) and the transfer-layer identity/pairing payloads
(``transfer`` already imports from ``transport``, never the reverse).

There is deliberately no settings gate: presence is the gate. Without a mesh
daemon the probe finds no tailnet route and returns ``None``, and every
downstream branch stays inert.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any, Optional

TAILNET_IPV4_NETWORK = ipaddress.ip_network("100.64.0.0/10")
TAILNET_IPV6_NETWORK = ipaddress.ip_network("fd7a:115c:a1e0::/48")

# Tailscale's quad100 virtual service address -- routed via the tailscale
# interface if and only if the daemon is up. A UDP connect() sends no packets;
# it just asks the kernel to resolve the route, so getsockname() reveals which
# source address (ours on the tailnet, if any) would be used.
_TAILNET_PROBE_ADDRESS = ("100.100.100.100", 53)


def is_tailnet_address(value: Any) -> bool:
    """True if ``value`` parses as an address inside a known tailnet range."""
    try:
        address = ipaddress.ip_address(str(value or "").strip().strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv4Address):
        return address in TAILNET_IPV4_NETWORK
    return address in TAILNET_IPV6_NETWORK


def get_tailnet_ip(*, socket_module: Any = None) -> Optional[str]:
    """This device's own tailnet IPv4, or None when not on a tailnet.

    Without a tailnet route the kernel resolves the probe via the default
    gateway and getsockname() returns a regular LAN address -- which fails the
    range check below, so absence degrades cleanly to None (never a wrong
    positive).
    """
    socket_api = socket_module or socket
    try:
        with socket_api.socket(socket_api.AF_INET, socket_api.SOCK_DGRAM) as probe:
            probe.connect(_TAILNET_PROBE_ADDRESS)
            candidate = str(probe.getsockname()[0] or "").strip()
    except OSError:
        return None
    return candidate if is_tailnet_address(candidate) else None
