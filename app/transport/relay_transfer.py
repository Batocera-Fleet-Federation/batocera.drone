"""Relayed transfer flows on top of the mux + AssetFetch.

Two halves, both running over the Drone's single outbound mux to the Edge:

* **Sender** (:func:`serve_asset`): on a TRANSFER_OFFER, open a sender relay leg
  and stream the requested asset out with :func:`assetfetch.serve_one`.
* **Receiver** (:func:`open_receiver_channel`): register a receiver leg, ask the
  Edge to offer the transfer to the sender, wait until both legs are paired, and
  return the :class:`RelayChannel` for the caller to run
  :func:`assetfetch.download` on.

:func:`open_local_file_source` maps an asset ref to local file bytes for the
sender, path-safely (no traversal, must stay under the given root). The byte
transfer never touches the control plane; it is relayed Drone-to-Drone.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Optional, Tuple

from . import assetfetch
from .base import DownloadRequest, PeerTransport, TransferContext


def open_local_file_source(
    root: Any,
    relative_path: str,
    offset: int = 0,
    *,
    chunk_size: int = assetfetch.DEFAULT_CHUNK_SIZE,
) -> Optional[Tuple[Iterable[bytes], dict]]:
    """Return ``(byte_iterable, {"size", "hash"})`` for ``relative_path`` under
    ``root``, starting at ``offset``; or None if the path is unsafe or missing.

    ``size`` is the full file size (so the receiver can show progress even when
    resuming); ``hash`` is None -- the receiver verifies against the expected
    fingerprint it already holds, exactly as on the HTTP path.
    """
    rel = str(relative_path or "").replace("\\", "/").lstrip("/")
    if not rel or ".." in PurePosixPath(rel).parts:
        return None
    root_path = Path(root).resolve()
    target = (root_path / rel).resolve()
    if target == root_path or root_path not in target.parents:
        return None
    if not target.is_file():
        return None
    size = target.stat().st_size
    start = max(0, int(offset or 0))

    def chunks() -> Iterable[bytes]:
        with open(target, "rb") as handle:
            if start:
                handle.seek(start)
            while True:
                buffer = handle.read(chunk_size)
                if not buffer:
                    break
                yield buffer

    return chunks(), {"size": size, "hash": None}


def serve_asset(
    mux_client: Any,
    session_id: str,
    resolve: assetfetch.AssetResolver,
    *,
    ready_timeout: float = 20.0,
) -> dict:
    """Sender side: open a sender leg for ``session_id`` and serve the asset.

    ``resolve(asset, offset)`` returns the local byte source (see
    :func:`open_local_file_source`) or None when unavailable.
    """
    channel = mux_client.open_relay_session(session_id, "sender", ready_timeout=ready_timeout)
    try:
        return assetfetch.serve_one(channel, resolve)
    finally:
        channel.close()


def open_receiver_channel(
    mux_client: Any,
    session_id: str,
    token: str,
    from_device: str,
    asset: Mapping[str, Any],
    *,
    ready_timeout: float = 20.0,
):
    """Receiver side: register a receiver leg, request the transfer, and wait for
    both legs to pair. Returns the :class:`RelayChannel` ready for
    :func:`assetfetch.download`. Raises on timeout or rejection."""
    channel = mux_client.start_relay_session(session_id, "receiver")
    try:
        mux_client.send_transfer_request(session_id, token, from_device, dict(asset))
    except Exception:
        mux_client.close_relay_session(session_id)
        raise
    if not channel.wait_ready(ready_timeout):
        mux_client.close_relay_session(session_id)
        raise TimeoutError(f"relay sender did not become ready for session {session_id}")
    if channel.failed:
        mux_client.close_relay_session(session_id)
        raise ConnectionError(f"relay transfer rejected by Edge for session {session_id}")
    return channel


class RelayReceiverTransport(PeerTransport):
    """Receiver-side relay transport: pull an asset from a peer via the Edge relay.

    Plugged into the TransportSelector as a fallback after the direct path. The
    actual fetch (session minting, channel, AssetFetch, disk write + verify) is
    injected from ``drone_api`` as ``fetch_fn`` to avoid an import cycle.
    """

    name = "relay"
    #: v1 relays ROM files; other asset types fall back to other transports.
    SUPPORTED_ASSET_TYPES = ("rom",)

    def __init__(
        self,
        fetch_fn: Callable[[DownloadRequest, TransferContext], dict],
        *,
        is_available: Callable[[], bool],
    ) -> None:
        self._fetch_fn = fetch_fn
        self._is_available = is_available

    def usable(self, request: DownloadRequest, context: TransferContext) -> bool:
        if request.asset_type not in self.SUPPORTED_ASSET_TYPES:
            return False
        if not self._is_available():
            return False
        peer = context.peer or {}
        return bool(peer.get("drone_id") or peer.get("device_id"))

    def fetch(self, request: DownloadRequest, context: TransferContext) -> dict:
        return self._fetch_fn(request, context)
