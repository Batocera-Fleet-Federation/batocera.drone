"""LAN-direct transport: same-network drones transfer over the local network.

When two drones sit behind the same router they report the **same public IP** to
Overmind. That is a reliable "same LAN" signal: the drone can then reach the peer
directly at its ``local_ip`` -- far faster than relaying and with no WAN hop or
port-forward. This reuses the existing direct mTLS ``/peer/*`` path (the drone
cert's SANs already include its LAN IPs); only the address differs.

It is registered ahead of the public-direct and relay tiers, so a same-LAN peer
is served over the LAN first. A false positive (e.g. two homes sharing a CGNAT
public IP) simply fails the LAN attempt and the selector falls back to the next
transport.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Optional

from .base import DownloadRequest, PeerTransport, TransferContext


class LanDirectTransport(PeerTransport):
    name = "lan"

    def __init__(
        self,
        fetch_fn: Callable[[DownloadRequest, TransferContext], dict],
        *,
        local_network: Callable[[], dict],
    ) -> None:
        # ``fetch_fn`` is the direct-path dispatch (same one DirectPublic uses);
        # ``local_network`` returns this drone's network info (public_ip, ipv4).
        self._fetch_fn = fetch_fn
        self._local_network = local_network

    def lan_url(self, peer: dict) -> Optional[str]:
        """Return the peer's LAN URL if it is on this drone's LAN, else None."""
        # Check the peer's cheap fields first so we only resolve our own network
        # (a probe) for actual LAN candidates -- not on every transfer/peer.
        peer_public = str(peer.get("public_ip") or peer.get("public") or "").strip()
        local_ip = str(peer.get("local_ip") or "").strip()
        if not peer_public or not local_ip:
            return None
        network = self._local_network() or {}
        my_public = str(network.get("public_ip") or "").strip()
        if not my_public or my_public != peer_public:
            return None
        scheme = str(peer.get("scheme") or "https")
        try:
            port = int(peer.get("api_port") or 443)
        except (TypeError, ValueError):
            port = 443
        host = f"[{local_ip}]" if ":" in local_ip and not local_ip.startswith("[") else local_ip
        suffix = "" if (port == 443 and scheme == "https") else f":{port}"
        return f"{scheme}://{host}{suffix}"

    def usable(self, request: DownloadRequest, context: TransferContext) -> bool:
        return bool(self.lan_url(context.peer or {}))

    def fetch(self, request: DownloadRequest, context: TransferContext) -> dict:
        url = self.lan_url(context.peer or {})
        # Point the direct fetch at the peer's LAN address (mTLS + verify/activity
        # all reused unchanged).
        lan_peer = {**(context.peer or {}), "public_reachable_url": url, "reachable_url": url}
        return self._fetch_fn(request, replace(context, peer=lan_peer))
