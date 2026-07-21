"""Peer-transfer transport abstraction for the Drone download queue.

Historically the download queue called the module-level ``_download_*_from_peer``
helpers directly, hard-wiring every transfer to one mechanism: a direct mTLS
HTTP GET against the peer's reachable URL (which requires the peer to be
reachable -- i.e. port-forwarded -- on the public internet).

This package introduces a thin seam so additional transports (LAN/tailnet-direct,
hole-punched P2P) can be added behind a single interface without the queue
knowing which is in use. Phase 0 ships only the existing direct path
(:class:`DirectPublicTransport`) wired into a one-element selector, so behavior
is unchanged.
"""

from .base import (
    DownloadRequest,
    PeerTransport,
    ProgressCallback,
    TransferContext,
)
from .direct_public import DirectPublicTransport
from .selector import TransportSelector

__all__ = [
    "DownloadRequest",
    "PeerTransport",
    "ProgressCallback",
    "TransferContext",
    "DirectPublicTransport",
    "TransportSelector",
]
