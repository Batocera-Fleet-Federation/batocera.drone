"""Peer-connectivity runtime: background worker threads.

Extracted from ``drone_api.py``. ``_start_local_network_workers`` runs the
tailnet-enrollment, LAN discovery/pairing, and paired-peer health-check workers.
Pure runtime over ``peer_connectivity`` -- no drone_api dependencies.
"""

import os
import sys
import time
from datetime import datetime, timezone
from threading import Event, RLock, Thread
from typing import Optional

try:
    from ..common.settings import Settings
    from ..device.tailnet_service import ensure_tailnet_networking, bootstrap_tailnet_from_swarm, tailnet_status, _repair_tailnet_data_path
    from . import local_network as _local_network
    from .drone_tls import DroneCertificateManager
    from .peer_connectivity import _peer_get_json_for_peer, _preferred_peer_address
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from device.tailnet_service import ensure_tailnet_networking, bootstrap_tailnet_from_swarm, tailnet_status, _repair_tailnet_data_path  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from transfer.drone_tls import DroneCertificateManager  # type: ignore
    from transfer.peer_connectivity import _peer_get_json_for_peer, _preferred_peer_address  # type: ignore


_LOCAL_NETWORK_STOP: Optional[Event] = None
_LOCAL_NETWORK_DISCOVERY: Optional[Thread] = None
# Serializes start/stop so overlapping callers cannot both read the same live
# generation and then overwrite each other's stop event / discovery handle.
# Re-entrant because a start retires the previous generation via stop.
_LOCAL_NETWORK_LIFECYCLE_LOCK = RLock()


def _wait_for_generation_stop(stop: Event, interval: float) -> bool:
    """Wait for a worker generation to stop, returning ``True`` when it has."""
    return stop.wait(interval)


def _sleep_unless_stopped(stop: Event, seconds: float, slice_seconds: float = 1.0) -> bool:
    """Sleep for ``seconds`` in slices, returning ``True`` once ``stop`` is set."""
    remaining = seconds
    while remaining > 0 and not stop.is_set():
        step = min(remaining, slice_seconds)
        time.sleep(step)
        remaining -= step
    return stop.is_set()


def stop_local_network_workers(timeout: float = 2.0) -> None:
    """Signal the current local-network generation to stop.

    Worker loops wait on the generation event, so signalling it retires their
    pending poll waits immediately.  Discovery additionally needs its socket
    closed before it can be joined.
    """
    global _LOCAL_NETWORK_STOP, _LOCAL_NETWORK_DISCOVERY
    with _LOCAL_NETWORK_LIFECYCLE_LOCK:
        if _LOCAL_NETWORK_STOP is not None:
            _LOCAL_NETWORK_STOP.set()
        discovery = _LOCAL_NETWORK_DISCOVERY
        if discovery is not None:
            # Unblock recvfrom so join is not racing the 1s datagram timeout.
            closer = getattr(discovery, "close_discovery_socket", None)
            if callable(closer):
                closer()
            discovery.join(timeout)
        _LOCAL_NETWORK_STOP = None
        _LOCAL_NETWORK_DISCOVERY = None


def local_network_workers_running() -> bool:
    """Return whether a local-network generation is currently live.

    Callers that latch "workers were started" must check this too: a stop
    issued directly on this module clears the live generation, and the latch
    alone would then skip the restart.
    """
    with _LOCAL_NETWORK_LIFECYCLE_LOCK:
        return _LOCAL_NETWORK_STOP is not None and not _LOCAL_NETWORK_STOP.is_set()


def _start_local_network_workers(settings: Settings) -> None:
    with _LOCAL_NETWORK_LIFECYCLE_LOCK:
        _start_local_network_generation(settings)


def _start_local_network_generation(settings: Settings) -> None:
    global _LOCAL_NETWORK_STOP, _LOCAL_NETWORK_DISCOVERY
    # A live generation must be stopped before its stop event and discovery
    # handle are overwritten, otherwise its discovery thread is orphaned.
    stop_local_network_workers()
    stop = Event()
    _LOCAL_NETWORK_STOP = stop
    Thread(target=ensure_tailnet_networking, args=(settings,), name="drone-tailnet-networking", daemon=True).start()

    def tailnet_watchdog() -> None:
        interval = max(30, int(os.environ.get("DRONE_TAILNET_WATCHDOG_INTERVAL_SECONDS", "60")))
        while not _wait_for_generation_stop(stop, interval):
            # One failed pass must not end the watchdog: an unhandled exception
            # would kill this thread for good, so log it and try again next tick.
            try:
                if not tailnet_status().get("running"):
                    ensure_tailnet_networking(settings)
                else:
                    _repair_tailnet_data_path()
            except Exception as error:
                print(
                    f"Tailnet watchdog pass failed: {error.__class__.__name__}: {error}",
                    file=sys.stderr,
                    flush=True,
                )

    Thread(target=tailnet_watchdog, name="drone-tailnet-watchdog", daemon=True).start()

    def tailnet_bootstrap_retry() -> None:
        # The startup bootstrap runs once, and a paired peer that shares its
        # auth key may only come online (or start sharing) later. Keep retrying
        # while this Drone is installed but not enrolled so it joins the tailnet
        # with no operator action. A separate thread keeps peer timeouts from
        # delaying the watchdog's data-path repair.
        interval = max(60, int(os.environ.get("DRONE_TAILNET_BOOTSTRAP_INTERVAL_SECONDS", "300")))
        while not _wait_for_generation_stop(stop, interval):
            try:
                status = tailnet_status()
                if status.get("installed") and not status.get("enrolled"):
                    bootstrap_tailnet_from_swarm(settings)
            except Exception as error:
                print(
                    f"Tailnet bootstrap retry failed: {error.__class__.__name__}: {error}",
                    file=sys.stderr,
                    flush=True,
                )

    Thread(target=tailnet_bootstrap_retry, name="drone-tailnet-bootstrap-retry", daemon=True).start()

    def fingerprint() -> str:
        return str(DroneCertificateManager(settings).metadata().get("fingerprint") or "")

    _LOCAL_NETWORK_DISCOVERY = _local_network.start_discovery_worker(settings, fingerprint, stop_event=stop)
    interval = max(10, int(os.environ.get("DRONE_LOCAL_HEALTH_INTERVAL_SECONDS", "30")))

    def health_loop() -> None:
        # Sleep in short slices so a stopped or restarted generation retires
        # this thread within one slice instead of lingering a full interval.
        while not _sleep_unless_stopped(stop, interval):
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
            try:
                from . import swarm_membership
            except ImportError:  # pragma: no cover - direct script execution fallback
                from transfer import swarm_membership  # type: ignore
            try:
                swarm_membership.propagate_membership(settings)
            except Exception as error:
                print(
                    f"Swarm membership propagation failed: {error.__class__.__name__}: {error}",
                    file=sys.stderr,
                    flush=True,
                )

    Thread(target=health_loop, name="drone-local-peer-health", daemon=True).start()
