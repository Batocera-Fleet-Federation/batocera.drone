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
    from ..web.handlers_network import HandlersNetworkMixin
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from device.tailnet_service import ensure_tailnet_networking, bootstrap_tailnet_from_swarm, tailnet_status, _repair_tailnet_data_path  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from transfer.drone_tls import DroneCertificateManager  # type: ignore
    from transfer.peer_connectivity import _peer_get_json_for_peer, _preferred_peer_address  # type: ignore
    from web.handlers_network import HandlersNetworkMixin  # type: ignore


_LOCAL_NETWORK_STOP: Optional[Event] = None
_LOCAL_NETWORK_DISCOVERY: Optional[Thread] = None
_LOCAL_NETWORK_HEALTH: Optional[Thread] = None
# Keep the concrete thread type separately from the module-level ``Thread``
# factory. Tests may replace that factory with opaque lifecycle handles, whose
# ``join`` method is the only retirement contract available to this module.
_NATIVE_THREAD_TYPE = Thread
# Serializes start/stop so overlapping callers cannot both read the same live
# generation and then overwrite each other's stop event / worker handles.
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


def _worker_remains_alive(worker: object) -> bool:
    """Return liveness only when a concrete thread gives us a reliable answer.

    Discovery has long supported opaque handles supplied by callers and test
    harnesses. Those handles can model ``join`` without changing a synthetic
    ``is_alive`` result, so treating them as timed out after a completed join
    strands the generation. Actual Python threads, including the peer-health
    worker, retain the strict post-join liveness check.
    """
    return isinstance(worker, _NATIVE_THREAD_TYPE) and worker.is_alive()


def stop_local_network_workers(timeout: float = 2.0) -> None:
    """Stop and join discovery and peer-health workers within ``timeout`` seconds.

    Worker loops wait on the generation event, so signalling it retires their
    pending poll waits immediately.  Discovery additionally needs its socket
    closed before it can be joined. A timed-out join raises ``TimeoutError`` and
    retains the stopped generation for another stop attempt. Callers must not
    remove peer state until this function returns successfully; a restart also
    must finish retiring the previous generation before replacing its handles.
    """
    global _LOCAL_NETWORK_STOP, _LOCAL_NETWORK_DISCOVERY, _LOCAL_NETWORK_HEALTH
    with _LOCAL_NETWORK_LIFECYCLE_LOCK:
        if _LOCAL_NETWORK_STOP is not None:
            _LOCAL_NETWORK_STOP.set()
        deadline = time.monotonic() + max(0.0, timeout)
        discovery = _LOCAL_NETWORK_DISCOVERY
        if discovery is not None:
            # Unblock recvfrom so join is not racing the 1s datagram timeout.
            closer = getattr(discovery, "close_discovery_socket", None)
            if callable(closer):
                closer()
            discovery.join(timeout)
        health = _LOCAL_NETWORK_HEALTH
        if health is not None:
            join = getattr(health, "join", None)
            if callable(join):
                join(max(0.0, deadline - time.monotonic()))
        for worker in (discovery, health):
            if _worker_remains_alive(worker):
                name = getattr(worker, "name", "unknown")
                raise TimeoutError(f"Local-network worker did not stop: {name}")
        _LOCAL_NETWORK_STOP = None
        _LOCAL_NETWORK_DISCOVERY = None
        _LOCAL_NETWORK_HEALTH = None


def local_network_workers_running() -> bool:
    """Return whether a local-network generation is currently live.

    Callers that latch "workers were started" must check this too: a stop
    issued directly on this module clears the live generation, and the latch
    alone would then skip the restart.  A generation whose discovery thread
    died without a stop (crash) is not live either, so it gets restarted too.
    """
    with _LOCAL_NETWORK_LIFECYCLE_LOCK:
        if _LOCAL_NETWORK_STOP is None or _LOCAL_NETWORK_STOP.is_set():
            return False
        discovery = _LOCAL_NETWORK_DISCOVERY
        if discovery is None:
            return False
        # The handle is opaque (tests and harnesses substitute their own), so only
        # a handle that reports liveness can be found dead; otherwise trust the event.
        is_alive = getattr(discovery, "is_alive", None)
        return not callable(is_alive) or bool(is_alive())


def _start_local_network_workers(settings: Settings) -> None:
    with _LOCAL_NETWORK_LIFECYCLE_LOCK:
        _start_local_network_generation(settings)


def _start_local_network_generation(settings: Settings) -> None:
    global _LOCAL_NETWORK_STOP, _LOCAL_NETWORK_DISCOVERY, _LOCAL_NETWORK_HEALTH
    # A live generation must be stopped before its stop event and worker
    # handles are overwritten, otherwise its threads are orphaned.
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

    def tailnet_peer_autopair_retry() -> None:
        # Mirrors the Swarm page's Discover/Refresh button: probe this Drone's
        # tailnet peers and auto-pair (code-free, tailnet-membership-authorized)
        # with any that are Drones not yet paired. Without this, a Drone only
        # ever ran that pass when a human opened the Swarm page and clicked
        # Discover/Refresh -- enrollment converged on its own via the bootstrap
        # retry above, but the actual pairing never did unless someone visited
        # the page. A throwaway HandlersNetworkMixin needs only ``.settings``
        # for this path (no in-flight HTTP request); its one cert-trust call
        # already falls back to the live-TLS-servers registry when ``.server``
        # is absent.
        interval = max(60, int(os.environ.get("DRONE_TAILNET_PEER_SYNC_INTERVAL_SECONDS", "300")))
        helper = HandlersNetworkMixin()
        helper.settings = settings
        while not _wait_for_generation_stop(stop, interval):
            try:
                status = tailnet_status()
                if status.get("enrolled"):
                    helper._sync_tailnet_peers(status)
            except Exception as error:
                print(
                    f"Tailnet peer auto-pair retry failed: {error.__class__.__name__}: {error}",
                    file=sys.stderr,
                    flush=True,
                )

    Thread(target=tailnet_peer_autopair_retry, name="drone-tailnet-peer-autopair", daemon=True).start()

    def fingerprint() -> str:
        return str(DroneCertificateManager(settings).metadata().get("fingerprint") or "")

    _LOCAL_NETWORK_DISCOVERY = _local_network.start_discovery_worker(settings, fingerprint, stop_event=stop)
    interval = max(10, int(os.environ.get("DRONE_LOCAL_HEALTH_INTERVAL_SECONDS", "30")))

    def health_loop() -> None:
        # Sleep in short slices so a stopped or restarted generation retires
        # this thread within one slice instead of lingering a full interval.
        while not _sleep_unless_stopped(stop, interval, slice_seconds=0.1):
            if not _local_network.is_local_mode(settings):
                continue
            checks = []
            for peer in _local_network.paired_peers(settings):
                if stop.is_set():
                    return
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
            if stop.is_set():
                return
            _local_network.save_peer_checks(settings, checks)
            if stop.is_set():
                return
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

    _LOCAL_NETWORK_HEALTH = Thread(target=health_loop, name="drone-local-peer-health", daemon=True)
    _LOCAL_NETWORK_HEALTH.start()
