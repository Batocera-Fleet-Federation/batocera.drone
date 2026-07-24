"""RomRequestHandler network + local-network admin handlers, as a mixin.

Extracted from ``drone_api.py``. Network-mode get/update, and the local-network
(LAN + tailnet P2P) endpoints: status/discover, pairing-code rotate, peer
pair/forget/assets, and local sync (single + bulk). Composed onto ``RomRequestHandler``.
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote, unquote

try:
    from ..device.device_control import _ensure_rom_write_access
    from ..device.tailnet_service import tailnet_enroll, tailnet_rotate_auth_key, tailnet_status
    from ..roms.gamelist import ARTWORK_FIELDS, _normalize_gamelist_rom_path
    from ..storage.rom_metadata_store import match_rom_cache_page
    from ..transfer import local_network as _local_network
    from ..transfer.drone_network import _network_mode
    from ..transfer.drone_tls import DroneCertificateManager
    from ..transfer.peer_connectivity import (
        _fetch_peer_info,
        _local_pair_peer,
        _local_peer_cert_cache_path,
        _normalize_peer_address,
        _peer_address,
        _peer_get_json,
        _peer_get_json_for_peer,
        _public_local_peer,
    )
    from .server_tls import load_peer_cert_everywhere
except ImportError:  # pragma: no cover - direct script execution fallback
    from device.device_control import _ensure_rom_write_access  # type: ignore
    from device.tailnet_service import tailnet_enroll, tailnet_rotate_auth_key, tailnet_status  # type: ignore
    from roms.gamelist import ARTWORK_FIELDS, _normalize_gamelist_rom_path  # type: ignore
    from storage.rom_metadata_store import match_rom_cache_page  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from transfer.drone_network import _network_mode  # type: ignore
    from transfer.drone_tls import DroneCertificateManager  # type: ignore
    from transfer.peer_connectivity import (  # type: ignore
        _fetch_peer_info,
        _local_pair_peer,
        _local_peer_cert_cache_path,
        _normalize_peer_address,
        _peer_address,
        _peer_get_json,
        _peer_get_json_for_peer,
        _public_local_peer,
    )
    from web.server_tls import load_peer_cert_everywhere  # type: ignore

# Local copy (drone_api keeps its own; same env). Not test-patched.
PEER_INVENTORY_TIMEOUT_SECONDS = float(os.environ.get("DRONE_PEER_INVENTORY_TIMEOUT_SECONDS", "120"))

# Per-peer budget for the Swarm-overview fan-out. Deliberately short: an
# offline drone should read as "Offline" quickly, not stall the whole page for
# the full inventory timeout.
SWARM_PEER_TIMEOUT_SECONDS = float(os.environ.get("DRONE_SWARM_PEER_TIMEOUT_SECONDS", "4"))


def _get_download_manager():
    """Delegate to the drone_api singleton accessor (lazy to avoid a cycle)."""
    try:
        from ..drone_api import _get_download_manager as _impl
    except ImportError:  # pragma: no cover - flat execution
        from drone_api import _get_download_manager as _impl  # type: ignore
    return _impl()


class HandlersNetworkMixin:
    def _handle_admin_network_mode(self) -> None:
        mode = _network_mode(self.settings)
        integrations = _local_network.get_integrations(self.settings)
        self._send_json(
            200,
            {
                "mode": mode,
                "local_network_active": integrations["local_network_enabled"],
                "local_network_enabled": integrations["local_network_enabled"],
                "modes": [_local_network.MODE_LOCAL_NETWORK],
            },
        )

    def _handle_admin_network_mode_update(self, payload: dict) -> None:
        current = _local_network.get_integrations(self.settings)
        if "local_network_enabled" in payload:
            _local_network.set_integrations(
                self.settings,
                local_network_enabled=bool(payload.get("local_network_enabled", current["local_network_enabled"])),
            )
        else:
            _local_network.set_mode(self.settings, str(payload.get("mode") or ""))
        # Local networking is always on, so this always applies (raises above,
        # before reaching here, if the request tried to disable it). Note:
        # verify_mode itself is fixed per-listener at construction time (see
        # _apply_server_tls's peer_mtls flag) and must never be reassigned
        # here -- doing so would silently undo the browser-facing listeners'
        # CERT_NONE policy (and the mobile client-cert-prompt fix it exists
        # for) the next time these settings are saved.
        for peer in _local_network.paired_peers(self.settings):
            cert_path = Path(str(peer.get("certificate_path") or ""))
            if cert_path.exists():
                load_peer_cert_everywhere(self.server, cert_path)
        _local_network.announce(self.settings, str(DroneCertificateManager(self.settings).metadata().get("fingerprint") or ""))
        self._handle_admin_network_mode()

    def _activate_local_peer_certificate(self, peer: dict) -> None:
        raw_path = str(peer.get("certificate_path") or "").strip()
        if not raw_path:
            return
        cert_path = Path(raw_path)
        if not cert_path.is_file():
            return
        load_peer_cert_everywhere(self.server, cert_path)

    @staticmethod
    def _tailnet_device_row(device: dict) -> dict:
        tailnet_ip = str(device.get("tailnet_ip") or "").strip()
        return {
            "drone_id": f"tailnet:{device.get('tailnet_id') or tailnet_ip}",
            "tailnet_id": str(device.get("tailnet_id") or tailnet_ip),
            "name": str(device.get("name") or device.get("hostname") or tailnet_ip or "Tailnet device"),
            "hostname": str(device.get("hostname") or ""),
            "reachable_url": _normalize_peer_address(tailnet_ip) if tailnet_ip else "",
            "tailnet_ip": tailnet_ip,
            "source": "Tailnet",
            "last_seen": str(device.get("last_seen") or ""),
            "paired": False,
            "tailnet_device": True,
            "tailnet_online": True,
        }

    @staticmethod
    def _probe_tailnet_device(device: dict) -> tuple[dict, Optional[dict], str]:
        tailnet_ip = str(device.get("tailnet_ip") or "").strip()
        try:
            return device, _fetch_peer_info(tailnet_ip, timeout=3.0), ""
        except Exception as error:
            return device, None, str(error) or error.__class__.__name__

    def _sync_tailnet_device(
        self,
        device: dict,
        *,
        info: Optional[dict] = None,
        probe_error: str = "",
        restore_peer_id: str = "",
    ) -> Optional[dict]:
        row = self._tailnet_device_row(device)
        tailnet_ip = str(row.get("tailnet_ip") or "")
        if info is None and not probe_error:
            _, info, probe_error = self._probe_tailnet_device(device)
        if info is None:
            return {**row, "tailnet_probe_error": probe_error}
        peer_id = str(info.get("drone_id") or "").strip()
        if not peer_id or peer_id == str(self.settings.device_id):
            return None
        tailnet_peer = {
            **info,
            "drone_id": peer_id,
            "tailnet_id": str(row.get("tailnet_id") or ""),
            "name": str(info.get("name") or row.get("name") or peer_id),
            "hostname": str(info.get("hostname") or row.get("hostname") or ""),
            "reachable_url": _normalize_peer_address(tailnet_ip),
            "advertised_reachable_url": str(info.get("reachable_url") or ""),
            "tailnet_ip": tailnet_ip,
            "source": "Tailnet",
            "pairing_source": "tailnet",
            "tailnet_device": False,
        }
        local_match = next(
            (
                peer for peer in _local_network.discovered_peers(self.settings, include_stale=True)
                if str(peer.get("drone_id") or "") == peer_id
                and str(peer.get("source") or "Local Network") == "Local Network"
            ),
            None,
        )
        discovered = local_match or _local_network.record_discovered_peer(self.settings, tailnet_peer, tailnet_ip)
        existing = _local_network.get_paired_peer(self.settings, peer_id)
        if existing:
            # A LAN announce may have arrived while the peer's tailscaled was
            # temporarily down and cleared its tailnet_ip. A successful probe
            # over the authenticated tailnet is authoritative reachability
            # evidence, so restore the route without replacing the preferred
            # LAN URL or requiring the already-paired Drone to pair again.
            expected_fingerprint = str(existing.get("certificate_fingerprint") or "").strip().lower()
            observed_fingerprint = str(info.get("certificate_fingerprint") or "").strip().lower()
            if expected_fingerprint and observed_fingerprint and expected_fingerprint != observed_fingerprint:
                return _public_local_peer(
                    {
                        **existing,
                        "tailnet_identity_error": "Tailnet peer certificate fingerprint does not match paired Drone",
                    }
                )
            restored = _local_network.save_paired_peer(
                self.settings,
                {
                    **existing,
                    "tailnet_id": str(row.get("tailnet_id") or existing.get("tailnet_id") or ""),
                    "tailnet_ip": tailnet_ip,
                },
            )
            return _public_local_peer(restored)
        if _local_network.is_tailnet_peer_forgotten(self.settings, peer_id) and restore_peer_id != peer_id:
            return _public_local_peer({**(discovered or tailnet_peer), "tailnet_forgotten": True, "paired": False})
        try:
            paired = _local_pair_peer(
                self.settings,
                tailnet_peer,
                "",
                tailnet_auto_pair=True,
            )
            self._activate_local_peer_certificate(paired)
            return _public_local_peer(paired)
        except Exception as error:
            return _public_local_peer(
                {**(discovered or tailnet_peer), "paired": False, "tailnet_pair_error": str(error) or error.__class__.__name__}
            )

    def _sync_tailnet_peers(self, status: dict) -> list[dict]:
        devices = [device for device in status.get("peers") or [] if isinstance(device, dict)]
        if not status.get("enrolled") or not devices:
            return []
        with ThreadPoolExecutor(max_workers=min(8, len(devices))) as pool:
            probes = list(pool.map(self._probe_tailnet_device, devices))
        rows = []
        for device, info, error in probes:
            row = self._sync_tailnet_device(device, info=info, probe_error=error)
            if row:
                rows.append(row)
        return rows

    def _local_network_status_payload(self, tailnet_devices: Optional[list[dict]] = None) -> dict:
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
        seen_ids = set()
        seen_addresses = set()
        seen_hosts = set()
        active_tailnet_ids = {str(peer.get("drone_id") or "") for peer in tailnet_devices or []}
        active_tailnet_addresses = {str(peer.get("tailnet_ip") or "") for peer in tailnet_devices or []}
        for peer in _local_network.discovered_peers(self.settings, include_stale=True):
            peer_id = str(peer.get("drone_id") or "")
            if hide_seeded_demo and peer_id == "fake-local-peer-01":
                continue
            if peer_id in paired:
                continue
            if tailnet_devices is not None and str(peer.get("source") or "") == "Tailnet":
                tailnet_ip = str(peer.get("tailnet_ip") or "")
                if peer_id not in active_tailnet_ids and tailnet_ip not in active_tailnet_addresses:
                    continue
            public_peer = _public_local_peer({**peer, "source": str(peer.get("source") or "Local Network"), "health": checks.get(peer_id)})
            discovered.append(public_peer)
            seen_ids.add(peer_id)
            seen_addresses.add(str(peer.get("tailnet_ip") or ""))
            seen_hosts.add(str(peer.get("hostname") or "").lower())
        for device in tailnet_devices or []:
            peer_id = str(device.get("drone_id") or "")
            address = str(device.get("tailnet_ip") or "")
            hostname = str(device.get("hostname") or "").lower()
            if device.get("paired") or peer_id in paired:
                continue
            if peer_id in seen_ids or (address and address in seen_addresses) or (hostname and hostname in seen_hosts):
                continue
            discovered.append(_public_local_peer({**device, "source": "Tailnet"}))
            seen_ids.add(peer_id)
            if address:
                seen_addresses.add(address)
            if hostname:
                seen_hosts.add(hostname)
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
        cert_path = Path(str(paired.get("certificate_path") or ""))
        if cert_path.exists():
            load_peer_cert_everywhere(self.server, cert_path)
        self._send_json(200, {"status": "paired", "peer": _public_local_peer(paired)})

    def _handle_admin_local_peer_pair_by_address(self, payload: dict) -> None:
        """Pair with a peer at an operator-entered address (no multicast needed).

        The LAN flow requires the peer to have been multicast-discovered first;
        that can't happen across routed links (a tailnet, another subnet). Here
        the operator supplies the address, we fetch the peer's open bootstrap
        identity from it, then run the exact same pairing handshake.
        """
        if not _local_network.is_local_mode(self.settings):
            self._send_json(409, {"error": "Enable Local Network mode before pairing"})
            return
        address = str(payload.get("address") or "").strip()
        dialed = _normalize_peer_address(address)
        try:
            info = _fetch_peer_info(dialed)
        except ValueError:
            raise
        except Exception as error:
            self._send_json(502, {"error": f"could not reach a Drone at {dialed}: {error}"})
            return
        peer_id = str(info.get("drone_id") or "").strip()
        if not peer_id:
            self._send_json(502, {"error": "peer did not report a drone id"})
            return
        if peer_id == self.settings.device_id:
            self._send_json(409, {"error": "that address answered as this Drone itself"})
            return
        peer = {
            "drone_id": peer_id,
            "name": str(info.get("name") or peer_id),
            "hostname": str(info.get("hostname") or ""),
            "scheme": str(info.get("scheme") or ("http" if dialed.startswith("http://") else "https")),
            "api_port": int(info.get("api_port") or 443),
            "peer_mtls_port": int(info.get("peer_mtls_port") or info.get("api_port") or 443),
            # The dialed address demonstrably routes from here (e.g. over the
            # tailnet); the peer's advertised .local URL may not.
            "reachable_url": dialed,
            "advertised_reachable_url": str(info.get("reachable_url") or ""),
            "tailnet_ip": str(info.get("tailnet_ip") or ""),
            "certificate_fingerprint": str(info.get("certificate_fingerprint") or ""),
        }
        paired = _local_pair_peer(self.settings, peer, str(payload.get("pairing_code") or ""))
        cert_path = Path(str(paired.get("certificate_path") or ""))
        if cert_path.exists():
            load_peer_cert_everywhere(self.server, cert_path)
        self._send_json(200, {"status": "paired", "peer": _public_local_peer(paired)})

    @staticmethod
    def _swarm_peer_ui_url(peer: dict) -> str:
        """Best URL for the *viewer's browser* to open the peer's own UI.

        Prefer the tailnet address (works from any network the viewer's mesh
        client is on); fall back to the peer-advertised URL (works on the
        peer's LAN), then the address this drone dials.
        """
        tailnet_ip = str(peer.get("tailnet_ip") or "").strip()
        if tailnet_ip:
            scheme = str(peer.get("scheme") or "https")
            try:
                port = int(peer.get("api_port") or 443)
            except (TypeError, ValueError):
                port = 443
            suffix = "" if (port == 443 and scheme == "https") else f":{port}"
            return f"{scheme}://{tailnet_ip}{suffix}"
        return str(peer.get("advertised_reachable_url") or peer.get("reachable_url") or "")

    def _swarm_probe_peer(self, peer: dict) -> dict:
        entry = {
            "drone_id": str(peer.get("drone_id") or ""),
            "name": str(peer.get("name") or peer.get("hostname") or peer.get("drone_id") or "Drone"),
            "hostname": str(peer.get("hostname") or ""),
            "is_self": False,
            "online": False,
            "paired": True,
            "reachable_url": str(peer.get("reachable_url") or ""),
            "advertised_reachable_url": str(peer.get("advertised_reachable_url") or ""),
            "tailnet_ip": str(peer.get("tailnet_ip") or ""),
            "ui_url": self._swarm_peer_ui_url(peer),
            "error": None,
            "latency_ms": None,
            "summary": None,
        }
        try:
            started = time.monotonic()
            if self.settings.use_fake_data and peer.get("fake_data"):
                summary = self._collect_peer_inventory("summary", {})
            else:
                summary, address = _peer_get_json_for_peer(
                    peer,
                    "/v1/api/peer/inventory/summary",
                    self.settings,
                    peer_id=entry["drone_id"],
                    config={"network_mode": "local_network"},
                    timeout=SWARM_PEER_TIMEOUT_SECONDS,
                    # An overall cap across *every* candidate address, not just
                    # each individual attempt -- without this, an offline peer
                    # with several candidates (tailnet + LAN + advertised URL)
                    # takes multiples of SWARM_PEER_TIMEOUT_SECONDS, not one.
                    overall_deadline=started + SWARM_PEER_TIMEOUT_SECONDS,
                )
                entry["reachable_url"] = address
            entry["latency_ms"] = int((time.monotonic() - started) * 1000)
            entry["summary"] = {key: summary.get(key) for key in ("systems", "system_counts", "counts", "updated_at")}
            entry["online"] = True
        except Exception as error:
            entry["error"] = str(error) or error.__class__.__name__
        return entry

    def _handle_admin_tailnet_status(self) -> None:
        self._send_json(200, tailnet_status())

    def _handle_admin_tailnet_discover(self) -> None:
        status = tailnet_status()
        tailnet_devices = self._sync_tailnet_peers(status)
        self._send_json(
            200,
            {
                "tailnet": status,
                "network": self._local_network_status_payload(tailnet_devices),
            },
        )

    def _handle_admin_tailnet_peer_restore(self, peer_id: str) -> None:
        peer_id = unquote(peer_id)
        status = tailnet_status()
        if not status.get("enrolled"):
            self._send_json(409, {"error": "Tailnet is not connected"})
            return
        for device in status.get("peers") or []:
            row = self._sync_tailnet_device(device, restore_peer_id=peer_id)
            if row and str(row.get("drone_id") or "") == peer_id and row.get("paired"):
                self._send_json(200, {"status": "paired", "peer": row})
                return
        self._send_json(404, {"error": "Drone is not an online Tailnet peer"})

    def _handle_admin_tailnet_enroll(self, payload: dict) -> None:
        """Enroll this drone in the tailnet with an auth key pasted in the UI.

        The key is a secret: it goes straight to the tailscale CLI and is never
        logged or included in any response/error text.
        """
        try:
            status = tailnet_enroll(str(payload.get("auth_key") or ""), self.settings)
        except ValueError:
            raise
        except RuntimeError as error:
            self._send_json(502, {"error": str(error)})
            return
        self._send_json(200, {"status": "enrolled" if status.get("enrolled") else "pending", **status})

    def _handle_admin_tailnet_rotate_auth_key(self, payload: dict) -> None:
        """Re-enroll this Drone with a replacement key without retaining it."""
        try:
            status = tailnet_rotate_auth_key(str(payload.get("auth_key") or ""), self.settings)
        except ValueError:
            raise
        except RuntimeError as error:
            self._send_json(502, {"error": str(error)})
            return
        self._send_json(200, {"status": "enrolled" if status.get("enrolled") else "pending", **status})

    def _handle_admin_swarm_overview(self) -> None:
        """One entry per Drone in the federation: this machine plus every paired
        peer, probed in parallel with a short per-peer budget so an offline
        drone degrades to ``online: false`` instead of hanging the page."""
        active = _local_network.is_local_mode(self.settings)
        own = _local_network.discovery_payload(self.settings, "")
        self_summary = self._collect_peer_inventory("summary", {})
        drones = [
            {
                "drone_id": str(self.settings.device_id),
                "name": str(self_summary.get("name") or own.get("name") or ""),
                "hostname": str(own.get("hostname") or ""),
                "is_self": True,
                "online": True,
                "paired": True,
                "reachable_url": str(own.get("reachable_url") or ""),
                "advertised_reachable_url": str(own.get("reachable_url") or ""),
                "tailnet_ip": str(own.get("tailnet_ip") or ""),
                "ui_url": "",
                "error": None,
                "latency_ms": 0,
                "summary": {key: self_summary.get(key) for key in ("systems", "system_counts", "counts", "updated_at")},
            }
        ]
        peers = _local_network.paired_peers(self.settings) if active else []
        if peers:
            with ThreadPoolExecutor(max_workers=min(8, len(peers))) as pool:
                drones.extend(pool.map(self._swarm_probe_peer, peers))
        self._send_json(
            200,
            {
                "active": active,
                "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "drones": drones,
            },
        )

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
            suffix = f"?{'&'.join(params)}" if params else ""
            result, _ = _peer_get_json_for_peer(
                peer,
                f"/v1/api/peer/inventory/{quote(asset_type, safe='')}{suffix}",
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
        matched_indexes = match_rom_cache_page(self.settings, items)
        if matched_indexes is not None:
            for index, item in enumerate(items):
                if isinstance(item, dict):
                    item["exists_locally"] = index in matched_indexes
            return
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
        include_roms: bool = True,
        overwrite_files: Optional[bool] = None,
        overwrite_artwork: Optional[bool] = None,
        artwork_only: bool = False,
        local_index_cache: Optional[dict] = None,
        local_artwork_cache: Optional[dict] = None,
    ) -> List[dict]:
        """Enqueue a single peer asset (and, for ROMs, its artwork when present).

        ROMs already present on this machine (matched by content fingerprint, or
        path when no fingerprint is available) are skipped unless `overwrite_files`
        is enabled; their artwork can still be copied and linked into the local
        gamelist. With `include_roms` disabled, artwork is attached only to ROMs
        already on this machine. `overwrite_artwork` and `artwork_only` remain as
        compatibility aliases for older Drone clients.
        `local_index_cache`/`local_artwork_cache` (system -> index) let the bulk path
        reuse the local ROM and artwork lookups across many items."""
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
        artwork_only = bool(payload.get("artwork_only", False))
        include_roms = bool(payload.get("include_roms", not artwork_only))
        overwrite_files = bool(payload.get("overwrite_files")) if "overwrite_files" in payload else None
        overwrite_artwork = bool(payload.get("overwrite_artwork", True))
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
            include_roms=include_roms,
            overwrite_files=overwrite_files,
            overwrite_artwork=overwrite_artwork,
            artwork_only=artwork_only,
        )
        rom_skipped = asset_type == "roms" and not any(job.get("file_type") == "ROM" for job in jobs)
        rom_absent = False
        if asset_type == "roms" and not include_roms:
            system = str(item.get("system") or payload.get("system") or "").strip()
            rom_absent = self._match_local_rom(self._local_rom_index(system), item) is None
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
        artwork_only = bool(payload.get("artwork_only", False))
        include_roms = bool(payload.get("include_roms", not artwork_only))
        overwrite_files = bool(payload.get("overwrite_files")) if "overwrite_files" in payload else None
        overwrite_artwork = bool(payload.get("overwrite_artwork", True))
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
                        include_roms=include_roms,
                        overwrite_files=overwrite_files,
                        overwrite_artwork=overwrite_artwork,
                        artwork_only=artwork_only,
                        local_index_cache=local_index_cache,
                        local_artwork_cache=local_artwork_cache,
                    )
                    asset_jobs = [job for job in jobs if job.get("file_type") != "ARTWORK"]
                    queued_assets += len(asset_jobs)
                    queued_artwork += len(jobs) - len(asset_jobs)
                    if asset_type == "roms" and include_roms and not asset_jobs:
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
        suffix = f"?{'&'.join(params)}" if params else ""
        result, _ = _peer_get_json_for_peer(
            peer,
            f"/v1/api/peer/inventory/{quote(asset_type, safe='')}{suffix}",
            self.settings,
            peer_id=peer_id,
            config={"network_mode": "local_network"},
            timeout=PEER_INVENTORY_TIMEOUT_SECONDS,
        )
        return result
