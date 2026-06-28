"""AssetFetch: move one asset's bytes over a PeerChannel.

A :class:`PeerChannel` is a reliable, ordered, bidirectional byte stream between
two Drones. Address-based transports (LAN, direct-public) reuse the existing mTLS
``/peer/*`` HTTP path with Range requests; the transports that are *not* HTTP --
the Overmind relay (Phase 2) and, later, hole-punched QUIC -- need an equivalent
way to pull an asset with the same capabilities (resumable offset, integrity
hash, cancellation). AssetFetch is that small request/response protocol.

Wire format reuses the mux frame codec ([1-byte type][4-byte BE length][payload]):

    FETCH   receiver -> sender   JSON  {"asset": <ref>, "offset": <int>}
    CHUNK   sender   -> receiver binary file bytes
    DONE    sender   -> receiver JSON  {"size": <int>, "hash": <str|None>}
    ERR     sender   -> receiver JSON  {"code": <str>, "message": <str>}
    CANCEL  receiver -> sender   JSON  {}

The protocol is intentionally dumb about *what* an asset is: the receiver passes
an opaque ``asset`` mapping (an AssetRef) and a per-chunk ``write`` callback; the
sender side resolves the ref to a byte source. File placement, fingerprint
verification and skip-if-present stay in the calling transport, exactly as they
do for the HTTP path.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Tuple

from . import mux

# AssetFetch message types (the frame "kind" byte). Distinct from mux FRAME_*
# because these ride *inside* a relay data stream, a separate layer.
AF_FETCH = 0x11
AF_CHUNK = 0x12
AF_DONE = 0x13
AF_ERR = 0x14
AF_CANCEL = 0x15

#: Default read size when streaming a file out (256 KiB).
DEFAULT_CHUNK_SIZE = 256 * 1024


class AssetFetchError(RuntimeError):
    """Sender reported an error, or the protocol was violated."""


class AssetFetchCancelled(RuntimeError):
    """The receiver cancelled the transfer."""


class PeerChannel(Protocol):
    """A reliable, ordered, bidirectional byte stream between two Drones."""

    def send(self, data: bytes) -> None: ...

    def read_exactly(self, n: int) -> bytes: ...

    def close(self) -> None: ...


# ---- Codec ---------------------------------------------------------------

def _encode_json(message_type: int, payload: Mapping[str, Any]) -> bytes:
    return mux.encode_frame(message_type, json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def encode_fetch(asset: Mapping[str, Any], offset: int = 0) -> bytes:
    return _encode_json(AF_FETCH, {"asset": dict(asset), "offset": int(offset)})


def encode_chunk(data: bytes) -> bytes:
    return mux.encode_frame(AF_CHUNK, data)


def encode_done(size: int, hash_value: Optional[str] = None) -> bytes:
    return _encode_json(AF_DONE, {"size": int(size), "hash": hash_value})


def encode_err(code: str, message: str = "") -> bytes:
    return _encode_json(AF_ERR, {"code": code, "message": message})


def encode_cancel() -> bytes:
    return _encode_json(AF_CANCEL, {})


def read_message(read_exactly: Callable[[int], bytes]) -> Tuple[int, bytes]:
    """Read one AssetFetch message; returns ``(type, payload)``."""
    return mux.read_frame(read_exactly)


def decode_json(payload: bytes) -> dict:
    return mux.decode_control(payload)


# ---- Receiver ------------------------------------------------------------

def download(
    channel: PeerChannel,
    asset: Mapping[str, Any],
    write: Callable[[bytes], None],
    *,
    offset: int = 0,
    progress: Optional[Callable[[int], None]] = None,
    cancel: Optional[Any] = None,  # threading.Event-like (.is_set())
) -> dict:
    """Pull ``asset`` from the sender over ``channel``, calling ``write`` per chunk.

    Returns the DONE metadata (``{"size", "hash"}``). Raises
    :class:`AssetFetchCancelled` if ``cancel`` fires and :class:`AssetFetchError`
    on a sender error or protocol violation. ``progress`` receives the running
    total of bytes received (including the starting ``offset``).
    """
    channel.send(encode_fetch(asset, offset))
    received = int(offset)
    while True:
        if cancel is not None and cancel.is_set():
            try:
                channel.send(encode_cancel())
            except OSError:
                pass
            raise AssetFetchCancelled("cancelled by receiver")
        message_type, payload = read_message(channel.read_exactly)
        if message_type == AF_CHUNK:
            if payload:
                write(payload)
                received += len(payload)
                if progress is not None:
                    progress(received)
        elif message_type == AF_DONE:
            return decode_json(payload)
        elif message_type == AF_ERR:
            detail = decode_json(payload)
            raise AssetFetchError(
                f"{detail.get('code') or 'error'}: {detail.get('message') or ''}".strip(": ")
            )
        else:
            raise AssetFetchError(f"unexpected message type during download: {message_type:#x}")


# ---- Sender --------------------------------------------------------------

#: Resolve an asset ref + offset to ``(byte_iterable, {"size", "hash"})`` or None
#: when the asset is unavailable. The iterable yields the file bytes from offset.
AssetResolver = Callable[[Mapping[str, Any], int], Optional[Tuple[Iterable[bytes], Mapping[str, Any]]]]


def serve_one(channel: PeerChannel, resolve: AssetResolver) -> dict:
    """Handle a single FETCH on ``channel``: stream the asset then send DONE.

    ``resolve(asset, offset)`` returns a ``(byte_iterable, meta)`` pair, or None
    to report the asset is unavailable. Returns a small result dict for logging.
    """
    message_type, payload = read_message(channel.read_exactly)
    if message_type == AF_CANCEL:
        return {"status": "cancelled", "bytes": 0}
    if message_type != AF_FETCH:
        channel.send(encode_err("protocol", f"expected fetch, got {message_type:#x}"))
        return {"status": "error", "bytes": 0}

    request = decode_json(payload)
    asset = request.get("asset") or {}
    offset = int(request.get("offset") or 0)
    resolved = resolve(asset, offset)
    if resolved is None:
        channel.send(encode_err("not_found", "asset unavailable"))
        return {"status": "error", "bytes": 0}

    source, meta = resolved
    sent = 0
    for chunk in source:
        if not chunk:
            continue
        channel.send(encode_chunk(bytes(chunk)))
        sent += len(chunk)
    channel.send(encode_done(int(meta.get("size", sent)), meta.get("hash")))
    return {"status": "completed", "bytes": sent}
