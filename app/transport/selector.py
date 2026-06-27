"""Chooses the best :class:`PeerTransport` for a transfer, with ordered fallback."""

from __future__ import annotations

from typing import List, Sequence

from .base import DownloadRequest, PeerTransport, TransferContext


class TransportSelector:
    """Holds transports in priority order and returns the first usable one.

    Phase 0 is constructed with a single transport (DirectPublic), so
    :meth:`select` always returns it -- byte-identical to the previous
    hard-wired path. Later phases register LAN / hole-punch / relay transports
    ahead of or behind it and the selector ranks them per request via
    :meth:`PeerTransport.usable`.
    """

    def __init__(self, transports: Sequence[PeerTransport]) -> None:
        self._transports: List[PeerTransport] = list(transports)
        if not self._transports:
            raise ValueError("TransportSelector requires at least one transport")

    @property
    def transports(self) -> List[PeerTransport]:
        return list(self._transports)

    def select(self, request: DownloadRequest, context: TransferContext) -> PeerTransport:
        for transport in self._transports:
            if transport.usable(request, context):
                return transport
        # Preserve legacy behavior: fall back to the first (highest-priority)
        # transport rather than failing the job before the underlying helper can
        # raise its own descriptive error.
        return self._transports[0]
