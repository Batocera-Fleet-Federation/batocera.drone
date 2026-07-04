"""Heartbeat-driven asset/saves resync requests + local thumbprint readers.

Extracted from ``drone_api.py``. Reads the Drone's local romset/BIOS/saves thumbprints
from the ROM-metadata cache and, when an Overmind heartbeat reports thumbprints that
differ, sets the shared push-requested Events (in ``common.runtime_state``) and wakes the
poller so the next metadata poll pushes a full resync.
"""

import sys
from typing import Tuple

try:
    from ..common.runtime_state import _ASSET_PUSH_REQUESTED, _ROM_METADATA_WAKE, _SAVES_PUSH_REQUESTED
    from ..common.settings import Settings
    from ..storage.rom_metadata_store import _read_rom_metadata_cache_state
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.runtime_state import _ASSET_PUSH_REQUESTED, _ROM_METADATA_WAKE, _SAVES_PUSH_REQUESTED  # type: ignore
    from common.settings import Settings  # type: ignore
    from storage.rom_metadata_store import _read_rom_metadata_cache_state  # type: ignore


def _local_asset_thumbprints(settings: Settings) -> Tuple[str, str]:
    """Return the (romset, bios) thumbprints the Drone last persisted for its on-disk assets."""
    try:
        state = _read_rom_metadata_cache_state(
            settings,
            "romset_files_thumbprint",
            "bios_files_thumbprint",
            "rom_inventory_fingerprint",
        )
    except Exception:
        return "", ""
    romset = str(state.get("romset_files_thumbprint") or state.get("rom_inventory_fingerprint") or "").strip()
    bios = str(state.get("bios_files_thumbprint") or "").strip()
    return romset, bios


def _snapshot_asset_thumbprints(snapshot: dict) -> Tuple[str, str]:
    romset = str(snapshot.get("romset_files_thumbprint") or snapshot.get("rom_inventory_fingerprint") or "").strip()
    bios = str(snapshot.get("bios_files_thumbprint") or "").strip()
    return romset, bios


def _maybe_request_asset_push_from_heartbeat(settings: Settings, response: dict) -> None:
    """Compare Overmind-echoed asset thumbprints with the Drone's local thumbprints.

    When Overmind reports a thumbprint that differs from what the Drone last synced,
    Overmind's stored asset set has drifted (or it never received ours). Flag a push so
    the next metadata poll uploads a full inventory and resyncs, and wake the poller so it
    happens promptly instead of waiting out the full poll interval. Only fires when the
    Drone actually has local assets, so a fresh Drone's initial upload still flows through
    the normal poller path.
    """
    if not isinstance(response, dict):
        return
    overmind_romset = str(response.get("romset_files_thumbprint") or "").strip()
    overmind_bios = str(response.get("bios_files_thumbprint") or "").strip()
    if not overmind_romset and not overmind_bios:
        return
    local_romset, local_bios = _local_asset_thumbprints(settings)
    if not local_romset:
        return
    romset_mismatch = overmind_romset != local_romset
    # Only treat BIOS as drifted when Overmind actually reported a BIOS thumbprint;
    # an older Overmind that never sends one should not trigger endless pushes.
    bios_mismatch = bool(overmind_bios) and overmind_bios != local_bios
    if not romset_mismatch and not bios_mismatch:
        return
    if _ASSET_PUSH_REQUESTED.is_set():
        return
    _ASSET_PUSH_REQUESTED.set()
    _ROM_METADATA_WAKE.set()
    print(
        "Asset thumbprint mismatch from heartbeat; queued resync push: "
        f"romset_mismatch={romset_mismatch} bios_mismatch={bios_mismatch} "
        f"overmind_romset={overmind_romset[:12]} local_romset={local_romset[:12]} "
        f"overmind_bios={overmind_bios[:12]} local_bios={local_bios[:12]}",
        file=sys.stdout,
        flush=True,
    )


def _local_saves_thumbprint(settings: Settings) -> str:
    """Compatibility shim: saves are no longer reported to Overmind heartbeats."""
    return ""


def _maybe_request_saves_push_from_heartbeat(settings: Settings, response: dict) -> None:
    """Compatibility shim: saves are no longer pushed as UI metadata."""
    _SAVES_PUSH_REQUESTED.clear()
