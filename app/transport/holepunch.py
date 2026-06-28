"""UDP NAT hole-punch primitives.

Two drones in different homes (different public IPs, both behind NAT) can often
open a direct UDP path without any port-forward:

1. Each creates a UDP socket and learns that socket's public ``ip:port`` from the
   Edge STUN reflector (:func:`gather_udp_candidate`).
2. They swap those candidates over the mux (MSG_SIGNAL).
3. Both send packets to each other's candidate at the same time; the first NAT to
   send creates an outbound mapping that lets the other's packets in
   (:func:`hole_punch`).

This module is just the path-establishment layer (no reliability). Carrying an
actual asset over the punched socket needs a reliable, ordered channel; see the
note in :class:`HolePunchUnavailable`. Symmetric NATs defeat hole punching
entirely -- callers fall back to the relay.
"""

from __future__ import annotations

import json
import socket
from typing import Optional, Tuple

PUNCH_MAGIC = b"BFF-PUNCH"


class HolePunchUnavailable(RuntimeError):
    """Raised when a direct UDP path could not be established (e.g. symmetric
    NAT). The caller should fall back to the relay transport."""


def parse_addr(value: str) -> Tuple[str, int]:
    """Parse ``"ip:port"`` (IPv4 or ``[ipv6]:port``) into ``(host, port)``."""
    text = str(value or "").strip()
    if text.startswith("["):  # [ipv6]:port
        host, _, port = text[1:].partition("]:")
        return host, int(port)
    host, _, port = text.rpartition(":")
    if not host:
        raise ValueError(f"invalid address: {value!r}")
    return host, int(port)


def gather_udp_candidate(
    stun_addr: Tuple[str, int],
    *,
    timeout: float = 3.0,
    bind_addr: Tuple[str, int] = ("0.0.0.0", 0),
) -> Tuple[socket.socket, str]:
    """Create a UDP socket and learn its reflexive ``ip:port`` from the Edge STUN
    reflector. Returns ``(socket, reflexive_addr)``; the socket stays open for the
    subsequent punch. Raises on timeout / bad reply (caller falls back to relay)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(bind_addr)
        sock.settimeout(timeout)
        sock.sendto(b"bind", stun_addr)
        data, _ = sock.recvfrom(512)
        info = json.loads(data.decode("utf-8"))
        reflexive = f"{info['ip']}:{int(info['port'])}"
    except (OSError, ValueError, KeyError) as error:
        sock.close()
        raise HolePunchUnavailable(f"STUN candidate gathering failed: {error}") from error
    return sock, reflexive


def hole_punch(
    sock: socket.socket,
    peer_addr: Tuple[str, int],
    *,
    attempts: int = 20,
    interval: float = 0.2,
) -> bool:
    """Punch toward ``peer_addr``: repeatedly send while listening for the peer's
    packet. Returns True once a packet from the peer is seen (path open)."""
    sock.settimeout(interval)
    ack = PUNCH_MAGIC + b"-ACK"
    for _ in range(max(1, attempts)):
        try:
            sock.sendto(PUNCH_MAGIC, peer_addr)
        except OSError:
            pass
        try:
            data, addr = sock.recvfrom(512)
        except socket.timeout:
            continue
        except OSError:
            continue
        if addr == peer_addr or data.startswith(PUNCH_MAGIC):
            # Send an ACK so the peer (which may still be sending) also confirms.
            try:
                sock.sendto(ack, peer_addr)
            except OSError:
                pass
            return True
    return False
