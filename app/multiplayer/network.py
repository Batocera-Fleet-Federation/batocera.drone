"""Reachability, latency and session-scoped port planning over the existing swarm routes.

Nothing here invents a new peer channel: probes go through the same cert-pinned,
address-failover peer client every other swarm call uses (``/peer/health``), so
LAN and Tailscale peers behave exactly as they do for transfers. Emulator
gameplay ports are a separate concern: they are allocated per session from a
configured range, never opened persistently, and nothing here modifies a
firewall.
"""

import ipaddress
import json
import re
import socket
import statistics
import time
from typing import Any, Callable, Iterable, Optional, Set
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

from . import models as m

try:
    from ..transfer.peer_connectivity import (_peer_address_candidates, _peer_get_json_for_peer,
                                              _peer_post_json_for_peer)
    from ..transport.tailnet import is_tailnet_address
except ImportError:  # pragma: no cover - flat execution
    from transfer.peer_connectivity import (_peer_address_candidates, _peer_get_json_for_peer,  # type: ignore
                                            _peer_post_json_for_peer)
    from transport.tailnet import is_tailnet_address  # type: ignore

PEER_CONFIG = {"network_mode": "local_network"}
PEER_API = "/v1/api/peer/multiplayer"
_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,250}[A-Za-z0-9])?$")


def valid_host(value: str) -> str:
    """A hostname or IP safe to write into an emulator setting; ``""`` if it is not."""
    text = str(value or "").strip().strip("[]")
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return text if _HOST.fullmatch(text) else ""


def route_scope(address: str) -> str:
    """``tailnet`` | ``lan`` | ``public`` | ``unknown`` for a peer URL or bare host."""
    host = urlparse(address).hostname if "://" in str(address) else str(address or "").strip("[]")
    host = str(host or "")
    if is_tailnet_address(host):
        return "tailnet"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if host.lower().endswith((".local", ".lan", ".home", ".internal")) or "." not in host:
            return "lan" if host else "unknown"
        if host.lower().endswith(".ts.net"):
            return "tailnet"
        return "unknown"
    if ip.is_private or ip.is_link_local or ip.is_loopback:
        return "lan"
    return "public"


class RelayProvider:
    """Seam for an optional layer-2/broadcast relay. Drone ships none (``none`` is the only provider)."""

    id = "none"

    def available(self) -> bool:
        return False

    def open_session(self, session_id: str, peers: Iterable[str]) -> dict:
        raise m.CapabilityError("No relay provider is configured. Broadcast-dependent LAN games work on one local network only.",
                                "relay_unavailable")

    def close_session(self, session_id: str) -> None:
        return None


class NetworkService:
    def __init__(self, settings: Any, *, get_json: Callable = _peer_get_json_for_peer,
                 post_json: Callable = _peer_post_json_for_peer, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, relay: Optional[RelayProvider] = None) -> None:
        self.settings = settings
        self._get = get_json
        self._post = post_json
        self._clock = clock
        self.relay = relay or RelayProvider()

    @staticmethod
    def peer_id(peer: dict) -> str:
        return str(peer.get("drone_id") or peer.get("device_id") or peer.get("id") or "")

    def candidates(self, peer: dict) -> list:
        try:
            return _peer_address_candidates(peer, settings=self.settings, peer_id=self.peer_id(peer))
        except Exception:  # noqa: BLE001 - a malformed stored peer must read as unreachable, not crash a page
            return []

    def peer_scope(self, peer: dict, preferred: str = "auto") -> str:
        addresses = self.candidates(peer)
        if not addresses:
            return "unknown"
        scopes = [route_scope(address) for address in addresses]
        if preferred in ("lan", "tailnet") and preferred in scopes:
            return preferred
        return scopes[0]

    def host_address(self, peer: dict, preferred: str = "auto") -> str:
        """The address *this* Drone uses to reach ``peer`` -- what an emulator client should dial."""
        # Gameplay dials an address chosen for gameplay, not the control-plane route order: a known LAN address
        # first, then the tailnet, then a public address (unless the user explicitly prefers the tailnet or LAN).
        scopes = ["lan", "tailnet", "public", "unknown"]
        if preferred in ("lan", "tailnet"):
            scopes.remove(preferred)
            scopes.insert(0, preferred)
        addresses = sorted(self.candidates(peer), key=lambda value: scopes.index(route_scope(value)))
        for address in addresses:
            host = valid_host(urlparse(address).hostname or "")
            if not host:
                continue
            try:
                ipaddress.ip_address(host)
                return host
            except ValueError:
                try:  # emulators generally want an IP; fall back to the name if it will not resolve
                    return socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
                except OSError:
                    return host
        return ""

    def post(self, peer: dict, endpoint: str, payload: dict, *, timeout: float = 5.0) -> dict:
        body, _address = self._post(peer, f"{PEER_API}{endpoint}", payload, self.settings, peer_id=self.peer_id(peer),
                                    config=PEER_CONFIG, timeout=timeout)
        return body

    def post_checked(self, peer: dict, endpoint: str, payload: dict, *, timeout: float = 6.0) -> dict:
        """POST and translate every failure into a ``MultiplayerError`` a person can read."""
        try:
            return self.post(peer, endpoint, payload, timeout=timeout)
        except HTTPError as error:
            message, code = f"The other Drone rejected the request (HTTP {error.code}).", "peer_rejected"
            try:
                body = json.loads(error.read(8192).decode("utf-8", "replace"))
                if isinstance(body, dict):
                    message = m.clean_text(body.get("error") or message, 300)
                    code = m.clean_text(body.get("code") or code, 40)
            except (ValueError, OSError):
                pass
            raise m.MultiplayerError(code, message, error.code if 400 <= error.code < 600 else 502) from error
        except (OSError, URLError, ValueError) as error:
            raise m.MultiplayerError("peer_unreachable", f"Could not reach the other Drone: {str(error)[:160] or type(error).__name__}", 502) from error

    def get(self, peer: dict, endpoint: str, *, timeout: float = 5.0) -> dict:
        body, _address = self._get(peer, f"{PEER_API}{endpoint}", self.settings, peer_id=self.peer_id(peer),
                                   config=PEER_CONFIG, timeout=timeout)
        return body

    def test_peer(self, peer: dict, samples: int = 3) -> dict:
        """Authenticated reachability + median round-trip time through the normal peer route."""
        timings = []
        error = ""
        address = ""
        for _ in range(max(1, samples)):
            started = self._clock()
            try:
                _body, address = self._get(peer, "/v1/api/peer/health", self.settings, peer_id=self.peer_id(peer),
                                           config=PEER_CONFIG, timeout=4.0)
                timings.append((self._clock() - started) * 1000.0)
            except Exception as failure:  # noqa: BLE001 - report, never raise, from a diagnostic
                error = str(failure)[:200] or type(failure).__name__
                break
        scope = route_scope(address) if address else self.peer_scope(peer)
        result = {"peer_id": self.peer_id(peer), "reachable": bool(timings), "scope": scope,
                  "address": urlparse(address).hostname or "" if address else "", "error": "" if timings else (error or "no route"),
                  "latency_ms": round(statistics.median(timings), 1) if timings else None,
                  "samples": len(timings), "advice": []}
        if not timings:
            result["advice"].append("The peer did not answer over any known route. Check that it is online and paired, "
                                    "and that you share a LAN or a tailnet.")
        else:
            if scope == "tailnet":
                result["advice"].append("Connected over Tailscale: native netplay works; LAN-broadcast games will not discover each other.")
            if result["latency_ms"] and result["latency_ms"] > 100:
                result["advice"].append("Round-trip above 100 ms: rollback and streaming will feel laggy.")
        return result

    # --------------------------------------------------------------- ports
    def allocate_port(self, start: int, end: int, in_use: Set[int]) -> int:
        for port in range(max(1, int(start)), min(65535, int(end)) + 1):
            if port in in_use:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind(("0.0.0.0", port))
                except OSError:
                    continue
            return port
        raise m.MultiplayerError("no_free_port", f"No free session port between {start} and {end}. Widen the range in Multiplayer settings.", 409)
