"""RomRequestHandler peer-serving handlers (the mTLS /peer/* endpoints), as a mixin.

Extracted from ``drone_api.py``. Serves the mTLS-gated ``GET /peer/{roms,bios,saves,artwork}``
downloads + manifests + inventory, plus peer pairing/health. Composed onto
``RomRequestHandler``; methods stay ``self``-bound (they use the handler's send/stream
helpers + ``self.repository``/``self.settings``). See the ``drone-p2p-transfer-security`` skill.
"""

import hashlib
import json
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

try:
    from ..common.auth import record_unauthorized_response
    from ..device.tailnet_service import tailnet_peer_ips
    from ..device.tailnet_service import export_tailnet_payload as _tailnet_export_payload
    from ..device.game_activity import (
        load_gameplay_history as _load_gameplay_history,
        load_gameplay_history_page as _load_gameplay_history_page,
    )
    from ..device.emulator_configs import (
        list_emulator_config_files as _list_emulator_config_files,
        read_emulator_config_file as _read_emulator_config_file,
    )
    from ..device import config_backup as _config_backup
    from ..device import notifications as _notifications
    from ..device import nfs_export_manager as _nfs_exports
    from ..device import smtp_manager as _smtp
    from ..device import vpn_manager as _vpn
    from ..roms.rom_metadata_state import _rom_metadata_cache_status
    from ..storage import config_backup_store as _config_backup_store
    from ..storage import movies_store as _movies_store
    from ..storage import saves_store as _saves_store
    from ..storage.rom_metadata_store import list_artwork_cache_page
    from ..transfer import local_network as _local_network
    from ..transfer.drone_network import (
        _drone_advertised_api_port,
        _drone_advertised_peer_mtls_port,
        _network_mode,
    )
    from ..transfer.drone_tls import DroneCertificateManager
    from ..transfer.network_identity import drone_scheme as _drone_scheme
    from ..transfer.peer_connectivity import _public_local_peer, _save_local_peer_certificate
    from ..transfer.transfer_files import build_folder_manifest as _build_folder_manifest
    from ..transfer.upload_tracker import get_upload_tracker as _get_upload_tracker
    from .server_tls import load_peer_cert_everywhere
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.auth import record_unauthorized_response  # type: ignore
    from device.tailnet_service import tailnet_peer_ips  # type: ignore
    from device.tailnet_service import export_tailnet_payload as _tailnet_export_payload  # type: ignore
    from device.game_activity import (  # type: ignore
        load_gameplay_history as _load_gameplay_history,
        load_gameplay_history_page as _load_gameplay_history_page,
    )
    from device.emulator_configs import (  # type: ignore
        list_emulator_config_files as _list_emulator_config_files,
        read_emulator_config_file as _read_emulator_config_file,
    )
    from device import config_backup as _config_backup  # type: ignore
    from device import notifications as _notifications  # type: ignore
    from device import nfs_export_manager as _nfs_exports  # type: ignore
    from device import smtp_manager as _smtp  # type: ignore
    from device import vpn_manager as _vpn  # type: ignore
    from roms.rom_metadata_state import _rom_metadata_cache_status  # type: ignore
    from storage import config_backup_store as _config_backup_store  # type: ignore
    from storage import movies_store as _movies_store  # type: ignore
    from storage import saves_store as _saves_store  # type: ignore
    from storage.rom_metadata_store import list_artwork_cache_page  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from transfer.drone_network import (  # type: ignore
        _drone_advertised_api_port,
        _drone_advertised_peer_mtls_port,
        _network_mode,
    )
    from transfer.drone_tls import DroneCertificateManager  # type: ignore
    from transfer.network_identity import drone_scheme as _drone_scheme  # type: ignore
    from transfer.peer_connectivity import _public_local_peer, _save_local_peer_certificate  # type: ignore
    from transfer.transfer_files import build_folder_manifest as _build_folder_manifest  # type: ignore
    from transfer.upload_tracker import get_upload_tracker as _get_upload_tracker  # type: ignore
    from web.server_tls import load_peer_cert_everywhere  # type: ignore


class HandlersPeerMixin:
    def _handle_peer_pair(self, payload: dict) -> None:
        if not _local_network.is_local_mode(self.settings):
            self._send_json(409, {"error": "Drone is not in local network mode"})
            return
        client_ip = self.client_address[0] if self.client_address else "-"
        tailnet_authorized = bool(payload.get("tailnet_auto_pair")) and client_ip in tailnet_peer_ips()
        if not tailnet_authorized and not _local_network.validate_pairing_code(self.settings, str(payload.get("pairing_code") or "")):
            record_unauthorized_response(client_ip)
            self._send_json(403, {"error": "invalid or expired pairing code"})
            return
        peer_id = str(payload.get("drone_id") or "").strip()
        certificate_pem = str(payload.get("certificate_pem") or "")
        if not peer_id or peer_id == self.settings.device_id:
            raise ValueError("invalid peer id")
        cert_path, fingerprint = _save_local_peer_certificate(self.settings, peer_id, certificate_pem)
        expected = str(payload.get("certificate_fingerprint") or "").strip().lower()
        if expected and expected != fingerprint.lower():
            cert_path.unlink(missing_ok=True)
            raise ValueError("peer certificate fingerprint mismatch")
        source_ip = self.client_address[0] if self.client_address else ""
        scheme = str(payload.get("scheme") or ("http" if self.settings.http_only else "https"))
        port = int(payload.get("api_port") or 443)
        peer_mtls_port = int(payload.get("peer_mtls_port") or port)
        advertised_reachable_url = str(payload.get("reachable_url") or "").strip()
        reachable_url = advertised_reachable_url
        if source_ip:
            suffix = "" if scheme == "https" and port == 443 else f":{port}"
            reachable_url = f"{scheme}://{source_ip}{suffix}"
        peer = _local_network.save_paired_peer(
            self.settings,
            {
                "drone_id": peer_id,
                "name": str(payload.get("name") or peer_id),
                "hostname": str(payload.get("hostname") or ""),
                "reachable_url": reachable_url,
                "advertised_reachable_url": advertised_reachable_url,
                "scheme": scheme,
                "api_port": port,
                "peer_mtls_port": peer_mtls_port,
                "tailnet_ip": str(payload.get("tailnet_ip") or ""),
                "pairing_source": "tailnet" if tailnet_authorized else "local_network",
                "certificate_fingerprint": fingerprint,
                "certificate_path": str(cert_path),
                "source_ip": source_ip,
            },
        )
        load_peer_cert_everywhere(self.server, cert_path)
        _local_network.pairing_code(self.settings, rotate=True)
        own_certificate = DroneCertificateManager(self.settings).ensure_certificate()
        own_discovery = _local_network.discovery_payload(
            self.settings,
            str(own_certificate.get("fingerprint") or ""),
        )
        self._send_json(
            200,
            {
                "status": "paired",
                "peer": _public_local_peer(peer),
                "drone_id": self.settings.device_id,
                "name": socket.gethostname(),
                "scheme": _drone_scheme(self.settings),
                "api_port": _drone_advertised_api_port(self.settings),
                "peer_mtls_port": _drone_advertised_peer_mtls_port(self.settings),
                "reachable_url": own_discovery.get("reachable_url"),
                "tailnet_ip": str(own_discovery.get("tailnet_ip") or ""),
                "certificate_pem": str(own_certificate.get("public_certificate") or ""),
                "certificate_fingerprint": str(own_certificate.get("fingerprint") or ""),
            },
        )

    def _handle_peer_info(self) -> None:
        # Unauthenticated by design, like POST /peer/pair: this is the pairing
        # bootstrap (no trust exists yet) and returns exactly what the multicast
        # discovery announce already broadcasts on the LAN -- identity, not
        # secrets. It lets a drone be "discovered" across links multicast can't
        # cross (e.g. a tailnet) by dialing its address directly.
        if not _local_network.is_local_mode(self.settings):
            self._send_json(409, {"error": "Drone is not in local network mode"})
            return
        certificate = DroneCertificateManager(self.settings).ensure_certificate()
        self._send_json(
            200,
            _local_network.discovery_payload(self.settings, str(certificate.get("fingerprint") or "")),
        )

    def _handle_peer_health(self) -> None:
        if not self._peer_request_authorized():
            return
        self._send_json(
            200,
            {
                "status": "ok",
                "drone_id": self.settings.device_id,
                "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "mtls": bool(self.settings.drone_mtls_enabled or _local_network.is_local_mode(self.settings)),
                "network_mode": _network_mode(self.settings),
            },
        )

    def _handle_peer_nfs_authorize(self, payload: dict) -> None:
        """Create or refresh this paired caller's read-only NFSv4 export."""
        if not self._peer_request_authorized():
            return
        peer_id = self._peer_requester_device_id()
        if not peer_id:
            self._send_json(403, {"error": "paired client identity could not be resolved"})
            return
        observed_address = self.client_address[0] if self.client_address else ""
        try:
            result = _nfs_exports.authorize_peer(self.settings, peer_id, observed_address)
        except ValueError as error:
            self._send_json(403, {"error": str(error)})
            return
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            self._send_json(503, {"error": str(error)})
            return
        self._send_json(200, result)

    def _handle_peer_nfs_revoke(self, payload: dict) -> None:
        """Remove the paired caller's NFS authorization, best effort."""
        if not self._peer_request_authorized():
            return
        peer_id = self._peer_requester_device_id()
        if not peer_id:
            self._send_json(403, {"error": "paired client identity could not be resolved"})
            return
        result = _nfs_exports.revoke_peer(self.settings, peer_id)
        status_code = 500 if result.get("status") == "error" else 200
        self._send_json(status_code, result)

    def _handle_peer_inventory(self, asset_type: str, query_params: dict, require_authorization: bool = True) -> None:
        if require_authorization and not self._peer_request_authorized():
            return
        self._send_json(200, self._collect_peer_inventory(asset_type, query_params))

    def _collect_peer_inventory(self, asset_type: str, query_params: dict) -> dict:
        normalized = str(asset_type or "").strip().lower()
        try:
            limit = max(1, min(int((query_params.get("limit") or ["500"])[0]), 2000))
            offset = max(0, int((query_params.get("offset") or ["0"])[0]))
        except (TypeError, ValueError):
            raise ValueError("limit and offset must be integers")
        query = str((query_params.get("q") or [""])[0]).strip().lower()
        genre = str((query_params.get("genre") or [""])[0]).strip()
        system = str((query_params.get("system") or [""])[0]).strip()
        systems = {
            value.strip().lower()
            for value in str((query_params.get("systems") or [""])[0]).split(",")
            if value.strip()
        }
        if normalized == "summary":
            cache_status = _rom_metadata_cache_status(self.settings)
            local_system_names = set(self.repository.list_local_system_names())
            system_rows = [
                row for row in self.repository.list_systems()
                if str(row.get("name") or "") in local_system_names
            ]
            system_counts = {
                str(row.get("name") or ""): int(row.get("rom_count") or 0)
                for row in system_rows
                if str(row.get("name") or "")
            }
            system_names = sorted(local_system_names | set(system_counts.keys()), key=str.lower)
            counts = dict(cache_status.get("counts") or {})
            counts["systems"] = len(system_names)
            counts["roms"] = sum(system_counts.values())
            try:
                genre_facets = self.repository.list_rom_genre_facets(systems=sorted(systems) if systems else None)
            except Exception:
                genre_facets = []
            if not isinstance(genre_facets, list):
                genre_facets = []
            response = {
                "drone_id": self.settings.device_id,
                "name": socket.gethostname(),
                "systems": system_names,
                "system_counts": system_counts,
                "counts": counts,
                "genres": [
                    str(row.get("genre") or "") for row in genre_facets
                    if isinstance(row, dict) and str(row.get("genre") or "").strip()
                ],
                "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            }
            include_bios_paths = str((query_params.get("include_bios_paths") or [""])[0]).strip().lower() in {
                "1", "true", "yes", "on",
            }
            if include_bios_paths:
                bios_paths = []
                offset = 0
                while True:
                    page = self.repository.list_bios_page(limit=5000, offset=offset)
                    if page is None:
                        # Never turn this API optimization into another full
                        # filesystem scan.  The requester can reconcile BIOS
                        # from the network filesystem in its background worker until the cache is
                        # authoritative.
                        bios_paths = None
                        break
                    items = page.get("items") if isinstance(page.get("items"), list) else []
                    for row in items:
                        if not isinstance(row, dict):
                            continue
                        relative_path = str(row.get("relative_path") or row.get("file_path") or row.get("path") or "").strip()
                        if relative_path and self.repository.is_local_bios_path(relative_path):
                            bios_paths.append(relative_path)
                    offset += len(items)
                    if not items or offset >= int(page.get("total") or 0):
                        break
                response["bios_paths"] = bios_paths
                response["bios_paths_available"] = bios_paths is not None
            return response
        selected_systems = [system] if system else sorted(systems)

        def paged_response(page: dict) -> dict:
            return {
                "drone_id": self.settings.device_id,
                "asset_type": normalized,
                "system": system or None,
                "systems": sorted(systems),
                "total": int(page.get("total") or 0),
                "limit": int(page.get("limit") or limit),
                "offset": int(page.get("offset") or 0),
                "items": [
                    {key: value for key, value in row.items() if key != "absolute_path"}
                    for row in (page.get("items") or [])
                    if isinstance(row, dict)
                ],
            }

        # Relational metadata can briefly retain rows from before a network
        # reference was enabled.  Filter the requested system set against the
        # current lexical filesystem ownership before consulting that cache.
        # This prevents a Drone from advertising an upstream peer's ROMs or
        # artwork and creating A -> B -> A transfer loops.
        local_system_names = None
        if normalized in {"roms", "artwork"}:
            local_by_key = {
                name.strip().lower(): name
                for name in self.repository.list_local_system_names()
                if name.strip()
            }
            if system:
                selected_systems = [local_by_key[system.lower()]] if system.lower() in local_by_key else []
            elif systems:
                selected_systems = [
                    local_by_key[key]
                    for key in sorted(systems)
                    if key in local_by_key
                ]
            else:
                selected_systems = sorted(local_by_key.values(), key=str.lower)
            local_system_names = set(selected_systems)
            if not selected_systems:
                return paged_response({"total": 0, "limit": limit, "offset": offset, "items": []})

        # Normal operation reads authoritative relational caches. Filtering,
        # counting, ordering, and pagination remain inside SQLite; the legacy
        # filesystem/list fallbacks below are only for an initial cache build.
        if normalized == "roms":
            page = self.repository.list_rom_assets_page(
                systems=selected_systems,
                query=query,
                genre=genre,
                limit=limit,
                offset=offset,
            )
            if page is not None:
                return paged_response(page)
        elif normalized == "bios":
            page = self.repository.list_bios_page(
                query=query,
                folder_systems=selected_systems,
                limit=limit,
                offset=offset,
            )
            if page is not None:
                return paged_response(page)
        elif normalized == "artwork":
            page = list_artwork_cache_page(
                self.settings,
                systems=selected_systems,
                query=query,
                limit=limit,
                offset=offset,
            )
            if page is not None:
                return paged_response(page)
        elif normalized == "saves":
            if self.settings.use_fake_data:
                _saves_store.sync_saves_cache(self.settings.saves_root)
            return paged_response(
                _saves_store.list_saves_page(
                    self.settings.saves_root,
                    systems=selected_systems,
                    query=query,
                    limit=limit,
                    offset=offset,
                )
            )
        elif normalized == "movies":
            # No system dimension at all -- selected_systems/system are
            # accepted for a uniform request shape but ignored.
            if self.settings.use_fake_data:
                _movies_store.sync_movies_cache(self.settings.movies_root)
            return paged_response(
                _movies_store.list_movies_page(
                    self.settings.movies_root,
                    query=query,
                    limit=limit,
                    offset=offset,
                )
            )
        elif normalized == "config_backups":
            # No system dimension at all -- selected_systems/system are
            # accepted for a uniform request shape but ignored, same as
            # movies. Only "complete" backups are ever downloadable, so
            # list_complete_page() filters to those before paging.
            return paged_response(
                _config_backup_store.list_complete_page(
                    self.settings,
                    query=query,
                    limit=limit,
                    offset=offset,
                )
            )
        elif normalized == "emulator_configs":
            configs = _list_emulator_config_files(
                self.settings,
                max_configs=limit,
                offset=offset,
                query=query,
            )
            page_items = []
            for row in configs.get("configs") or []:
                if not isinstance(row, dict):
                    continue
                enriched = {
                    "name": Path(str(row.get("relative_path") or "")).name,
                    "root_name": row.get("root_name"),
                    "relative_path": row.get("relative_path"),
                    "size": row.get("size"),
                    "modified_at": row.get("modified_at"),
                    "error": row.get("error"),
                    "is_downloadable": False,
                }
                try:
                    detail = _read_emulator_config_file(
                        self.settings,
                        str(row.get("root_name") or ""),
                        str(row.get("relative_path") or ""),
                        max_bytes=65536,
                    )
                    if detail.get("content") is not None:
                        enriched["content"] = detail.get("content")
                        enriched["content_truncated"] = bool(detail.get("truncated"))
                    if detail.get("fingerprint"):
                        enriched["fingerprint"] = detail.get("fingerprint")
                except Exception as error:
                    enriched.setdefault("error", str(error))
                page_items.append(enriched)
            return paged_response(
                {
                    "total": configs.get("total") or 0,
                    "limit": limit,
                    "offset": offset,
                    "items": page_items,
                }
            )
        elif normalized == "gameplay":
            page = _load_gameplay_history_page(
                self.settings,
                query=query,
                limit=limit,
                offset=offset,
            )
            for row in page.get("items") or []:
                if isinstance(row, dict):
                    row["is_downloadable"] = False
            return paged_response(page)
        if normalized == "roms":
            # Scan only the requested systems. Scanning the WHOLE library and then
            # filtering (the old plural-`systems` path) is dramatically slower on a
            # large library and could blow past the requester's peer-fetch timeout,
            # surfacing as a silent "Failed to fetch". An empty target list means
            # "no filter" -> the whole library.
            if system:
                target_systems = list(local_system_names or [])
            elif systems:
                target_systems = sorted(local_system_names or [], key=str.lower)
            else:
                target_systems = sorted(local_system_names or [], key=str.lower)
            per_system_rows = []
            for system_name in target_systems:
                try:
                    _, system_rows = self.repository.list_assets(system_name, "roms")
                except Exception:
                    continue
                # Stamp the system on every row so the requester (and the bulk copy
                # path) always knows where each ROM belongs, even when the SQLite
                # fast path omits it.
                for row in system_rows:
                    if isinstance(row, dict):
                        row["system"] = system_name
                per_system_rows.append(system_rows)
            if len(per_system_rows) <= 1:
                rows = per_system_rows[0] if per_system_rows else []
            else:
                # Round-robin interleave so every requested system is visible from
                # the first page (and downloads in a balanced order) instead of all
                # of one system before the next -- which made multi-system requests
                # look like only one system was returned.
                rows = []
                longest = max(len(system_rows) for system_rows in per_system_rows)
                for index in range(longest):
                    for system_rows in per_system_rows:
                        if index < len(system_rows):
                            rows.append(system_rows[index])
        elif normalized == "bios":
            rows = self.repository.list_bios_entries()
        elif normalized == "artwork":
            rows = self.repository.list_artwork_metadata()
            rows = [
                row for row in rows
                if str(row.get("system") or "") in (local_system_names or set())
            ]
        elif normalized == "saves":
            if self.settings.use_fake_data:
                _saves_store.sync_saves_cache(self.settings.saves_root)
            rows = _saves_store.list_saves(self.settings.saves_root, system=system or None)
        elif normalized == "emulator_configs":
            configs = _list_emulator_config_files(self.settings, max_configs=2000)
            rows = [
                {
                    "name": Path(str(row.get("relative_path") or "")).name,
                    "root_name": row.get("root_name"),
                    "relative_path": row.get("relative_path"),
                    "size": row.get("size"),
                    "modified_at": row.get("modified_at"),
                    "error": row.get("error"),
                    "is_downloadable": False,
                }
                for row in configs.get("configs") or []
                if isinstance(row, dict)
            ]
        elif normalized == "gameplay":
            rows = sorted(
                [dict(row, is_downloadable=False) for row in _load_gameplay_history(self.settings)],
                key=lambda row: str(row.get("played_at") or row.get("started_at") or ""),
                reverse=True,
            )
        else:
            raise ValueError("asset type must be summary, roms, bios, artwork, saves, movies, config_backups, emulator_configs, or gameplay")
        rows = [
            {key: value for key, value in row.items() if key not in {"absolute_path"}}
            for row in rows
            if isinstance(row, dict)
        ]
        if systems:
            rows = [
                row for row in rows
                if str(row.get("system") or row.get("root_name") or "").strip().lower() in systems
            ]
        if query:
            rows = [row for row in rows if query in json.dumps(row, sort_keys=True).lower()]
        total = len(rows)
        page = rows[offset:offset + limit]
        if normalized == "emulator_configs":
            enriched_page = []
            for row in page:
                enriched = dict(row)
                try:
                    detail = _read_emulator_config_file(
                        self.settings,
                        str(row.get("root_name") or ""),
                        str(row.get("relative_path") or ""),
                        max_bytes=65536,
                    )
                    if detail.get("content") is not None:
                        enriched["content"] = detail.get("content")
                        enriched["content_truncated"] = bool(detail.get("truncated"))
                    if detail.get("fingerprint"):
                        enriched["fingerprint"] = detail.get("fingerprint")
                except Exception as error:
                    enriched.setdefault("error", str(error))
                enriched_page.append(enriched)
            page = enriched_page
        return {
            "drone_id": self.settings.device_id,
            "asset_type": normalized,
            "system": system or None,
            "systems": sorted(systems),
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": page,
        }

    def _handle_peer_rom_download(self, system: str, relative_path: str) -> None:
        if not self._peer_request_authorized():
            return
        try:
            system_dir = self.repository.get_local_system_dir(system).resolve()
        except (FileNotFoundError, ValueError):
            self._send_json(404, {"error": "not found"})
            return
        rel = unquote(relative_path or "").replace("\\", "/").lstrip("/")
        if not rel or ".." in Path(rel).parts:
            self._send_json(400, {"error": "invalid rom path"})
            return
        target = (system_dir / rel).resolve()
        if not target.exists() or not target.is_file() or (target != system_dir and system_dir not in target.parents):
            self.log_error("peer rom download failed system=%s rom=%s resolved=%s reason=not_found", system, rel, str(target))
            self._send_json(404, {"error": "not found"})
            return
        self.log_message("peer rom download system=%s rom=%s bytes=%s", system, rel, target.stat().st_size)
        self._stream_file(
            target, "application/octet-stream", as_attachment=True,
            upload_meta={"asset_type": "rom", "system": system, "relative_path": rel},
        )

    def _handle_peer_rom_resolve_by_id(self, system: str, gamelist_id: str) -> None:
        """Resolve a ROM by its gamelist ``<game id>`` to the sender's local path.

        The receiver was told only ``(system, gamelist_id)`` (no path),
        so it asks the source drone to map the id -> ``<path>`` from that drone's own
        gamelist.xml. It then pulls the bytes over the normal path-based ``/peer/roms``
        (or ``/peer/rom-manifest`` for folders) endpoint and places the file at the
        same relative path locally.
        """
        if not self._peer_request_authorized():
            return
        gid = unquote(gamelist_id or "").strip()
        if not gid:
            self._send_json(400, {"error": "invalid gamelist id"})
            return
        try:
            # Do not let the normal resolver follow a Drone-managed NFS/SMB
            # reference and advertise another peer's bytes as local content.
            self.repository.get_local_system_dir(system)
        except (FileNotFoundError, ValueError):
            self._send_json(404, {"error": "not found"})
            return
        try:
            target, relative_path, entry_type, marker_relative_path = self.repository.resolve_rom_file_by_gamelist_id(system, gid)
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        except Exception:
            self.log_error("peer rom resolve-by-id failed system=%s gid=%s reason=not_found", system, gid)
            self._send_json(404, {"error": "not found"})
            return
        response = {
            "system": system,
            "gamelist_id": gid,
            "relative_path": relative_path,
            "entry_type": entry_type,
            "marker_relative_path": marker_relative_path,
        }
        if entry_type == "file":
            try:
                stat = target.stat()
                response["file_size"] = int(stat.st_size)
            except OSError:
                pass
            try:
                response["rom_fingerprint"] = self.repository.build_fingerprint(target)
            except Exception:
                pass
        else:
            try:
                size, _ = self.repository.build_directory_stats(target)
                response["file_size"] = int(size)
            except OSError:
                pass
            # Folder-unit ROMs keep the marker file as the identity: fingerprint the
            # marker so the receiver's present-check matches its own scan. True
            # directory entries (marker == the folder itself) carry no fingerprint.
            marker_target = (self.repository.get_local_system_dir(system).resolve() / marker_relative_path).resolve()
            if marker_relative_path != relative_path and marker_target.is_file():
                try:
                    response["rom_fingerprint"] = self.repository.build_fingerprint(marker_target)
                except Exception:
                    pass
        # Tell the receiver which artwork fields this game has on disk so it can pull
        # them (receiver-driven) right after the ROM instead of guessing every field.
        # Keyed by the gamelist <path> -- the marker for folder-unit ROMs.
        try:
            present = self.repository.list_present_artwork(system)
            response["artwork_types"] = sorted(present.get(marker_relative_path.lower(), set()))
        except Exception:
            response["artwork_types"] = []
        self.log_message("peer rom resolve-by-id system=%s gid=%s rom=%s type=%s", system, gid, relative_path, entry_type)
        self._send_json(200, response)

    def _handle_peer_rom_manifest(self, system: str, relative_path: str) -> None:
        if not self._peer_request_authorized():
            return
        try:
            system_dir = self.repository.get_local_system_dir(system).resolve()
        except (FileNotFoundError, ValueError):
            self._send_json(404, {"error": "not found"})
            return
        rel = unquote(relative_path or "").replace("\\", "/").lstrip("/")
        if not rel or ".." in Path(rel).parts:
            self._send_json(400, {"error": "invalid rom path"})
            return
        target = (system_dir / rel).resolve()
        if not target.exists() or not target.is_dir() or (target != system_dir and system_dir not in target.parents):
            self.log_error("peer rom manifest failed system=%s rom=%s resolved=%s reason=not_found", system, rel, str(target))
            self._send_json(404, {"error": "not found"})
            return
        self._send_json(200, {"system": system, "relative_path": rel, **_build_folder_manifest(target)})

    def _handle_peer_bios_download(self, relative_path: str) -> None:
        if not self._peer_request_authorized():
            return
        try:
            bios_root = self.repository.get_bios_root().resolve()
        except FileNotFoundError:
            self._send_json(404, {"error": "not found"})
            return
        rel = unquote(relative_path or "").replace("\\", "/").lstrip("/")
        if not rel or ".." in Path(rel).parts:
            self._send_json(400, {"error": "invalid bios path"})
            return
        if not self.repository.is_local_bios_path(rel):
            self._send_json(404, {"error": "not found"})
            return
        target = (bios_root / rel).resolve()
        if not target.exists() or not target.is_file() or (target != bios_root and bios_root not in target.parents):
            self.log_error("peer bios download failed bios=%s resolved=%s reason=not_found", rel, str(target))
            self._send_json(404, {"error": "not found"})
            return
        self.log_message("peer bios download bios=%s bytes=%s", rel, target.stat().st_size)
        self._stream_file(
            target, "application/octet-stream", as_attachment=True,
            upload_meta={"asset_type": "bios", "relative_path": rel},
        )

    def _handle_peer_movie_download(self, relative_path: str) -> None:
        """Serve a single movie file to an authenticated peer (mTLS when enabled).

        Movies have no system/artwork association (unlike ROMs/BIOS/saves),
        so this is a flat lookup under ``movies_root`` -- same shape as BIOS,
        minus any system dimension.
        """
        if not self._peer_request_authorized():
            return
        movies_root = Path(self.settings.movies_root).resolve()
        rel = unquote(relative_path or "").replace("\\", "/").lstrip("/")
        if not rel or ".." in Path(rel).parts:
            self._send_json(400, {"error": "invalid movie path"})
            return
        target = (movies_root / rel).resolve()
        if not target.exists() or not target.is_file() or (target != movies_root and movies_root not in target.parents):
            self.log_error("peer movie download failed movie=%s resolved=%s reason=not_found", rel, str(target))
            self._send_json(404, {"error": "not found"})
            return
        self.log_message("peer movie download movie=%s bytes=%s", rel, target.stat().st_size)
        self._stream_file(
            target, "application/octet-stream", as_attachment=True,
            upload_meta={"asset_type": "movies", "relative_path": rel},
        )

    def _handle_peer_config_backup_download(self, file_name: str) -> None:
        """Serve a config-backup tarball to an authenticated peer (mTLS when
        enabled). Looked up by file_name (the same identity every other flat
        asset type -- BIOS, movies -- uses), not by this drone's local
        integer id, since a peer's request has no way to know that id. Only
        "complete" backups are ever downloadable; a "creating"/"error" row's
        file may not exist yet or may be a half-built temp file."""
        if not self._peer_request_authorized():
            return
        name = unquote(file_name or "").strip()
        if not name or "/" in name or "\\" in name or name.startswith("."):
            self._send_json(400, {"error": "invalid backup file name"})
            return
        row = _config_backup_store.get_by_file_name(self.settings, name)
        if row is None or row.get("status") != "complete":
            self._send_json(404, {"error": "not found"})
            return
        backups_root = _config_backup.backups_directory(self.settings).resolve()
        target = (backups_root / name).resolve()
        if not target.exists() or not target.is_file() or backups_root not in target.parents:
            self.log_error("peer config backup download failed file=%s resolved=%s reason=not_found", name, str(target))
            self._send_json(404, {"error": "not found"})
            return
        self.log_message("peer config backup download file=%s bytes=%s", name, target.stat().st_size)
        self._stream_file(
            target, "application/gzip", as_attachment=True,
            upload_meta={"asset_type": "config_backups", "relative_path": name},
        )

    def _handle_peer_save_download(self, system: str, relative_path: str) -> None:
        """Serve a single game-save file to an authenticated peer (mTLS when enabled)."""
        if not self._peer_request_authorized():
            return
        saves_root = Path(self.settings.saves_root).resolve()
        system_clean = unquote(system or "").replace("\\", "/").strip("/")
        rel = unquote(relative_path or "").replace("\\", "/").lstrip("/")
        if not system_clean or ".." in Path(system_clean).parts or not rel or ".." in Path(rel).parts:
            self._send_json(400, {"error": "invalid save path"})
            return
        target = (saves_root / system_clean / rel).resolve()
        if not target.exists() or not target.is_file() or saves_root not in target.parents:
            self.log_error("peer save download failed system=%s save=%s resolved=%s reason=not_found", system_clean, rel, str(target))
            self._send_json(404, {"error": "not found"})
            return
        self.log_message("peer save download system=%s save=%s bytes=%s", system_clean, rel, target.stat().st_size)
        self._stream_file(
            target, "application/octet-stream", as_attachment=True,
            upload_meta={"asset_type": "saves", "system": system_clean, "relative_path": rel},
        )

    def _handle_peer_artwork_download(self, system: str, artwork_type: str, rom_path: str) -> None:
        if not self._peer_request_authorized():
            return
        try:
            self.repository.get_local_system_dir(system)
        except (FileNotFoundError, ValueError):
            self._send_json(404, {"error": "not found"})
            return
        try:
            target, relative_path, gamelist_ref = self.repository.resolve_artwork_file(system, unquote(rom_path or ""), unquote(artwork_type or ""))
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        except Exception:
            self._send_json(404, {"error": "not found"})
            return
        self.log_message("peer artwork download system=%s type=%s rom=%s artwork=%s bytes=%s", system, artwork_type, rom_path, relative_path, target.stat().st_size)
        self._stream_file(
            target,
            "application/octet-stream",
            as_attachment=True,
            extra_headers={"X-Asset-Relative-Path": relative_path, "X-Gamelist-Reference": gamelist_ref},
            upload_meta={"asset_type": "artwork", "system": system, "relative_path": relative_path},
        )

    def _handle_peer_vpn_config(self) -> None:
        """Serve this drone's VPN config (+ credentials, if saved) to a paired peer.

        Gated by _peer_request_authorized() (same mTLS pairing check as every
        other /peer/* endpoint above) *plus* vpn_manager's own sharing_enabled
        flag -- VPN credentials are more sensitive than the ROM/BIOS/save/movie/
        artwork bytes served above, so pairing alone is not treated as implicit
        consent here; the source drone's owner must explicitly opt in. See the
        drone-vpn-management skill.
        """
        if not self._peer_request_authorized():
            return
        payload = _vpn.export_payload(self.settings)
        if payload is None:
            self._send_json(404, {"error": "VPN sharing is not enabled on this drone, or no configuration has been uploaded yet"})
            return
        self.log_message("peer vpn config served has_credentials=%s", payload.get("has_credentials"))
        self._send_json(200, payload)

    def _handle_peer_smtp_config(self) -> None:
        """Serve this drone's SMTP settings to a paired peer.

        Mirrors _handle_peer_vpn_config exactly: gated by
        _peer_request_authorized() (same mTLS pairing check as every other
        /peer/* endpoint) plus smtp_manager's own sharing_enabled flag --
        SMTP credentials are sensitive the same way VPN credentials are, so
        pairing alone is not treated as implicit consent here either. See
        the drone-smtp-notifications skill.
        """
        if not self._peer_request_authorized():
            return
        payload = _smtp.export_payload(self.settings)
        if payload is None:
            self._send_json(404, {"error": "SMTP sharing is not enabled on this drone, or no configuration has been set up yet"})
            return
        self.log_message("peer smtp config served host=%s", payload.get("host"))
        self._send_json(200, payload)

    def _handle_peer_smtp_notifications(self, payload: dict) -> None:
        """Ingest audit events from a Drone using this SMTP configuration.

        The client certificate is the source identity; a payload may repeat
        that ID for diagnostics, but it cannot impersonate another paired
        Drone. The SMTP owner performs the idempotent database insert and its
        independent digest worker controls actual mail delivery.
        """
        if not self._peer_request_authorized():
            return
        payload = payload if isinstance(payload, dict) else {}
        certificate_peer_id = self._peer_requester_device_id()
        claimed_peer_id = str(payload.get("source_drone_id") or "").strip()
        if certificate_peer_id and claimed_peer_id and certificate_peer_id != claimed_peer_id:
            self._send_json(403, {"error": "source_drone_id does not match the paired client certificate"})
            return
        source_peer_id = str(certificate_peer_id or claimed_peer_id).strip()
        peer = _local_network.get_paired_peer(self.settings, source_peer_id) if source_peer_id else None
        if not source_peer_id or not peer:
            self._send_json(403, {"error": "notification relay requires a paired source drone"})
            return
        source_peer_name = str(peer.get("name") or peer.get("hostname") or source_peer_id)
        try:
            result = _smtp.ingest_relayed_notifications(
                self.settings,
                payload.get("events"),
                source_drone_id=source_peer_id,
                source_drone_name=source_peer_name,
            )
        except PermissionError as error:
            self._send_json(403, {"error": str(error)})
            return
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        self._send_json(202, result)

    def _handle_peer_smtp_mail(self, payload: dict) -> None:
        """Queue explicit mail jobs relayed by a paired satellite."""
        if not self._peer_request_authorized():
            return
        payload = payload if isinstance(payload, dict) else {}
        certificate_peer_id = self._peer_requester_device_id()
        claimed_peer_id = str(payload.get("source_drone_id") or "").strip()
        if certificate_peer_id and claimed_peer_id and certificate_peer_id != claimed_peer_id:
            self._send_json(403, {"error": "source_drone_id does not match the paired client certificate"})
            return
        source_peer_id = str(certificate_peer_id or claimed_peer_id).strip()
        peer = _local_network.get_paired_peer(self.settings, source_peer_id) if source_peer_id else None
        if not source_peer_id or not peer:
            self._send_json(403, {"error": "mail relay requires a paired source drone"})
            return
        source_peer_name = str(peer.get("name") or peer.get("hostname") or source_peer_id)
        try:
            result = _smtp.ingest_relayed_mail_jobs(
                self.settings,
                payload.get("jobs"),
                source_drone_id=source_peer_id,
                source_drone_name=source_peer_name,
            )
        except PermissionError as error:
            self._send_json(403, {"error": str(error)})
            return
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        self._send_json(202, result)

    def _handle_peer_tailnet_config(self) -> None:
        """Serve this drone's Tailscale auth key to a paired peer.

        Mirrors _handle_peer_vpn_config exactly: gated by
        _peer_request_authorized() (same mTLS pairing check as every other
        /peer/* endpoint) plus tailnet_service's own sharing_enabled flag --
        an auth key is sensitive the same way VPN/SMTP credentials are, so
        pairing alone is not treated as implicit consent here either.
        """
        if not self._peer_request_authorized():
            return
        payload = _tailnet_export_payload(self.settings)
        if payload is None:
            self._send_json(404, {"error": "Tailnet sharing is not enabled on this drone, or it is not enrolled yet"})
            return
        self.log_message("peer tailnet config served enrolled=%s", payload.get("enrolled"))
        self._send_json(200, payload)

    def _peer_requester_device_id(self) -> Optional[str]:
        """Best-effort identity of the peer on the other end of this mTLS
        connection, for upload-activity display only -- the authorization
        decision has already been made by _peer_request_authorized. Returns
        None when it can't be determined (e.g. local HTTP-only mode)."""
        try:
            der = self.connection.getpeercert(binary_form=True) if hasattr(self.connection, "getpeercert") else None
        except Exception:
            der = None
        if not der:
            return None
        fingerprint = hashlib.sha256(der).hexdigest().lower()
        for peer in _local_network.paired_peers(self.settings):
            if str(peer.get("certificate_fingerprint") or "").strip().lower() == fingerprint:
                return str(peer.get("drone_id") or "") or None
        return None

    def _stream_file(
        self,
        path: Path,
        content_type: str,
        as_attachment: bool = False,
        extra_headers: Optional[dict] = None,
        upload_meta: Optional[dict] = None,
        csp_override: Optional[str] = None,
    ) -> None:
        file_size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(file_size))
        self._send_security_headers(csp_override=csp_override)
        if as_attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        for key, value in (extra_headers or {}).items():
            self.send_header(str(key), str(value))
        self.end_headers()

        tracker = _get_upload_tracker()
        upload_id = None
        if upload_meta is not None:
            upload_id = tracker.start(
                peer_device_id=self._peer_requester_device_id(),
                asset_type=str(upload_meta.get("asset_type") or "rom"),
                relative_path=str(upload_meta.get("relative_path") or path.name),
                system=upload_meta.get("system"),
                transport="direct",
                total_bytes=file_size,
            )
        sent = 0
        try:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
                    if upload_id:
                        tracker.progress(upload_id, sent)
            if upload_id:
                tracker.finish(upload_id, "completed")
                _notifications.record_event(
                    self.settings,
                    "asset_uploaded",
                    "Asset uploaded",
                    str((upload_meta or {}).get("relative_path") or path.name),
                )
        except Exception as error:
            if upload_id:
                tracker.finish(upload_id, "failed", error=str(error))
            raise

    def _stream_cached_image(self, path: Path) -> None:
        key = str(path)

        if self.image_miss_cache.has(key):
            raise FileNotFoundError()

        cached = self.image_cache.get(key)
        current_mtime = path.stat().st_mtime if path.exists() else None
        if cached and cached["meta"].get("mtime") == current_mtime:
            data = cached["data"]
            content_type = cached["meta"]["content_type"]
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self._send_security_headers(cache_control="public, max-age=3600")
            self.end_headers()
            self.wfile.write(data)
            return

        if not path.exists():
            self.image_miss_cache.put(key)
            raise FileNotFoundError()

        if not path.is_file():
            raise ValueError("not a file")

        data = path.read_bytes()
        content_type = self._guess_content_type(path)
        self.image_cache.put(key, data, meta={"content_type": content_type, "mtime": path.stat().st_mtime})

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._send_security_headers(cache_control="public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)
