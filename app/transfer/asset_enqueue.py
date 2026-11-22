"""Per-item "copy this peer asset here" logic, as a mixin.

Extracted from ``web/handlers_network.py`` so the Swarm sync routes (``/admin/local-network/sync``
and ``-bulk``) and backup asset restore (``backups/asset_restore.py``) queue downloads through
exactly the same rules: skip ROMs already on this machine (content fingerprint, else path), pull a
folder-unit ROM as its folder, queue only the artwork fields that are missing, repair media
folder permissions once per system. The methods stay ``self``-bound (they use ``self.settings`` and
``self.repository``), so ``HandlersNetworkMixin`` call sites and tests that patch
``handler._local_rom_index`` and friends are unchanged.
"""

from pathlib import Path
from typing import Dict, List, Optional

try:
    from ..device.device_control import _ensure_rom_write_access
    from ..roms.gamelist import ARTWORK_FIELDS, _normalize_gamelist_rom_path
except ImportError:  # pragma: no cover - direct script execution fallback
    from device.device_control import _ensure_rom_write_access  # type: ignore
    from roms.gamelist import ARTWORK_FIELDS, _normalize_gamelist_rom_path  # type: ignore


class PeerAssetEnqueueMixin:
    def _local_rom_index(self, system: str, cache: Optional[dict] = None) -> dict:
        """Build a lookup of this machine's ROMs for a system: content
        fingerprint -> relative path, and normalized path -> relative path. Used
        to decide whether a peer ROM already exists locally. Optionally memoized
        in `cache` (system -> index) for bulk operations."""
        if cache is not None and system in cache:
            return cache[system]
        fingerprints: Dict[str, str] = {}
        paths: Dict[str, str] = {}
        names_by_size: Dict[tuple, str] = {}
        try:
            _, rows = self.repository.list_assets(system, "roms")
        except Exception:
            rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            rel = str(row.get("relative_path") or row.get("rom_path") or row.get("file_path") or "")
            rel_norm = rel.replace("\\", "/").lstrip("./").lower()
            fp = str(row.get("fingerprint") or row.get("rom_fingerprint") or "").strip().lower()
            if rel_norm:
                paths.setdefault(rel_norm, rel)
                size = row.get("byte_count") or row.get("file_size") or row.get("size")
                try:
                    size_int = int(size)
                except (TypeError, ValueError):
                    size_int = -1
                names_by_size.setdefault((Path(rel_norm).name, size_int), rel)
            if fp:
                fingerprints.setdefault(fp, rel)
        system_dir = (self.settings.roms_root if getattr(self, "settings", None) else self.repository.roms_root) / system
        if system_dir.exists() and system_dir.is_dir():
            for entry in sorted(system_dir.rglob("*"), key=lambda path: path.relative_to(system_dir).as_posix().lower()):
                if not entry.is_file():
                    continue
                rel = entry.relative_to(system_dir).as_posix()
                if self.repository.should_ignore_rom_path(Path(rel)):
                    continue
                rel_norm = rel.replace("\\", "/").lstrip("./").lower()
                try:
                    size_int = int(entry.stat().st_size)
                except OSError:
                    continue
                paths.setdefault(rel_norm, rel)
                names_by_size.setdefault((Path(rel_norm).name, size_int), rel)
                try:
                    fingerprints.setdefault(self.repository.build_fingerprint(entry).lower(), rel)
                except Exception:
                    continue
        index = {"fingerprints": fingerprints, "paths": paths, "names_by_size": names_by_size}
        if cache is not None:
            cache[system] = index
        return index

    def _local_artwork_index(self, system: str, cache: Optional[dict] = None) -> dict:
        """Normalized ROM path -> set of artwork fields already present locally for a
        system. Memoized in `cache` (system -> index) for bulk operations so the
        gamelist is parsed once per system."""
        if cache is not None and system in cache:
            return cache[system]
        try:
            index = self.repository.list_present_artwork(system)
        except Exception:
            index = {}
        if cache is not None:
            cache[system] = index
        return index

    def _ensure_system_writable_once(self, system: str) -> None:
        """Ensure this ROM system's media dirs + gamelist are writable by the Drone,
        at most once per request (a bulk copy touches one system many times)."""
        system = str(system or "").strip()
        if not system:
            return
        done = getattr(self, "_perm_repaired_systems", None)
        if done is None:
            done = set()
            self._perm_repaired_systems = done
        if system in done:
            return
        done.add(system)
        try:
            _ensure_rom_write_access(self.settings, system)
        except Exception:
            pass

    def _match_local_rom(self, index: dict, item: dict) -> Optional[str]:
        """Return the local relative path of a ROM matching this peer item, or
        None. Prefers a content-fingerprint (thumbprint) match; falls back to a
        path match only when no fingerprint is available to compare."""
        peer_fp = str(item.get("rom_fingerprint") or item.get("fingerprint") or "").strip().lower()
        if peer_fp:
            match = index.get("fingerprints", {}).get(peer_fp)
            if match:
                return match
        rel = str(item.get("relative_path") or item.get("rom_path") or item.get("file_path") or "")
        rel_norm = rel.replace("\\", "/").lstrip("./").lower()
        match = index.get("paths", {}).get(rel_norm)
        if match:
            return match
        size = item.get("byte_count") or item.get("file_size") or item.get("size")
        try:
            size_int = int(size)
        except (TypeError, ValueError):
            size_int = -1
        if size_int >= 0 and rel_norm:
            return index.get("names_by_size", {}).get((Path(rel_norm).name, size_int))
        return None

    def _enqueue_local_asset(
        self,
        manager: "DownloadManager",
        config: dict,
        peer: dict,
        asset_type: str,
        item: dict,
        default_system: str = "",
        include_artwork: bool = True,
        include_roms: bool = True,
        overwrite_files: Optional[bool] = None,
        overwrite_artwork: Optional[bool] = None,
        artwork_only: bool = False,
        local_index_cache: Optional[dict] = None,
        local_artwork_cache: Optional[dict] = None,
        artwork_job_overwrite: Optional[bool] = None,
    ) -> List[dict]:
        """Enqueue a single peer asset (and, for ROMs, its artwork when present).

        ROMs already present on this machine (matched by content fingerprint, or
        path when no fingerprint is available) are skipped unless `overwrite_files`
        is enabled; their artwork can still be copied and linked into the local
        gamelist. With `include_roms` disabled, artwork is attached only to ROMs
        already on this machine. `overwrite_artwork` and `artwork_only` remain as
        compatibility aliases for older Drone clients.
        `local_index_cache`/`local_artwork_cache` (system -> index) let the bulk path
        reuse the local ROM and artwork lookups across many items.
        `artwork_job_overwrite` is the ``overwrite`` flag of each queued artwork job (default
        True: a re-copy replaces the deterministic ``<rom>-<field>`` file). Backup asset
        restore passes False so an existing media file is never replaced."""
        overwrite_artwork_files = (
            bool(overwrite_files)
            if overwrite_files is not None
            else (True if overwrite_artwork is None else bool(overwrite_artwork))
        )
        # Older clients only controlled artwork replacement; they never requested
        # replacement of ROM, BIOS, or save files.
        overwrite_files = bool(overwrite_files) if overwrite_files is not None else False
        if artwork_only:
            include_roms = False
            include_artwork = True
        jobs: List[dict] = []
        if asset_type == "roms":
            system = str(item.get("system") or default_system or "").strip()
            relative_path = str(item.get("relative_path") or item.get("rom_path") or item.get("file_path") or "").strip()
            if not relative_path:
                return jobs
            index = self._local_rom_index(system, cache=local_index_cache)
            local_match = self._match_local_rom(index, item)
            if not include_roms:
                if local_match is None:
                    return jobs
                art_local_path = local_match
            else:
                pending_match = (
                    manager.find_pending_rom(system, relative_path, item.get("rom_fingerprint") or item.get("fingerprint"))
                    if hasattr(manager, "find_pending_rom")
                    else None
                )
                if (local_match is None or overwrite_files) and pending_match is None:
                    # Not on this machine yet -> download it. Folder-unit ROMs (marker
                    # file in a per-game folder) pull the folder; relative_path stays
                    # the marker (the gamelist identity + artwork key).
                    transfer_rel = relative_path
                    marker_rel = None
                    if str(item.get("entry_type") or "").strip().lower() == "folder" and item.get("transfer_unit_path"):
                        transfer_rel = str(item.get("transfer_unit_path") or "").strip() or relative_path
                        marker_rel = str(item.get("marker_relative_path") or relative_path).strip() or None
                    jobs.append(manager.enqueue_rom(
                        config,
                        peer,
                        system,
                        transfer_rel,
                        expected_size=item.get("byte_count") or item.get("file_size"),
                        expected_fingerprint=item.get("rom_fingerprint") or item.get("fingerprint"),
                        entry_type=str(item.get("entry_type") or "file"),
                        marker_relative_path=marker_rel,
                        overwrite=overwrite_files,
                    ))
                    art_local_path = relative_path
                else:
                    # Already present (thumbprint match) -> skip the ROM, attach artwork
                    # to the existing local ROM so it shows after a gamelist refresh.
                    art_local_path = local_match or relative_path
            if include_artwork:
                gamelist = item.get("gamelist") if isinstance(item.get("gamelist"), dict) else {}
                fields = [field for field in ARTWORK_FIELDS if gamelist.get(field)]
                if not overwrite_artwork_files and fields:
                    # Skip artwork the local ROM already has -- only fetch what's
                    # missing so user-curated/scraped art isn't clobbered.
                    present = self._local_artwork_index(system, cache=local_artwork_cache).get(
                        _normalize_gamelist_rom_path(art_local_path).lower(), set()
                    )
                    fields = [field for field in fields if field not in present]
                if fields:
                    # Make the target system's media dirs + gamelist.xml writable
                    # for the unprivileged Drone before the artwork jobs run. Done
                    # once per system, synchronously, so the download worker doesn't
                    # race ahead of the privileged permission repair.
                    self._ensure_system_writable_once(system)
                for field in fields:
                    try:
                        jobs.append(manager.enqueue_artwork(
                            config, peer, system, relative_path, field,
                            overwrite=True if artwork_job_overwrite is None else bool(artwork_job_overwrite),
                            local_rom_path=art_local_path,
                        ))
                    except Exception:
                        continue
        elif asset_type == "bios":
            relative_path = str(item.get("path") or item.get("relative_path") or item.get("file_path") or "").strip()
            if not relative_path:
                return jobs
            jobs.append(manager.enqueue_bios(
                config,
                peer,
                relative_path,
                expected_size=item.get("byte_count") or item.get("file_size"),
                expected_md5=item.get("bios_md5") or item.get("md5"),
                overwrite=overwrite_files,
            ))
        elif asset_type == "artwork":
            artwork_types = item.get("artwork_types")
            if isinstance(artwork_types, list):
                default_artwork_type = str(artwork_types[0] if artwork_types else "image")
            else:
                default_artwork_type = str(artwork_types or "image")
            system = str(item.get("system") or default_system or "")
            self._ensure_system_writable_once(system)
            jobs.append(manager.enqueue_artwork(
                config,
                peer,
                system,
                str(item.get("rom_path") or item.get("file_path") or ""),
                str(item.get("artwork_type") or default_artwork_type),
                overwrite=overwrite_files,
            ))
        elif asset_type == "saves":
            relative_path = str(item.get("file_path") or item.get("relative_path") or "").strip()
            if not relative_path:
                return jobs
            jobs.append(manager.enqueue_save(
                config,
                peer,
                str(item.get("system") or default_system or ""),
                relative_path,
                expected_size=item.get("file_size"),
                expected_fingerprint=item.get("saves_fingerprint") or item.get("fingerprint"),
                overwrite=overwrite_files,
            ))
        elif asset_type == "movies":
            relative_path = str(item.get("file_path") or item.get("relative_path") or "").strip()
            if not relative_path:
                return jobs
            jobs.append(manager.enqueue_movie(
                config,
                peer,
                relative_path,
                expected_size=item.get("file_size") or item.get("byte_count"),
                expected_fingerprint=item.get("movies_fingerprint") or item.get("fingerprint"),
                overwrite=overwrite_files,
            ))
        elif asset_type == "music":
            relative_path = str(item.get("file_path") or item.get("relative_path") or "").strip()
            if not relative_path:
                return jobs
            jobs.append(manager.enqueue_music(
                config,
                peer,
                relative_path,
                expected_size=item.get("file_size") or item.get("byte_count"),
                expected_fingerprint=item.get("music_fingerprint") or item.get("fingerprint"),
                overwrite=overwrite_files,
            ))
        else:
            raise ValueError("asset_type must be roms, bios, artwork, saves, movies, or music")
        return jobs


class PeerAssetEnqueuer(PeerAssetEnqueueMixin):
    """The mixin's methods on a bare object (no HTTP request), for callers outside the handlers."""

    def __init__(self, settings, repository) -> None:
        self.settings = settings
        self.repository = repository
