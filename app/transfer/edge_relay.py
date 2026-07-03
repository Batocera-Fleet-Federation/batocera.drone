"""Edge mux client + relay/hole-punch transfer tiers.

Extracted from ``drone_api.py``. Holds the persistent outbound Edge ``MuxClient``
(``_start_edge_mux_client``), serves offered assets to a receiver over a relay leg
(upgrading to a hole-punched direct path when possible), and pulls a ROM via the Edge
relay (``_relay_download_rom`` and the ``_relay_fetch`` transport tier).
``_edge_token_for`` resolves the live Edge auth token.
"""

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from typing import Optional, Tuple
from urllib.parse import quote

try:
    from ..app_version import drone_app_version as _drone_app_version
    from ..common.settings import Settings
    from ..overmind.overmind_client import _overmind_post_json
    from ..overmind.overmind_config import overmind_load_config
    from ..transport import assetfetch as _assetfetch
    from ..transport import holepunch as _holepunch
    from ..transport import relay_transfer as _relay_transfer
    from ..transport.mux_client import MuxClient, MuxSession, connect_tls, parse_edge_endpoint
    from . import local_network as _local_network
    from .download_errors import DownloadCancelled
    from .network_identity import get_local_certificate_ips as _build_local_certificate_ips
    from .network_identity import get_local_ip_addresses as _build_local_ip_addresses
    from .transfer_files import safe_rom_relative_path as _safe_rom_relative_path
except ImportError:  # pragma: no cover - direct script execution fallback
    from app_version import drone_app_version as _drone_app_version  # type: ignore
    from common.settings import Settings  # type: ignore
    from overmind.overmind_client import _overmind_post_json  # type: ignore
    from overmind.overmind_config import overmind_load_config  # type: ignore
    from transport import assetfetch as _assetfetch  # type: ignore
    from transport import holepunch as _holepunch  # type: ignore
    from transport import relay_transfer as _relay_transfer  # type: ignore
    from transport.mux_client import MuxClient, MuxSession, connect_tls, parse_edge_endpoint  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from transfer.download_errors import DownloadCancelled  # type: ignore
    from transfer.network_identity import get_local_certificate_ips as _build_local_certificate_ips  # type: ignore
    from transfer.network_identity import get_local_ip_addresses as _build_local_ip_addresses  # type: ignore
    from transfer.transfer_files import safe_rom_relative_path as _safe_rom_relative_path  # type: ignore

# The running persistent Edge connection, exposed so the relay transport (sender serve +
# receiver fetch) can multiplex transfers over it. Only edge_relay reads/writes it.
_EDGE_MUX_CLIENT = None


def _serve_transfer_offer(settings: Settings, client: "MuxClient", offer: dict) -> dict:
    """Sender side: stream the offered asset to the receiver.

    Opens a sender relay leg, tries to upgrade it to a direct hole-punched path
    (falling back to relay), then serves the asset (path-safe, under the kind's
    root) over whichever channel resulted. Returns the serve result dict.
    """
    # _resolve_asset_root stays in drone_api (Phase 6 will move it); lazy-import to avoid a cycle.
    try:
        from ..drone_api import _resolve_asset_root
    except ImportError:  # pragma: no cover - flat execution
        from drone_api import _resolve_asset_root  # type: ignore
    session_id = str(offer.get("session_id") or "")
    asset = offer.get("asset") if isinstance(offer.get("asset"), dict) else {}
    if not session_id or not asset:
        return {"status": "error", "bytes": 0}

    def resolve(requested_asset, offset):
        root = _resolve_asset_root(settings, requested_asset.get("kind"))
        if root is None:
            return None
        return _relay_transfer.open_local_file_source(
            root, requested_asset.get("relative_path"), offset
        )

    relay_channel = client.open_relay_session(session_id, "sender")
    transfer_channel, is_direct = _maybe_holepunch(settings, relay_channel)
    try:
        result = _assetfetch.serve_one(transfer_channel, resolve)
        result["transport"] = "holepunch" if is_direct else "relay"
        return result
    finally:
        if is_direct:
            try:
                transfer_channel.close()
            except Exception:  # noqa: BLE001
                pass
        relay_channel.close()


def _handle_transfer_offer(settings: Settings, offer: dict) -> None:
    """Handle a TRANSFER_OFFER from the Edge by serving it on a worker thread
    (the mux read loop invokes this and must not block)."""
    client = _EDGE_MUX_CLIENT
    if client is None:
        return
    session_id = str(offer.get("session_id") or "")

    def run() -> None:
        try:
            result = _serve_transfer_offer(settings, client, offer)
            print(
                f"[edge-relay] served transfer session={session_id} "
                f"status={result.get('status')} bytes={result.get('bytes')}",
                file=sys.stdout,
                flush=True,
            )
        except Exception as error:  # noqa: BLE001 -- never let a serve crash the mux
            print(
                f"[edge-relay] serve failed session={session_id}: {error}",
                file=sys.stderr,
                flush=True,
            )

    Thread(target=run, name="edge-relay-serve", daemon=True).start()


def _edge_mux_available() -> bool:
    """True when a live Edge mux is connected (so relay transfers can run)."""
    client = _EDGE_MUX_CLIENT
    return client is not None and client.connected


def _edge_stun_addr(settings: Settings) -> Optional[Tuple[str, int]]:
    """The Edge's UDP STUN reflector address (host from the edge URL + STUN port)."""
    if not settings.edge_url:
        return None
    try:
        host, _ = parse_edge_endpoint(settings.edge_url)
    except ValueError:
        return None
    return host, int(settings.edge_stun_port)


def _maybe_holepunch(settings: Settings, channel):
    """Try to upgrade a paired relay ``channel`` to a direct hole-punched channel.

    Returns ``(transfer_channel, is_direct)``: a reliable-UDP channel when both
    drones successfully punch + confirm, else the original relay channel so the
    transfer falls back to relaying. Any failure is swallowed (relay fallback)."""
    if not settings.holepunch_enabled:
        return channel, False
    stun_addr = _edge_stun_addr(settings)
    if stun_addr is None:
        return channel, False
    try:
        return _holepunch.negotiate_direct_channel(channel, stun_addr)
    except Exception as error:  # noqa: BLE001 -- never let punch negotiation break a transfer
        print(f"[edge-relay] hole-punch negotiation failed: {error}", file=sys.stderr, flush=True)
        return channel, False


_LOCAL_NETWORK_CACHE: dict = {"at": 0.0, "value": {}}
_LOCAL_NETWORK_SNAPSHOT_TTL_SECONDS = 120.0


def _local_network_snapshot() -> dict:
    """This drone's network info (public_ip + LAN ipv4), cached briefly.

    Used by the LAN-direct transport to detect same-LAN peers (peers behind the
    same NAT report the same public IP). Cached because resolving the public IP
    makes a network call; brief staleness is harmless -- a wrong guess just fails
    the LAN attempt and the selector falls back to the next transport.
    """
    now = time.monotonic()
    cache = _LOCAL_NETWORK_CACHE
    if cache["value"] and now - cache["at"] < _LOCAL_NETWORK_SNAPSHOT_TTL_SECONDS:
        return cache["value"]
    try:
        value = _build_local_ip_addresses()
    except Exception:
        value = {}
    cache["at"] = now
    cache["value"] = value
    return value


def _request_transfer_session(
    settings: Settings, config: dict, source_device_id: str, asset: dict
) -> Tuple[str, str]:
    """Ask Overmind to authorize a relayed pull; return ``(session_id, token)``."""
    base_url = str(config.get("overmind_url") or "").strip().rstrip("/")
    token = str(config.get("overmind_token") or "").strip()
    if not base_url or not token:
        raise RuntimeError("Overmind is not configured; cannot mint a transfer")
    device_id = quote(settings.overmind_device_id, safe="")
    endpoint = f"{base_url}/api/devices/{device_id}/transfers"
    response = _overmind_post_json(
        endpoint, {"source_device_id": source_device_id, "asset": asset}, token=token, settings=settings
    )
    session_id = str(response.get("session_id") or "")
    transfer_token = str(response.get("token") or "")
    if not session_id or not transfer_token:
        raise RuntimeError("transfer authorization returned no session/token")
    return session_id, transfer_token


def _relay_download_rom(
    settings: Settings,
    config: dict,
    peer: dict,
    system: str,
    relative_path: str,
    expected_size=None,
    expected_fingerprint=None,
    progress_callback=None,
    cancellation_event: Optional[Event] = None,
) -> dict:
    """Pull a ROM from ``peer`` over the Edge relay, writing + verifying it locally.

    Mirrors _download_rom_from_peer's skip-if-present + fingerprint-verify +
    activity-dict contract (so a relayed ROM is indistinguishable from a direct
    one to the queue/UI); only the byte source differs (AssetFetch over the mux
    instead of an mTLS HTTP GET).
    """
    # RomRepository stays in drone_api (Phase 4 will move it); lazy-import to avoid a cycle.
    try:
        from ..drone_api import RomRepository
    except ImportError:  # pragma: no cover - flat execution
        from drone_api import RomRepository  # type: ignore
    source_device_id = str(peer.get("drone_id") or peer.get("device_id") or "")
    if not source_device_id:
        raise RuntimeError("relay peer has no device id")
    client = _EDGE_MUX_CLIENT
    if client is None:
        raise RuntimeError("edge mux not connected")

    rel = _safe_rom_relative_path(relative_path)
    system_dir = (settings.roms_root / system).resolve()
    target = (system_dir / rel).resolve()
    if target == system_dir or system_dir not in target.parents:
        raise ValueError("invalid target path")
    partial_target = target.with_name(f"{target.name}.part")
    started_dt = datetime.now(timezone.utc).replace(microsecond=0)
    started = started_dt.isoformat()
    started_mono = time.monotonic()
    expected_fingerprint_clean = str(expected_fingerprint or "").strip().lower()

    def skipped_activity(existing: Path, reason: str) -> dict:
        completed_dt = datetime.now(timezone.utc).replace(microsecond=0)
        duration_ms = int((time.monotonic() - started_mono) * 1000)
        try:
            size = int(existing.stat().st_size)
        except OSError:
            try:
                size = int(expected_size or 0) if expected_size not in (None, "") else 0
            except (TypeError, ValueError):
                size = 0
        try:
            fingerprint = (
                RomRepository.build_fingerprint(existing)
                if existing.is_file()
                else expected_fingerprint_clean
            )
        except Exception:
            fingerprint = expected_fingerprint_clean
        return {
            "source_drone_id": source_device_id,
            "target_drone_id": settings.overmind_device_id,
            "system": system,
            "rom_name": rel,
            "relative_path": existing.relative_to(system_dir).as_posix(),
            "action": "download",
            "status": "skipped",
            "skip_reason": reason,
            "failure_reason": reason,
            "bytes_transferred": 0,
            "file_size": size or expected_size,
            "fingerprint": fingerprint,
            "rom_fingerprint": expected_fingerprint_clean or fingerprint,
            "download_started_at": started,
            "download_completed_at": completed_dt.isoformat(),
            "started_at": started,
            "completed_at": completed_dt.isoformat(),
            "duration_ms": duration_ms,
            "duration_seconds": round(duration_ms / 1000, 3),
            "selected_peer_reason": "local ROM already exists",
        }

    if target.exists():
        return skipped_activity(target, "target path already exists")
    if expected_fingerprint_clean and system_dir.exists() and system_dir.is_dir():
        for candidate in sorted(
            system_dir.rglob("*"), key=lambda path: path.relative_to(system_dir).as_posix().lower()
        ):
            if not candidate.is_file():
                continue
            if RomRepository.should_ignore_rom_path(Path(candidate.relative_to(system_dir).as_posix())):
                continue
            try:
                if RomRepository.build_fingerprint(candidate).lower() == expected_fingerprint_clean:
                    return skipped_activity(candidate, "matching ROM already exists")
            except Exception:
                continue

    # asset.relative_path is relative to roms_root (system/<file>) so the sender
    # resolves it under its own roms_root.
    asset = {"kind": "rom", "relative_path": f"{system}/{rel}"}
    target.parent.mkdir(parents=True, exist_ok=True)
    session_id, transfer_token = _request_transfer_session(settings, config, source_device_id, asset)
    relay_channel = _relay_transfer.open_receiver_channel(
        client, session_id, transfer_token, source_device_id, asset
    )
    transfer_channel, is_direct = _maybe_holepunch(settings, relay_channel)
    bytes_written = 0
    total_bytes = int(expected_size) if expected_size not in (None, "") else None
    try:
        with partial_target.open("wb") as handle:
            def write(chunk: bytes) -> None:
                nonlocal bytes_written
                handle.write(chunk)
                bytes_written += len(chunk)

            def progress(received_total: int) -> None:
                if progress_callback:
                    progress_callback(received_total, total_bytes)

            _assetfetch.download(
                transfer_channel, asset, write, offset=0, progress=progress, cancel=cancellation_event
            )
    except _assetfetch.AssetFetchCancelled as error:
        if partial_target.exists():
            partial_target.unlink()
        raise DownloadCancelled(str(error) or "download cancelled") from error
    except Exception:
        if partial_target.exists():
            partial_target.unlink()
        raise
    finally:
        if is_direct:
            try:
                transfer_channel.close()
            except Exception:  # noqa: BLE001
                pass
        relay_channel.close()

    if expected_size not in (None, ""):
        try:
            if int(expected_size) != bytes_written:
                raise RuntimeError(f"size mismatch expected={expected_size} actual={bytes_written}")
        except ValueError:
            pass
    actual_fingerprint = RomRepository.build_fingerprint(partial_target)
    if expected_fingerprint_clean and actual_fingerprint.lower() != expected_fingerprint_clean:
        if partial_target.exists():
            partial_target.unlink()
        raise RuntimeError(
            f"fingerprint mismatch expected={expected_fingerprint_clean} actual={actual_fingerprint}"
        )
    partial_target.replace(target)
    completed_dt = datetime.now(timezone.utc).replace(microsecond=0)
    duration_ms = int((time.monotonic() - started_mono) * 1000)
    return {
        "source_drone_id": source_device_id,
        "target_drone_id": settings.overmind_device_id,
        "system": system,
        "rom_name": rel,
        "relative_path": target.relative_to(system_dir).as_posix(),
        "action": "download",
        "status": "completed",
        "transport": "holepunch" if is_direct else "relay",
        "bytes_transferred": bytes_written,
        "file_size": expected_size or bytes_written,
        "fingerprint": actual_fingerprint,
        "rom_fingerprint": expected_fingerprint_clean or actual_fingerprint,
        "download_started_at": started,
        "download_completed_at": completed_dt.isoformat(),
        "started_at": started,
        "completed_at": completed_dt.isoformat(),
        "duration_ms": duration_ms,
        "duration_seconds": round(duration_ms / 1000, 3),
        "selected_peer_reason": (
            "direct hole-punched path via Overmind edge"
            if is_direct
            else "relayed via Overmind edge"
        ),
    }


def _relay_fetch(request: "DownloadRequest", context: "TransferContext") -> dict:
    """RelayReceiverTransport fetch dispatch (v1 supports ROM files)."""
    if request.asset_type != "rom":
        raise RuntimeError(f"relay transport does not support asset type {request.asset_type}")
    return _relay_download_rom(
        context.settings,
        context.config,
        context.peer,
        request.system,
        request.relative_path,
        expected_size=request.expected_size,
        expected_fingerprint=request.expected_fingerprint,
        progress_callback=context.progress_callback,
        cancellation_event=context.cancellation_event,
    )


def _edge_token_for(settings: Settings) -> str:
    """Return the Drone's current Overmind token for Edge authentication.

    Reads the live runtime config (which merges the token persisted at claim
    time) so a Drone claimed *after* startup authenticates on its next reconnect
    without a restart; falls back to the static env token.
    """
    try:
        token = str(overmind_load_config(settings).get("overmind_token") or "").strip()
    except Exception:
        token = ""
    return token or (settings.overmind_token or "").strip()


def _start_edge_mux_client(settings: Settings) -> None:
    """Open the persistent outbound connection to the Overmind Edge service.

    Opt-in during rollout (``DRONE_EDGE_ENABLED=1`` + ``DRONE_EDGE_URL``). This is
    what removes the need for any inbound connectivity / port-forwarding: the
    Drone dials out and the Edge pushes presence (and, in later phases, transfer
    signaling and relayed data) back down the same connection.

    Phase 1 scope: authenticate with the Drone's Overmind token, advertise
    capabilities + LAN addresses, and keep the link warm with PING/PONG so the
    Edge can track liveness and report a reflexive (NAT-observed) address. The
    client starts even before the Drone is claimed; the token is read lazily per
    reconnect so it begins authenticating as soon as a token exists.
    """
    edge_url = (settings.edge_url or "").strip()
    if not edge_url:
        print("Edge mux disabled: no DRONE_EDGE_URL configured", file=sys.stdout, flush=True)
        return

    try:
        lan_addrs = [ip for ip in _build_local_certificate_ips() if ip and ip != "127.0.0.1"]
    except Exception:
        lan_addrs = []

    def make_session() -> "MuxSession":
        return MuxSession(
            device_id=settings.overmind_device_id,
            token=_edge_token_for(settings),
            capabilities=["relay"],  # LAN / hole-punch advertised in later phases
            lan_addrs=lan_addrs,
            app_version=_drone_app_version(),
            on_presence=lambda swarm: print(
                f"[edge-mux] presence update: {len(swarm)} peer(s)", file=sys.stdout, flush=True
            ),
        )

    def connect() -> "MuxLink":
        return connect_tls(
            edge_url,
            verify=settings.edge_verify_tls,
            cafile=str(settings.drone_mtls_ca_file) if settings.drone_mtls_ca_file else None,
            client_cert=str(settings.drone_cert_file) if settings.drone_cert_file.exists() else None,
            client_key=str(settings.drone_key_file) if settings.drone_key_file.exists() else None,
        )

    global _EDGE_MUX_CLIENT
    client = MuxClient(
        connect=connect,
        session_factory=make_session,
        ping_interval=float(max(1, settings.edge_ping_seconds)),
        log=lambda message: print(f"[edge-mux] {message}", file=sys.stdout, flush=True),
        on_transfer_offer=lambda offer: _handle_transfer_offer(settings, offer),
    )
    _EDGE_MUX_CLIENT = client
    Thread(target=client.run_forever, name="edge-mux-client", daemon=True).start()
    print(f"Edge mux client started: {edge_url}", file=sys.stdout, flush=True)
