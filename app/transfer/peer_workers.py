"""Peer-connectivity runtime: background worker threads.

Extracted from ``drone_api.py``. ``_start_local_network_workers`` runs the
tailnet-enrollment, LAN discovery/pairing, and paired-peer health-check workers.
Pure runtime over ``peer_connectivity`` -- no drone_api dependencies.
"""

import os
import time
from datetime import datetime, timezone
from threading import Thread

try:
    from ..common.settings import Settings
    from ..device.tailnet_service import ensure_tailnet_networking, tailnet_status
    from . import local_network as _local_network
    from .drone_tls import DroneCertificateManager
    from .peer_connectivity import _peer_get_json_for_peer, _preferred_peer_address
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from device.tailnet_service import ensure_tailnet_networking, tailnet_status  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from transfer.drone_tls import DroneCertificateManager  # type: ignore
    from transfer.peer_connectivity import _peer_get_json_for_peer, _preferred_peer_address  # type: ignore


def _start_local_network_workers(settings: Settings) -> None:
    Thread(target=ensure_tailnet_networking, name="drone-tailnet-networking", daemon=True).start()

    def tailnet_watchdog() -> None:
        interval = max(30, int(os.environ.get("DRONE_TAILNET_WATCHDOG_INTERVAL_SECONDS", "60")))
        while True:
            time.sleep(interval)
            if not tailnet_status().get("running"):
                ensure_tailnet_networking()

    Thread(target=tailnet_watchdog, name="drone-tailnet-watchdog", daemon=True).start()

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
                address = _preferred_peer_address(peer, settings=settings, peer_id=peer_id)
                checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                result = {
                    "source_drone_id": settings.device_id,
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
