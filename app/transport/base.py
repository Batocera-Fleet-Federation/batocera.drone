"""Core types for the Drone peer-transfer transport abstraction.

A :class:`PeerTransport` fetches one asset (ROM / BIOS / artwork / save) from a
peer Drone and returns the same ``activity`` dict the legacy
``_download_*_from_peer`` helpers produce, so the download queue stays agnostic
to which mechanism actually moved the bytes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from threading import Event
from typing import Any, Callable, Optional


#: ``(downloaded_bytes, total_bytes_or_None)`` progress reporter.
ProgressCallback = Callable[[int, Optional[int]], None]


@dataclass
class DownloadRequest:
    """Transport-agnostic description of one asset to fetch from a peer.

    Mirrors the per-asset arguments the queue previously spread across the
    individual ``_download_*_from_peer`` calls. ``expected_fingerprint`` carries
    the ROM/save fingerprint or the BIOS md5 depending on ``asset_type`` -- the
    transport passes it straight through so the shared skip-if-present and hash
    verification logic is unchanged.
    """

    asset_type: str  # "rom" | "bios" | "saves" | "artwork"
    system: str = ""
    relative_path: str = ""
    rom_path: str = ""  # artwork only (the ROM the artwork belongs to)
    artwork_type: str = ""  # artwork only
    entry_type: str = "file"  # "file" | "folder" (ROM only)
    expected_size: Optional[int] = None
    expected_fingerprint: Optional[str] = None  # fingerprint (rom/save) or md5 (bios)
    overwrite: bool = False  # artwork only
    local_rom_path: Optional[str] = None  # artwork only


@dataclass
class TransferContext:
    """Runtime collaborators a transport needs to perform a fetch."""

    settings: Any
    repository: Any
    config: dict
    peer: dict
    progress_callback: Optional[ProgressCallback] = None
    cancellation_event: Optional[Event] = None


class PeerTransport(ABC):
    """A mechanism for fetching an asset from a peer Drone."""

    #: Stable identifier surfaced in logs / activity records.
    name: str = "peer"

    def usable(self, request: DownloadRequest, context: TransferContext) -> bool:
        """Return True if this transport can serve ``request`` for the peer.

        The Phase 0 default is permissive (always usable). Later transports
        (relay / LAN / hole-punch) override this so :class:`TransportSelector`
        can rank candidates and fall back between them.
        """
        return True

    @abstractmethod
    def fetch(self, request: DownloadRequest, context: TransferContext) -> dict:
        """Fetch the asset and return an activity dict (same shape as before)."""
        raise NotImplementedError
