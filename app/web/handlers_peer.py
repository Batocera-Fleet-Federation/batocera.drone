"""RomRequestHandler peer-serving handlers (the mTLS /peer/* endpoints), as a mixin.

Extracted from ``drone_api.py``. Serves the mTLS-gated ``GET /peer/{roms,bios,saves,artwork}``
downloads + manifests + inventory, plus peer pairing/health. Composed onto
``RomRequestHandler``; methods stay ``self``-bound (they use the handler's send/stream
helpers + ``self.repository``/``self.settings``). See the ``drone-p2p-transfer-security`` skill.
"""

import json
import socket
import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

try:
    from ..common.auth import record_unauthorized_response
    from ..overmind.overmind_game_logs import load_gameplay_history as _load_gameplay_history
    from ..overmind.overmind_reporting import (
        list_emulator_config_files as _list_emulator_config_files,
        read_emulator_config_file as _read_emulator_config_file,
    )
    from ..roms.rom_metadata_state import _rom_metadata_cache_status
    from ..storage import saves_store as _saves_store
    from ..transfer import local_network as _local_network
    from ..transfer.drone_network import _drone_advertised_api_port, _network_mode
    from ..transfer.drone_tls import DroneCertificateManager
    from ..transfer.network_identity import drone_scheme as _drone_scheme
    from ..transfer.peer_connectivity import _public_local_peer, _save_local_peer_certificate
    from ..transfer.transfer_files import build_folder_manifest as _build_folder_manifest
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.auth import record_unauthorized_response  # type: ignore
    from overmind.overmind_game_logs import load_gameplay_history as _load_gameplay_history  # type: ignore
    from overmind.overmind_reporting import (  # type: ignore
        list_emulator_config_files as _list_emulator_config_files,
        read_emulator_config_file as _read_emulator_config_file,
    )
    from roms.rom_metadata_state import _rom_metadata_cache_status  # type: ignore
    from storage import saves_store as _saves_store  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from transfer.drone_network import _drone_advertised_api_port, _network_mode  # type: ignore
    from transfer.drone_tls import DroneCertificateManager  # type: ignore
    from transfer.network_identity import drone_scheme as _drone_scheme  # type: ignore
    from transfer.peer_connectivity import _public_local_peer, _save_local_peer_certificate  # type: ignore
    from transfer.transfer_files import build_folder_manifest as _build_folder_manifest  # type: ignore


class HandlersPeerMixin:
    def _handle_peer_pair(self, payload: dict) -> None:
        if not _local_network.is_local_mode(self.settings):
            self._send_json(409, {"error": "Drone is not in local network mode"})
            return
        if not _local_network.validate_pairing_code(self.settings, str(payload.get("pairing_code") or "")):
            client_ip = self.client_address[0] if self.client_address else "-"
            record_unauthorized_response(client_ip)
            self._send_json(403, {"error": "invalid or expired pairing code"})
            return
        peer_id = str(payload.get("drone_id") or "").strip()
        certificate_pem = str(payload.get("certificate_pem") or "")
        if not peer_id or peer_id == self.settings.overmind_device_id:
            raise ValueError("invalid peer id")
        cert_path, fingerprint = _save_local_peer_certificate(self.settings, peer_id, certificate_pem)
        expected = str(payload.get("certificate_fingerprint") or "").strip().lower()
        if expected and expected != fingerprint.lower():
            cert_path.unlink(missing_ok=True)
            raise ValueError("peer certificate fingerprint mismatch")
        source_ip = self.client_address[0] if self.client_address else ""
        scheme = str(payload.get("scheme") or ("http" if self.settings.http_only else "https"))
        port = int(payload.get("api_port") or 443)
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
                "certificate_fingerprint": fingerprint,
                "certificate_path": str(cert_path),
                "source_ip": source_ip,
            },
        )
        ssl_context = getattr(self.server, "ssl_context", None)
        if ssl_context is not None:
            try:
                ssl_context.load_verify_locations(cafile=str(cert_path))
            except ssl.SSLError:
                pass
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
                "drone_id": self.settings.overmind_device_id,
                "name": socket.gethostname(),
                "scheme": _drone_scheme(self.settings),
                "api_port": _drone_advertised_api_port(self.settings),
                "reachable_url": own_discovery.get("reachable_url"),
                "certificate_pem": str(own_certificate.get("public_certificate") or ""),
                "certificate_fingerprint": str(own_certificate.get("fingerprint") or ""),
            },
        )

    def _handle_peer_health(self) -> None:
        if not self._peer_request_authorized():
            return
        self._send_json(
            200,
            {
                "status": "ok",
                "drone_id": self.settings.overmind_device_id,
                "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "mtls": bool(self.settings.drone_mtls_enabled or _local_network.is_local_mode(self.settings)),
                "network_mode": _network_mode(self.settings),
            },
        )

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
        system = str((query_params.get("system") or [""])[0]).strip()
        systems = {
            value.strip().lower()
            for value in str((query_params.get("systems") or [""])[0]).split(",")
            if value.strip()
        }
        if normalized == "summary":
            cache_status = _rom_metadata_cache_status(self.settings)
            system_rows = self.repository.list_systems()
            system_counts = {
                str(row.get("name") or ""): int(row.get("rom_count") or 0)
                for row in system_rows
                if str(row.get("name") or "")
            }
            system_names = sorted(set(self.repository.list_system_names()) | set(system_counts.keys()), key=str.lower)
            return {
                "drone_id": self.settings.overmind_device_id,
                "name": socket.gethostname(),
                "systems": system_names,
                "system_counts": system_counts,
                "counts": cache_status.get("counts") or {},
                "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            }
        if normalized == "roms":
            # Scan only the requested systems. Scanning the WHOLE library and then
            # filtering (the old plural-`systems` path) is dramatically slower on a
            # large library and could blow past the requester's peer-fetch timeout,
            # surfacing as a silent "Failed to fetch". An empty target list means
            # "no filter" -> the whole library.
            if system:
                target_systems = [system]
            elif systems:
                target_systems = [name for name in self.repository.list_system_names() if name.strip().lower() in systems]
            else:
                target_systems = list(self.repository.list_system_names())
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
            if system:
                rows = [row for row in rows if str(row.get("system") or "").lower() == system.lower()]
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
            raise ValueError("asset type must be summary, roms, bios, artwork, saves, emulator_configs, or gameplay")
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
            "drone_id": self.settings.overmind_device_id,
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
        system_dir = self.repository.get_system_dir(system).resolve()
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
        self._stream_file(target, "application/octet-stream", as_attachment=True)

    def _handle_peer_rom_resolve_by_id(self, system: str, gamelist_id: str) -> None:
        """Resolve a ROM by its gamelist ``<game id>`` to the sender's local path.

        The receiver was told only ``(system, gamelist_id)`` by Overmind (no path),
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
            marker_target = (self.repository.get_system_dir(system).resolve() / marker_relative_path).resolve()
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
        system_dir = self.repository.get_system_dir(system).resolve()
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
        target = (bios_root / rel).resolve()
        if not target.exists() or not target.is_file() or (target != bios_root and bios_root not in target.parents):
            self.log_error("peer bios download failed bios=%s resolved=%s reason=not_found", rel, str(target))
            self._send_json(404, {"error": "not found"})
            return
        self.log_message("peer bios download bios=%s bytes=%s", rel, target.stat().st_size)
        self._stream_file(target, "application/octet-stream", as_attachment=True)

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
        self._stream_file(target, "application/octet-stream", as_attachment=True)

    def _handle_peer_artwork_download(self, system: str, artwork_type: str, rom_path: str) -> None:
        if not self._peer_request_authorized():
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
        )

    def _stream_file(self, path: Path, content_type: str, as_attachment: bool = False, extra_headers: Optional[dict] = None) -> None:
        file_size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(file_size))
        self._send_security_headers()
        if as_attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        for key, value in (extra_headers or {}).items():
            self.send_header(str(key), str(value))
        self.end_headers()

        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

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
            self._send_security_headers()
            self.send_header("Cache-Control", "public, max-age=3600")
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
        self._send_security_headers()
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)
