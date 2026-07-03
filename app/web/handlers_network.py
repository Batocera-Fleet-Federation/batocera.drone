"""RomRequestHandler network + local-network admin handlers, as a mixin.

Extracted from ``drone_api.py``. Overmind status, network-mode get/update, and the
local-network (LAN P2P) endpoints: status/discover, pairing-code rotate, peer
pair/forget/assets, and local sync (single + bulk). Composed onto ``RomRequestHandler``.
"""

import os
import ssl
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote, unquote

try:
    from ..device.device_control import _ensure_rom_write_access
    from ..overmind.overmind_config import build_overmind_status
    from ..roms.gamelist import ARTWORK_FIELDS, _normalize_gamelist_rom_path
    from ..transfer import local_network as _local_network
    from ..transfer.drone_network import _network_mode
    from ..transfer.drone_tls import DroneCertificateManager
    from ..transfer.peer_connectivity import (
        _local_pair_peer,
        _local_peer_cert_cache_path,
        _peer_address,
        _peer_get_json,
        _public_local_peer,
    )
except ImportError:  # pragma: no cover - direct script execution fallback
    from device.device_control import _ensure_rom_write_access  # type: ignore
    from overmind.overmind_config import build_overmind_status  # type: ignore
    from roms.gamelist import ARTWORK_FIELDS, _normalize_gamelist_rom_path  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from transfer.drone_network import _network_mode  # type: ignore
    from transfer.drone_tls import DroneCertificateManager  # type: ignore
    from transfer.peer_connectivity import (  # type: ignore
        _local_pair_peer,
        _local_peer_cert_cache_path,
        _peer_address,
        _peer_get_json,
        _public_local_peer,
    )

# Local copy (drone_api keeps its own; same env). Not test-patched.
PEER_INVENTORY_TIMEOUT_SECONDS = float(os.environ.get("DRONE_PEER_INVENTORY_TIMEOUT_SECONDS", "120"))


def _get_download_manager():
    """Delegate to the drone_api singleton accessor (lazy to avoid a cycle)."""
    try:
        from ..drone_api import _get_download_manager as _impl
    except ImportError:  # pragma: no cover - flat execution
        from drone_api import _get_download_manager as _impl  # type: ignore
    return _impl()


class HandlersNetworkMixin:
    def _handle_admin_overmind_status(self) -> None:
        self._send_json(200, build_overmind_status(self.settings))

    def _require_overmind_mode(self) -> bool:
        if _local_network.is_overmind_mode(self.settings):
            return True
        self._send_json(409, {"error": "Overmind integration is disabled"})
        return False

    def _handle_admin_network_mode(self) -> None:
        mode = _network_mode(self.settings)
        integrations = _local_network.get_integrations(self.settings)
        self._send_json(
            200,
            {
                "mode": mode,
                "overmind_active": integrations["overmind_enabled"],
                "local_network_active": integrations["local_network_enabled"],
                "overmind_enabled": integrations["overmind_enabled"],
                "local_network_enabled": integrations["local_network_enabled"],
                "modes": [
                    _local_network.MODE_OVERMIND,
                    _local_network.MODE_LOCAL_NETWORK,
                    _local_network.MODE_BOTH,
                    _local_network.MODE_DISABLED,
                ],
            },
        )

    def _handle_admin_network_mode_update(self, payload: dict) -> None:
        current = _local_network.get_integrations(self.settings)
        if "overmind_enabled" in payload or "local_network_enabled" in payload:
            result = _local_network.set_integrations(
                self.settings,
                overmind_enabled=bool(payload.get("overmind_enabled", current["overmind_enabled"])),
                local_network_enabled=bool(payload.get("local_network_enabled", current["local_network_enabled"])),
            )
        else:
            result = _local_network.set_mode(self.settings, str(payload.get("mode") or ""))
        if result["local_network_enabled"]:
            ssl_context = getattr(self.server, "ssl_context", None)
            if ssl_context is not None:
                ssl_context.verify_mode = ssl.CERT_OPTIONAL
                for peer in _local_network.paired_peers(self.settings):
                    cert_path = Path(str(peer.get("certificate_path") or ""))
                    if cert_path.exists():
                        try:
                            ssl_context.load_verify_locations(cafile=str(cert_path))
                        except ssl.SSLError:
                            continue
            _local_network.announce(self.settings, str(DroneCertificateManager(self.settings).metadata().get("fingerprint") or ""))
        self._handle_admin_network_mode()

    def _local_network_status_payload(self) -> dict:
        hide_seeded_demo = False
        if self.settings.use_fake_data and _local_network.is_local_mode(self.settings):
            discovered_peers = _local_network.discovered_peers(self.settings, include_stale=True)
            visible_peer = next((peer for peer in discovered_peers if not peer.get("fake_data")), None)
            paired_peers = _local_network.paired_peers(self.settings)
            if visible_peer and not any(not peer.get("fake_data") for peer in paired_peers):
                _local_network.forget_peer(self.settings, "fake-local-peer-01")
                _local_network.save_paired_peer(self.settings, {**visible_peer, "fake_data": True})
            hide_seeded_demo = visible_peer is not None
        paired = {str(peer.get("drone_id") or ""): peer for peer in _local_network.paired_peers(self.settings)}
        checks = {
            str(check.get("target_drone_id") or ""): check
            for check in _local_network.load_peer_checks(self.settings)
            if isinstance(check, dict)
        }
        discovered = []
        seen = set()
        for peer in _local_network.discovered_peers(self.settings, include_stale=True):
            peer_id = str(peer.get("drone_id") or "")
            if hide_seeded_demo and peer_id == "fake-local-peer-01":
                continue
            discovered.append(_public_local_peer({**peer, **paired.get(peer_id, {}), "health": checks.get(peer_id)}))
            seen.add(peer_id)
        for peer_id, peer in paired.items():
            if peer_id not in seen:
                discovered.append(_public_local_peer({**peer, "health": checks.get(peer_id)}))
        return {
            "mode": _network_mode(self.settings),
            "active": _local_network.is_local_mode(self.settings),
            "pairing": _local_network.pairing_code(self.settings),
            "peers": discovered,
            "paired_count": len(paired),
            "discovered_count": len(discovered),
            "downloads": _get_download_manager().snapshot() if _get_download_manager() else {},
            "activity": _local_network.load_activity(self.settings),
        }

    def _handle_admin_local_network_status(self) -> None:
        self._send_json(200, self._local_network_status_payload())

    def _handle_admin_local_network_discover(self) -> None:
        if not _local_network.is_local_mode(self.settings):
            self._send_json(409, {"error": "Enable Local Network mode before discovering peers"})
            return
        sent = _local_network.announce(
            self.settings,
            str(DroneCertificateManager(self.settings).metadata().get("fingerprint") or ""),
        )
        payload = self._local_network_status_payload()
        payload["announcement_sent"] = sent
        self._send_json(200, payload)

    def _handle_admin_local_pairing_code_rotate(self) -> None:
        if not _local_network.is_local_mode(self.settings):
            self._send_json(409, {"error": "Enable Local Network mode before pairing"})
            return
        self._send_json(200, {"pairing": _local_network.pairing_code(self.settings, rotate=True)})

    def _handle_admin_local_peer_pair(self, peer_id: str, payload: dict) -> None:
        peer_id = unquote(peer_id)
        if not _local_network.is_local_mode(self.settings):
            self._send_json(409, {"error": "Enable Local Network mode before pairing"})
            return
        peer = next(
            (row for row in _local_network.discovered_peers(self.settings, include_stale=True) if str(row.get("drone_id") or "") == peer_id),
            None,
        )
        if not peer:
            self._send_json(404, {"error": "discovered peer not found"})
            return
        paired = _local_pair_peer(self.settings, peer, str(payload.get("pairing_code") or ""))
        ssl_context = getattr(self.server, "ssl_context", None)
        cert_path = Path(str(paired.get("certificate_path") or ""))
        if ssl_context is not None and cert_path.exists():
            try:
                ssl_context.load_verify_locations(cafile=str(cert_path))
            except ssl.SSLError:
                pass
        self._send_json(200, {"status": "paired", "peer": _public_local_peer(paired)})

    def _handle_admin_local_peer_forget(self, peer_id: str) -> None:
        peer_id = unquote(peer_id)
        removed = _local_network.forget_peer(self.settings, peer_id)
        _local_peer_cert_cache_path(self.settings, peer_id).unlink(missing_ok=True)
        self._send_json(200, {"status": "forgotten" if removed else "not_found", "peer_id": peer_id})

    def _handle_admin_local_peer_assets(self, peer_id: str, query_params: dict) -> None:
        peer_id = unquote(peer_id)
        if not _local_network.is_local_mode(self.settings):
            self._send_json(409, {"error": "Enable Local Network mode before browsing peers"})
            return
        peer = _local_network.get_paired_peer(self.settings, peer_id)
        if not peer:
            self._send_json(404, {"error": "paired peer not found"})
            return
        asset_type = str((query_params.get("type") or ["summary"])[0]).strip().lower()
        if self.settings.use_fake_data and peer.get("fake_data"):
            result = self._collect_peer_inventory(asset_type, query_params)
        else:
            params = []
            for key in ("system", "systems", "q", "limit", "offset"):
                value = str((query_params.get(key) or [""])[0]).strip()
                if value:
                    params.append(f"{quote(key, safe='')}={quote(value, safe='')}")
            address = _peer_address(peer)
            if not address:
                raise ValueError("paired peer has no reachable address")
            suffix = f"?{'&'.join(params)}" if params else ""
            result = _peer_get_json(
                f"{address}/v1/api/peer/inventory/{quote(asset_type, safe='')}{suffix}",
                self.settings,
                peer_id=peer_id,
                config={"network_mode": "local_network"},
                timeout=PEER_INVENTORY_TIMEOUT_SECONDS,
            )
        if asset_type == "roms" and isinstance(result, dict):
            self._annotate_roms_exist_locally(result.get("items") or [])
        self._send_json(200, result)

    def _annotate_roms_exist_locally(self, items: List[dict]) -> None:
        """Flag each peer ROM row with whether it already exists on this machine
        (by content thumbprint) so the UI can show it and skip re-downloading."""
        cache: dict = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            system = str(item.get("system") or "").strip()
            if not system:
                item["exists_locally"] = False
                continue
            index = self._local_rom_index(system, cache=cache)
            item["exists_locally"] = self._match_local_rom(index, item) is not None

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
        overwrite_artwork: bool = True,
        artwork_only: bool = False,
        local_index_cache: Optional[dict] = None,
        local_artwork_cache: Optional[dict] = None,
    ) -> List[dict]:
        """Enqueue a single peer asset (and, for ROMs, its artwork when present).

        ROMs already present on this machine (matched by content fingerprint, or
        path when no fingerprint is available) are NOT re-downloaded; their artwork
        is still copied and linked into the local gamelist. When `overwrite_artwork`
        is False, artwork fields the local ROM already has (referenced in gamelist.xml
        and present on disk) are left untouched -- only missing artwork is fetched.
        When `artwork_only` is True, ROM files are never downloaded and artwork is
        attached only to ROMs that already exist on this machine (peer ROMs missing
        here are skipped entirely). Returns the list of jobs created. Shared by the
        single-item sync and the bulk "copy all" handlers.
        `local_index_cache`/`local_artwork_cache` (system -> index) let the bulk path
        reuse the local ROM and artwork lookups across many items."""
        jobs: List[dict] = []
        if asset_type == "roms":
            system = str(item.get("system") or default_system or "").strip()
            relative_path = str(item.get("relative_path") or item.get("rom_path") or item.get("file_path") or "").strip()
            if not relative_path:
                return jobs
            index = self._local_rom_index(system, cache=local_index_cache)
            local_match = self._match_local_rom(index, item)
            if artwork_only:
                # Artwork-only sync: never copy the ROM file. Attach artwork only to
                # ROMs that already exist on this machine; skip peer ROMs we don't have.
                if local_match is None:
                    return jobs
                art_local_path = local_match
            else:
                if local_match is None:
                    pending_match = (
                        manager.find_pending_rom(system, relative_path, item.get("rom_fingerprint") or item.get("fingerprint"))
                        if hasattr(manager, "find_pending_rom")
                        else None
                    )
                else:
                    pending_match = None
                if local_match is None and pending_match is None:
                    # Not on this machine yet -> download it.
                    jobs.append(manager.enqueue_rom(
                        config,
                        peer,
                        system,
                        relative_path,
                        expected_size=item.get("byte_count") or item.get("file_size"),
                        expected_fingerprint=item.get("rom_fingerprint") or item.get("fingerprint"),
                        entry_type=str(item.get("entry_type") or "file"),
                    ))
                    art_local_path = relative_path
                else:
                    # Already present (thumbprint match) -> skip the ROM, attach artwork
                    # to the existing local ROM so it shows after a gamelist refresh.
                    art_local_path = local_match or relative_path
            if include_artwork or artwork_only:
                gamelist = item.get("gamelist") if isinstance(item.get("gamelist"), dict) else {}
                fields = [field for field in ARTWORK_FIELDS if gamelist.get(field)]
                if not overwrite_artwork and fields:
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
                            overwrite=True, local_rom_path=art_local_path,
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
                overwrite=True,
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
            ))
        else:
            raise ValueError("asset_type must be roms, bios, artwork, or saves")
        return jobs

    def _handle_admin_local_sync(self, payload: dict) -> None:
        if not _local_network.is_local_mode(self.settings):
            self._send_json(409, {"error": "Enable Local Network mode before syncing assets"})
            return
        peer_id = str(payload.get("peer_id") or "").strip()
        asset_type = str(payload.get("asset_type") or "").strip().lower()
        item = payload.get("item") if isinstance(payload.get("item"), dict) else payload
        include_artwork = bool(payload.get("include_artwork", True))
        overwrite_artwork = bool(payload.get("overwrite_artwork", True))
        artwork_only = bool(payload.get("artwork_only", False))
        peer = _local_network.get_paired_peer(self.settings, peer_id)
        manager = _get_download_manager()
        if not peer:
            self._send_json(404, {"error": "paired peer not found"})
            return
        if manager is None:
            self._send_json(503, {"error": "download manager unavailable"})
            return
        config = {"network_mode": "local_network"}
        jobs = self._enqueue_local_asset(
            manager,
            config,
            peer,
            asset_type,
            item,
            default_system=str(payload.get("system") or ""),
            include_artwork=include_artwork,
            overwrite_artwork=overwrite_artwork,
            artwork_only=artwork_only,
        )
        rom_skipped = asset_type == "roms" and not any(job.get("file_type") == "ROM" for job in jobs)
        # Artwork-only against a ROM we don't have here -> nothing to do.
        rom_absent = artwork_only and not jobs
        self._send_json(202, {
            "status": "queued",
            "job": jobs[0] if jobs else None,
            "jobs": jobs,
            "rom_skipped": rom_skipped,
            "rom_absent": rom_absent,
        })

    def _handle_admin_local_sync_bulk(self, payload: dict) -> None:
        """Copy every item of an asset type from a paired peer.

        Pages through the peer's inventory server-side (so it works regardless of
        the UI's current page) and enqueues each transferable item. Optionally
        scoped to a single system and/or a search query. For ROMs, artwork is
        enqueued alongside each ROM when include_artwork is set."""
        if not _local_network.is_local_mode(self.settings):
            self._send_json(409, {"error": "Enable Local Network mode before syncing assets"})
            return
        peer_id = str(payload.get("peer_id") or "").strip()
        asset_type = str(payload.get("asset_type") or "").strip().lower()
        if asset_type not in {"roms", "bios", "artwork", "saves"}:
            self._send_json(400, {"error": "Bulk copy supports roms, bios, artwork, or saves"})
            return
        system = str(payload.get("system") or "").strip()
        systems = [
            str(value).strip()
            for value in (payload.get("systems") or [])
            if str(value).strip()
        ]
        query = str(payload.get("q") or "").strip()
        include_artwork = bool(payload.get("include_artwork", True))
        overwrite_artwork = bool(payload.get("overwrite_artwork", True))
        artwork_only = bool(payload.get("artwork_only", False))
        peer = _local_network.get_paired_peer(self.settings, peer_id)
        manager = _get_download_manager()
        if not peer:
            self._send_json(404, {"error": "paired peer not found"})
            return
        if manager is None:
            self._send_json(503, {"error": "download manager unavailable"})
            return
        config = {"network_mode": "local_network"}
        page_size = 500
        queued_assets = 0
        queued_artwork = 0
        skipped_existing = 0
        total = 0
        local_index_cache: dict = {}
        local_artwork_cache: dict = {}

        def walk_scope(scope_system: str) -> None:
            """Page through one scope (a single system, or the whole library when
            scope_system is empty) and enqueue each item."""
            nonlocal queued_assets, queued_artwork, skipped_existing, total
            offset = 0
            scope_total = None
            while True:
                params = [f"limit={page_size}", f"offset={offset}"]
                if scope_system:
                    params.append(f"system={quote(scope_system, safe='')}")
                if query:
                    params.append(f"q={quote(query, safe='')}")
                inventory = self._fetch_peer_inventory(peer, peer_id, asset_type, params)
                items = inventory.get("items") or []
                if scope_total is None:
                    scope_total = inventory.get("total")
                    if isinstance(scope_total, int):
                        total += scope_total
                if not items:
                    break
                for entry in items:
                    if not isinstance(entry, dict):
                        continue
                    jobs = self._enqueue_local_asset(
                        manager,
                        config,
                        peer,
                        asset_type,
                        entry,
                        default_system=scope_system or system,
                        include_artwork=include_artwork,
                        overwrite_artwork=overwrite_artwork,
                        artwork_only=artwork_only,
                        local_index_cache=local_index_cache,
                        local_artwork_cache=local_artwork_cache,
                    )
                    asset_jobs = [job for job in jobs if job.get("file_type") != "ARTWORK"]
                    queued_assets += len(asset_jobs)
                    queued_artwork += len(jobs) - len(asset_jobs)
                    if asset_type == "roms" and not artwork_only and not asset_jobs:
                        skipped_existing += 1
                offset += len(items)
                if scope_total is not None and offset >= int(scope_total):
                    break
                if len(items) < page_size:
                    break

        # For ROMs, walk one system at a time so each peer inventory fetch stays
        # small (the peer scans only that system) instead of rescanning its whole
        # library for every page -- which is slow and times out on large libraries.
        # An "all systems" request is expanded via the cheap summary endpoint.
        if asset_type == "roms":
            scope_systems = systems or ([system] if system else [])
            if not scope_systems:
                try:
                    summary = self._fetch_peer_inventory(peer, peer_id, "summary", [])
                    scope_systems = [str(name) for name in (summary.get("systems") or []) if str(name).strip()]
                except Exception:
                    scope_systems = []
            if scope_systems:
                for scope_system in scope_systems:
                    walk_scope(scope_system)
            else:
                walk_scope("")
        else:
            # bios/saves/artwork inventories aren't per-system scanned the same way.
            walk_scope(system)
        self._send_json(202, {
            "status": "queued",
            "asset_type": asset_type,
            "system": system or None,
            "systems": systems,
            "queued_assets": queued_assets,
            "queued_artwork": queued_artwork,
            "skipped_existing": skipped_existing,
            "total_available": total,
        })

    def _fetch_peer_inventory(self, peer: dict, peer_id: str, asset_type: str, params: List[str]) -> dict:
        """Fetch a page of a peer's inventory, transparently handling the
        fake-data local peer (used in tests/dev) the same way browsing does."""
        if self.settings.use_fake_data and peer.get("fake_data"):
            query_params: dict = {}
            for raw in params:
                if "=" in raw:
                    key, value = raw.split("=", 1)
                    query_params[unquote(key)] = [unquote(value)]
            return self._collect_peer_inventory(asset_type, query_params)
        address = _peer_address(peer)
        if not address:
            raise ValueError("paired peer has no reachable address")
        suffix = f"?{'&'.join(params)}" if params else ""
        return _peer_get_json(
            f"{address}/v1/api/peer/inventory/{quote(asset_type, safe='')}{suffix}",
            self.settings,
            peer_id=peer_id,
            config={"network_mode": "local_network"},
            timeout=PEER_INVENTORY_TIMEOUT_SECONDS,
        )
