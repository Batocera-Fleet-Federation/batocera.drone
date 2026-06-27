"""The legacy direct mTLS-over-HTTP peer fetch, as a :class:`PeerTransport`.

This wraps the existing ``_download_*_from_peer`` dispatch (injected as a single
callable from ``drone_api`` to avoid an import cycle) so it participates in the
transport selector. It is the path that requires the peer to be reachable at a
public or LAN URL today; it is retained as one transport tier while
outbound-only transports (relay, hole-punch) are added alongside it.
"""

from __future__ import annotations

from typing import Callable

from .base import DownloadRequest, PeerTransport, TransferContext

#: Callable that performs the actual asset-type dispatch + download and returns
#: an activity dict. Supplied by ``drone_api`` so this module stays free of any
#: dependency on the large ``drone_api`` module (no import cycle).
FetchFn = Callable[[DownloadRequest, TransferContext], dict]


class DirectPublicTransport(PeerTransport):
    """Fetch via a direct mTLS HTTP GET against the peer's reachable URL."""

    name = "direct-public"

    def __init__(self, fetch_fn: FetchFn) -> None:
        self._fetch_fn = fetch_fn

    def fetch(self, request: DownloadRequest, context: TransferContext) -> dict:
        return self._fetch_fn(request, context)
