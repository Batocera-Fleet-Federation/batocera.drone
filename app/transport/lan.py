"""LAN-direct transport: same-network drones transfer over the local network.

When two drones sit behind the same router they each self-report the **same
public IP** (exchanged directly peer-to-peer at pairing/discovery time, not via
any central service). That is a reliable "same LAN" signal: the drone can then
reach the peer directly at its ``local_ip`` -- far faster than a legacy WAN hop
and with no port-forward. This reuses the existing direct mTLS ``/peer/*`` path
(the drone cert's SANs already include its LAN IPs); only the address differs.

A **tailnet** peer counts as LAN too: when both drones run a mesh-VPN daemon
(see ``tailnet.py``), the peer's stable Tailnet address is directly reachable
across NATs and Tailscale keeps same-LAN traffic peer-to-peer. It is preferred
ahead of hostname and literal-IP fallback routes so a stale LAN address cannot
delay every request after a peer moves networks.

It is registered ahead of the legacy direct-public tier. A false positive
(e.g. two homes sharing a CGNAT public IP, or a stale tailnet address) simply
fails the direct attempt and the selector falls back to the next transport.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Optional

from .base import DownloadRequest, PeerTransport, TransferContext
from .tailnet import is_tailnet_address


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
        """Return the peer's LAN (or tailnet) URL if directly reachable, else None."""
        return self._tailnet_url(peer) or self._same_network_url(peer)

    def _same_network_url(self, peer: dict) -> Optional[str]:
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
        return self._peer_url(peer, local_ip)

    def _tailnet_url(self, peer: dict) -> Optional[str]:
        # A peer's tailnet address is only reachable if this drone is on the
        # tailnet too (own snapshot carries tailnet_ip). Same cheap-fields-first
        # ordering as _same_network_url.
        resolved = peer.get("resolved_network") if isinstance(peer.get("resolved_network"), dict) else {}
        peer_tailnet = str(peer.get("tailnet_ip") or resolved.get("tailnet_ip") or "").strip()
        if not peer_tailnet or not is_tailnet_address(peer_tailnet):
            return None
        network = self._local_network() or {}
        if not str(network.get("tailnet_ip") or "").strip():
            return None
        return self._peer_url(peer, peer_tailnet)

    @staticmethod
    def _peer_url(peer: dict, host_ip: str) -> str:
        scheme = str(peer.get("scheme") or "https")
        try:
            port = int(peer.get("api_port") or 443)
        except (TypeError, ValueError):
            port = 443
        host = f"[{host_ip}]" if ":" in host_ip and not host_ip.startswith("[") else host_ip
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
