"""Chooses the best :class:`PeerTransport` for a transfer, with ordered fallback."""

from __future__ import annotations

from typing import List, Optional, Sequence

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

    def usable_transports(self, request: DownloadRequest, context: TransferContext) -> List[PeerTransport]:
        usable = [t for t in self._transports if t.usable(request, context)]
        return usable or [self._transports[0]]

    def fetch(self, request: DownloadRequest, context: TransferContext) -> dict:
        """Fetch via the best usable transport, falling back to the next on failure.

        Tries each usable transport in priority order. A cancellation (the job's
        cancellation_event is set) is never retried -- it re-raises immediately so
        a user cancel doesn't silently fall back to another transport. Otherwise
        the last error is raised if every transport fails.
        """
        last_error: Optional[BaseException] = None
        for transport in self.usable_transports(request, context):
            try:
                return transport.fetch(request, context)
            except Exception as error:  # noqa: BLE001 -- try the next transport
                last_error = error
                cancel = context.cancellation_event
                if cancel is not None and cancel.is_set():
                    raise
        if last_error is not None:
            raise last_error
        raise RuntimeError("no transport available")
