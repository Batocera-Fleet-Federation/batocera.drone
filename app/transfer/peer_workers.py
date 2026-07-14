"""Peer-connectivity runtime: public-IP health probe + background worker threads.

Extracted from ``drone_api.py``. ``_probe_peer_public_ip`` health-checks a peer over its
public endpoint; ``_start_peer_health_check_thread`` periodically probes swarm peers and
reports results; ``_start_local_network_workers`` runs the local-network discovery/pairing
workers. Pure runtime over ``peer_connectivity`` -- no drone_api dependencies.
"""

import os
import ssl
import time
from datetime import datetime, timezone
from threading import Thread
from typing import Optional
from urllib.parse import quote

try:
    from ..common.logging_setup import _overmind_log
    from ..common.settings import Settings
    from ..device.tailnet_service import ensure_tailnet_networking
    from ..overmind.overmind_client import _format_overmind_error, _overmind_post_json
    from ..overmind.overmind_config import (
        _load_overmind_config_for_settings,
        _overmind_peer_results_path_for_settings,
        _overmind_swarm_path_for_settings,
    )
    from ..storage.state_store import database_path as _state_database_path
    from ..storage.state_store import load_payload as _load_state_payload
    from ..storage.state_store import save_payload as _save_state_payload
    from . import local_network as _local_network
    from .drone_tls import DroneCertificateManager
    from .peer_connectivity import (
        _check_peer,
        _peer_address,
        _peer_api_port,
        _peer_get_json,
        _peer_get_json_for_peer,
        _peer_health_url,
    )
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.logging_setup import _overmind_log  # type: ignore
    from common.settings import Settings  # type: ignore
    from device.tailnet_service import ensure_tailnet_networking  # type: ignore
    from overmind.overmind_client import _format_overmind_error, _overmind_post_json  # type: ignore
    from overmind.overmind_config import (  # type: ignore
        _load_overmind_config_for_settings,
        _overmind_peer_results_path_for_settings,
        _overmind_swarm_path_for_settings,
    )
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import load_payload as _load_state_payload  # type: ignore
    from storage.state_store import save_payload as _save_state_payload  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from transfer.drone_tls import DroneCertificateManager  # type: ignore
    from transfer.peer_connectivity import (  # type: ignore
        _check_peer,
        _peer_address,
        _peer_api_port,
        _peer_get_json,
        _peer_get_json_for_peer,
        _peer_health_url,
    )

# Local copy of the health-check interval (drone_api keeps its own for the poller
# bootstrap still resident there); both read the same env var, so values match.
PEER_CHECK_INTERVAL_SECONDS = int(os.environ.get("DRONE_PEER_CHECK_INTERVAL_SECONDS", "300"))


def _probe_peer_public_ip(settings: Settings, peer: dict, config: Optional[dict] = None) -> dict:
    """Health-check a peer using its public endpoint via mTLS https://<public_ip>[:port]/health."""
    peer_id = str(peer.get("drone_id") or peer.get("device_id") or peer.get("id") or "")
    public_ip = str(peer.get("public_ip") or "").strip()
    api_port = _peer_api_port(peer)
    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    result: dict = {
        "source_drone_id": settings.overmind_device_id,
        "target_drone_id": peer_id,
        "target_address": None,
        "public_ip": public_ip or None,
        "api_port": api_port,
        "status": "fail",
        "latency_ms": None,
        "failure_reason": None,
        "checked_at": checked_at,
    }
    if not public_ip:
        result["failure_reason"] = "no public IP available"
        return result
    host = public_ip
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port_suffix = "" if api_port == 443 else f":{api_port}"
    address = f"https://{host}{port_suffix}"
    result["target_address"] = address
    started = time.monotonic()
    try:
        _peer_get_json(_peer_health_url(address), settings, peer_id=peer_id, config=config)
        result["status"] = "pass"
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
    except ssl.SSLError as error:
        message = str(error)
        if config and any(term in message.lower() for term in ("unknown ca", "certificate", "cert")):
            try:
                _peer_get_json(_peer_health_url(address), settings, peer_id=peer_id, config=config, refresh_cert=True)
                result["status"] = "pass"
                result["latency_ms"] = int((time.monotonic() - started) * 1000)
                return result
            except Exception as retry_error:
                result["failure_reason"] = f"{message}; retry after cert refresh: {retry_error}"
                return result
        result["failure_reason"] = message
    except Exception as error:
        result["failure_reason"] = str(error)
    return result


def _start_peer_health_check_thread(settings: Settings) -> None:
    """Start a background thread that periodically health-checks swarm peers via their public IP."""
    interval = max(30, PEER_CHECK_INTERVAL_SECONDS)

    def loop() -> None:
        while True:
            time.sleep(interval)
            if not _local_network.is_overmind_mode(settings):
                continue
            try:
                config = _load_overmind_config_for_settings(settings)
                base_url = str(config.get("overmind_url") or "").strip().rstrip("/")
                token = str(config.get("overmind_token") or "").strip()
                if not base_url or not token:
                    continue
                swarm = _load_state_payload(
                    _state_database_path(settings.userdata_root),
                    "overmind_swarm.json",
                    [],
                    legacy_path=_overmind_swarm_path_for_settings(settings),
                )
                if not swarm:
                    continue
                peer_results = []
                for peer in swarm:
                    peer_id = str(peer.get("drone_id") or peer.get("device_id") or peer.get("id") or "")
                    if not peer_id or peer_id == settings.overmind_device_id:
                        continue
                    if not str(peer.get("public_ip") or "").strip():
                        continue
                    result = _probe_peer_public_ip(settings, peer, config=config)
                    peer_results.append(result)
                    _overmind_log(
                        f"Peer health check: source={settings.overmind_device_id} target={peer_id} "
                        f"status={result['status']} address={result.get('target_address')} "
                        f"latency={result.get('latency_ms')}ms"
                    )
                if peer_results:
                    _save_state_payload(
                        _state_database_path(settings.userdata_root),
                        "peer_checks.json",
                        peer_results,
                    )
                    _overmind_peer_results_path_for_settings(settings).unlink(missing_ok=True)
                    device_id = quote(settings.overmind_device_id, safe="")
                    try:
                        _overmind_post_json(
                            f"{base_url}/api/devices/{device_id}/peer-checks",
                            {"results": peer_results},
                            token=token,
                            settings=settings,
                        )
                        _overmind_log(
                            f"Peer health checks reported to Overmind: {len(peer_results)} result(s)"
                        )
                    except Exception as report_error:
                        _overmind_log(
                            f"Failed to report peer health checks to Overmind: {_format_overmind_error(report_error)}"
                        )
            except Exception as error:
                _overmind_log(f"Peer health check thread error: {_format_overmind_error(error)}")

    thread = Thread(target=loop, name="peer-health-checker", daemon=True)
    thread.start()


def _start_local_network_workers(settings: Settings) -> None:
    Thread(target=ensure_tailnet_networking, name="drone-tailnet-networking", daemon=True).start()

    def fingerprint() -> str:
        return str(DroneCertificateManager(settings).metadata().get("fingerprint") or "")

    _local_network.start_discovery_worker(settings, fingerprint)
    interval = max(10, int(os.environ.get("DRONE_LOCAL_HEALTH_INTERVAL_SECONDS", "30")))

    def health_loop() -> None:
        while True:
            time.sleep(interval)
            if not _local_network.is_local_mode(settings):
                continue
            checks = []
            for peer in _local_network.paired_peers(settings):
                peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
                address = _peer_address(peer)
                checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                result = {
                    "source_drone_id": settings.overmind_device_id,
                    "target_drone_id": peer_id,
                    "target_address": address,
                    "status": "fail",
                    "latency_ms": None,
                    "failure_reason": None,
                    "checked_at": checked_at,
                }
                if not address:
                    result["failure_reason"] = "no peer address available"
                    checks.append(result)
                    continue
                started = time.monotonic()
                try:
                    _, address = _peer_get_json_for_peer(
                        peer,
                        "/v1/api/peer/health",
                        settings,
                        peer_id=peer_id,
                        config={"network_mode": "local_network"},
                    )
                    result["target_address"] = address
                    result["status"] = "pass"
                    result["latency_ms"] = int((time.monotonic() - started) * 1000)
                except Exception as error:
                    result["failure_reason"] = str(error)
                checks.append(result)
            _local_network.save_peer_checks(settings, checks)

    Thread(target=health_loop, name="drone-local-peer-health", daemon=True).start()
