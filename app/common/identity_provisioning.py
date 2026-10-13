"""Clone-safe Drone identity: hardware binding, automatic reprovisioning, recovery.

Runs once at the very start of ``main()`` -- after logging is up, before any listener,
poller, discovery or peer worker exists -- under an exclusive provisioning lock:

    UNINITIALIZED -> VALIDATING -> READY
                              \\-> CLONE_DETECTED -> PROVISIONING -> READY
                              \\-> RECOVERY_REQUIRED   (swarm networking held)

The durable *identity binding* (``identity/binding.json``) ties the installation id,
logical Drone id and identity-certificate fingerprint to a hashed hardware
fingerprint (``hardware_identity``). It is bookkeeping, **not** proof of hardware: it is
copied along with the disk, so only a confident hardware mismatch can reveal a clone.

* same machine / first boot / upgrade -> nothing is rewritten.
* confident different machine         -> a brand-new identity is provisioned
  automatically; the inherited keys, pinned peers and swarm rosters are removed
  *before* the new identity exists, and nothing is announced to the old swarm.
* missing / contradictory evidence on a bound install -> keys and state are kept but
  swarm networking stays off until an administrator resolves it (``resolve_recovery``)
  -- a missing identifier is never treated as new hardware.
* a planned motherboard swap can be pre-authorized by an administrator
  (``authorize_hardware_change``), which rebinds instead of reprovisioning.

Provisioning is journaled (``identity/journal.json``) so an interrupted run resumes
rather than resurrecting the inherited identity. Stdlib only.
"""

import dataclasses
import hashlib
import json
import os
import re
import secrets
import ssl
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX development hosts
    fcntl = None  # type: ignore

try:
    from .device_identity import _device_id_path, _normalize_device_id, _write_persisted_machine_id
    from .hardware_identity import (
        BASELINE,
        CONTRADICTORY,
        DIFFERENT,
        FINGERPRINT_VERSION,
        INSUFFICIENT,
        SAME,
        HardwareIdentityProvider,
        HardwareSnapshot,
        compare_hardware,
    )
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.device_identity import (  # type: ignore
        _device_id_path,
        _normalize_device_id,
        _write_persisted_machine_id,
    )
    from common.hardware_identity import (  # type: ignore
        BASELINE,
        CONTRADICTORY,
        DIFFERENT,
        FINGERPRINT_VERSION,
        INSUFFICIENT,
        SAME,
        HardwareIdentityProvider,
        HardwareSnapshot,
        compare_hardware,
    )

BINDING_SCHEMA_VERSION = 1
LOCK_TIMEOUT_SECONDS = 120.0
DEFAULT_AUTHORIZATION_HOURS = 24
MAX_AUTHORIZATION_HOURS = 72
_HISTORY_LIMIT = 20
_COLLISION_EVENT_NAMESPACE = "identity_collisions"


class State(str, Enum):
    UNINITIALIZED = "UNINITIALIZED"
    VALIDATING = "VALIDATING"
    CLONE_DETECTED = "CLONE_DETECTED"
    PROVISIONING = "PROVISIONING"
    READY = "READY"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


# Status values for the Admin/System UI.
STATUS_VERIFIED = "identity_verified"
STATUS_NEW_INSTALLATION = "new_installation_provisioned"
STATUS_CLONE = "clone_automatically_provisioned"
STATUS_UNAVAILABLE = "identity_verification_unavailable"
STATUS_RECOVERY = "identity_recovery_required"
STATUS_MANAGED = "identity_managed_externally"


class IdentityError(RuntimeError):
    pass


@dataclasses.dataclass
class IdentityResult:
    settings: Any
    state: State
    status: str
    origin: str = ""
    reason: str = ""
    swarm_networking_enabled: bool = True


_STATUS_LOCK = threading.Lock()
_CURRENT: Dict[str, Any] = {"state": State.UNINITIALIZED.value, "status": "", "reason": "", "swarm_networking_enabled": True}
_PROCESS_LOCK = threading.RLock()
_FINGERPRINT_CACHE: Dict[str, Any] = {}


def _log(message: str) -> None:
    print(f"[Identity] {message}", file=sys.stdout, flush=True)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _now_iso() -> str:
    return _now().isoformat()


# ----------------------------------------------------------------------- gate


def swarm_networking_allowed() -> bool:
    """False while identity is unresolved: no discovery, pairing, mTLS peer traffic or sync."""
    with _STATUS_LOCK:
        return bool(_CURRENT.get("swarm_networking_enabled", True))


def _publish(state: State, status: str, reason: str, swarm_allowed: bool, origin: str = "") -> None:
    with _STATUS_LOCK:
        _CURRENT.update(
            state=state.value,
            status=status,
            reason=reason,
            swarm_networking_enabled=swarm_allowed,
            origin=origin,
        )


def reset_runtime_state_for_tests() -> None:
    with _STATUS_LOCK:
        _CURRENT.clear()
        _CURRENT.update(state=State.UNINITIALIZED.value, status="", reason="", swarm_networking_enabled=True)
    _FINGERPRINT_CACHE.clear()


# ---------------------------------------------------------------------- paths


def identity_dir(settings: Any) -> Path:
    configured = os.environ.get("DRONE_IDENTITY_DIR")
    if configured:
        return Path(configured)
    return Path(settings.userdata_root) / "system" / "drone-app" / "identity"


def _binding_path(settings: Any) -> Path:
    return identity_dir(settings) / "binding.json"


def _journal_path(settings: Any) -> Path:
    return identity_dir(settings) / "journal.json"


def _recovery_path(settings: Any) -> Path:
    return identity_dir(settings) / "recovery.json"


def _authorization_path(settings: Any) -> Path:
    return identity_dir(settings) / "hardware-change-authorization.json"


def _lock_path(settings: Any) -> Path:
    return identity_dir(settings) / "provisioning.lock"


def _ensure_dir(settings: Any) -> Path:
    directory = identity_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    _fsync_directory(path.parent)


_ABSENT = object()
_CORRUPT = object()


def _read_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _ABSENT
    except OSError:
        return _CORRUPT
    try:
        value = json.loads(text)
    except ValueError:
        return _CORRUPT
    return value if isinstance(value, dict) else _CORRUPT


class _ProvisioningLock:
    """Cross-process (flock) + in-process exclusive lock with a bounded wait."""

    def __init__(self, settings: Any):
        self._path = _lock_path(settings)
        self._settings = settings
        self._handle = None

    def __enter__(self) -> "_ProvisioningLock":
        _PROCESS_LOCK.acquire()
        try:
            _ensure_dir(self._settings)
            if fcntl is not None:
                self._handle = open(self._path, "a+")
                try:
                    os.chmod(self._path, 0o600)
                except OSError:
                    pass
                deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
                while True:
                    try:
                        fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise IdentityError("timed out waiting for the identity provisioning lock")
                        time.sleep(0.1)
        except BaseException:
            self._release()
            raise
        return self

    def _release(self) -> None:
        if self._handle is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self._handle, fcntl.LOCK_UN)
            except OSError:
                pass
            self._handle.close()
            self._handle = None
        _PROCESS_LOCK.release()

    def __exit__(self, *_exc: object) -> None:
        self._release()


# ---------------------------------------------------------------- certificates


def _certificate_manager(settings: Any):
    try:
        from ..transfer.drone_tls import DroneCertificateManager
    except ImportError:  # pragma: no cover - direct script execution fallback
        from transfer.drone_tls import DroneCertificateManager  # type: ignore
    return DroneCertificateManager(settings)


def _identity_pair_status(settings: Any) -> Dict[str, Any]:
    """Describe the on-disk identity certificate/key pair without modifying anything."""
    cert_file, key_file = settings.drone_cert_file, settings.drone_key_file
    status: Dict[str, Any] = {"present": cert_file.exists() or key_file.exists(), "valid": False, "fingerprint": ""}
    if not (cert_file.is_file() and key_file.is_file()):
        return status
    metadata = _certificate_manager(settings).metadata()
    if metadata.get("status") != "loaded":
        return status
    status["fingerprint"] = str(metadata.get("fingerprint") or "").lower()
    status["expired"] = metadata.get("renewal_status") == "expired"
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    except (ssl.SSLError, OSError, ValueError):
        return status
    status["valid"] = not status["expired"]
    return status


def own_certificate_fingerprint(settings: Any) -> str:
    """SHA-256 of this Drone's identity certificate (cached by mtime); '' if unavailable."""
    configured = getattr(settings, "drone_cert_file", None)
    if configured is None:
        return ""
    cert_file = Path(configured)
    try:
        stamp = (str(cert_file), cert_file.stat().st_mtime_ns, cert_file.stat().st_size)
    except OSError:
        return ""
    cached = _FINGERPRINT_CACHE.get("value")
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        der = ssl.PEM_cert_to_DER_cert(cert_file.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return ""
    fingerprint = hashlib.sha256(der).hexdigest()
    _FINGERPRINT_CACHE["value"] = (stamp, fingerprint)
    return fingerprint


def _wipe_file(path: Path, strict: bool = True) -> None:
    """Overwrite then unlink, so a retired private key is not left readable.

    ``strict`` (identity key/certificate): failing to remove the file raises, because the
    inherited identity must never survive. Hygiene-only callers pass ``strict=False``.
    """
    try:
        size = path.stat().st_size
        if path.is_file() and not path.is_symlink() and size:
            with open(path, "r+b") as handle:
                handle.write(b"\0" * size)
                handle.flush()
                os.fsync(handle.fileno())
    except OSError:
        pass
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        if strict:
            raise


def _externally_managed(settings: Any) -> Optional[str]:
    if getattr(settings, "use_fake_data", False):
        return "fake_data"
    if str(getattr(settings, "drone_mtls_mode", "")) == "managed":
        return "managed_mtls"
    if getattr(settings, "tls_cert_file", None) is not None:
        return "external_tls_certificate"
    return None


def _env_pinned_device_id() -> bool:
    return bool(_normalize_device_id(os.environ.get("DRONE_DEVICE_ID") or os.environ.get("OVERMIND_DEVICE_ID")))


# --------------------------------------------------------------------- binding


def _load_binding(settings: Any):
    """Return (binding, problem). ``binding`` is None when absent or unusable."""
    raw = _read_json(_binding_path(settings))
    if raw is _ABSENT:
        return None, None
    if raw is _CORRUPT:
        return None, "binding_unreadable"
    try:
        version = int(raw.get("schema_version"))
    except (TypeError, ValueError):
        return None, "binding_invalid"
    if version < 1:
        return None, "binding_invalid"
    installation_id = raw.get("installation_id")
    raw_device_id = raw.get("device_id")
    raw_fingerprint = raw.get("identity_certificate_fingerprint")
    if not all(isinstance(value, str) for value in (installation_id, raw_device_id, raw_fingerprint)):
        return None, "binding_invalid"
    device_id = _normalize_device_id(raw_device_id)
    fingerprint = raw_fingerprint.lower()
    if not installation_id or not device_id or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        return None, "binding_invalid"
    hardware = HardwareSnapshot.from_dict(raw.get("hardware"))
    if (raw.get("hardware_bound") or raw.get("hardware_fingerprint")) and not (hardware and hardware.components):
        # The binding says it once recorded hardware evidence but none is left: damaged or
        # erased, so it cannot be silently re-baselined onto whatever machine is booting.
        return None, "binding_hardware_evidence_missing"
    binding = dict(raw)
    binding["device_id"] = device_id
    binding["identity_certificate_fingerprint"] = fingerprint
    binding["hardware"] = hardware
    if not isinstance(binding.get("history"), list):
        binding["history"] = []
    return binding, None


def _binding_document(binding: Dict[str, Any]) -> dict:
    hardware: Optional[HardwareSnapshot] = binding.get("hardware")
    document = {key: value for key, value in binding.items() if key != "hardware"}
    document["schema_version"] = BINDING_SCHEMA_VERSION
    document["hardware"] = hardware.to_dict() if hardware else None
    document["hardware_fingerprint"] = hardware.fingerprint if hardware else None
    document["hardware_bound"] = bool(hardware and hardware.components)
    document["hardware_fingerprint_version"] = hardware.version if hardware else FINGERPRINT_VERSION
    document["history"] = list(binding.get("history") or [])[-_HISTORY_LIMIT:]
    return document


def _save_binding(settings: Any, binding: Dict[str, Any]) -> None:
    _atomic_write_json(_binding_path(settings), _binding_document(binding))


def _history_entry(event: str, **details: Any) -> dict:
    return {"at": _now_iso(), "event": event, **{key: value for key, value in details.items() if value not in (None, "")}}


def _new_binding(
    settings: Any,
    hardware: Optional[HardwareSnapshot],
    certificate_fingerprint: str,
    origin: str,
    history: List[dict],
    installation_id: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "schema_version": BINDING_SCHEMA_VERSION,
        "installation_id": installation_id or secrets.token_hex(16),
        "device_id": settings.device_id,
        "hardware": hardware if hardware and hardware.components else None,
        "identity_certificate_fingerprint": certificate_fingerprint,
        "provisioned_at": _now_iso(),
        "origin": origin,
        "history": history,
    }


# --------------------------------------------------------------- authorization


def hardware_change_authorization(settings: Any) -> Optional[dict]:
    raw = _read_json(_authorization_path(settings))
    if raw is _ABSENT or raw is _CORRUPT:
        return None
    try:
        expires = datetime.fromisoformat(str(raw.get("expires_at")))
    except (TypeError, ValueError):
        return None
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= _now():
        return None
    return {"authorized_at": raw.get("authorized_at"), "expires_at": expires.isoformat(), "authorized_by": raw.get("authorized_by", "")}


def authorize_hardware_change(settings: Any, hours: Optional[float] = None, actor: str = "") -> dict:
    """Allow ONE upcoming hardware change to keep this identity (planned motherboard swap).

    Callers must have authenticated an administrator. Do not image the disk while an
    authorization is pending: every clone booted from that image would keep this identity.
    """
    try:
        window = float(hours if hours is not None else DEFAULT_AUTHORIZATION_HOURS)
    except (TypeError, ValueError):
        raise ValueError("hours must be a number")
    if not 0 < window <= MAX_AUTHORIZATION_HOURS:
        raise ValueError(f"hours must be between 0 and {MAX_AUTHORIZATION_HOURS}")
    with _ProvisioningLock(settings):
        if _externally_managed(settings):
            raise ValueError("identity is managed externally on this Drone")
        authorized = _now()
        payload = {
            "authorized_at": authorized.isoformat(),
            "expires_at": (authorized + timedelta(hours=window)).isoformat(),
            "authorized_by": str(actor or "")[:64],
        }
        _atomic_write_json(_authorization_path(settings), payload)
    _log("Hardware change authorized by an administrator; the next hardware change keeps this identity")
    return hardware_change_authorization(settings) or {}


def cancel_hardware_change(settings: Any) -> bool:
    with _ProvisioningLock(settings):
        try:
            _authorization_path(settings).unlink()
            removed = True
        except FileNotFoundError:
            removed = False
    if removed:
        _log("Pending hardware-change authorization cancelled")
    return removed


def _consume_authorization(settings: Any) -> None:
    try:
        _authorization_path(settings).unlink()
    except FileNotFoundError:
        pass


# ------------------------------------------------------------------ provisioning


def _record_journal(settings: Any, journal: dict) -> None:
    _atomic_write_json(_journal_path(settings), journal)


def _valid_journal(journal: dict) -> bool:
    """Accept only journals that our provisioning state machine could have written.

    A JSON object is not evidence that an identity was being retired. In
    particular, a stray ``{"reason": "clone"}`` must not destroy an intact
    original or trap it in a recovery state that forbids keeping its identity.
    """
    if journal.get("reason") not in {"initial", "clone", "operator_new_identity"}:
        return False
    if journal.get("state") not in {State.CLONE_DETECTED.value, State.PROVISIONING.value}:
        return False
    if not isinstance(journal.get("steps"), dict) or not isinstance(journal.get("started_at"), str):
        return False
    if not journal["started_at"]:
        return False
    for field in ("previous_device_id", "previous_installation_id", "previous_certificate_fingerprint"):
        if not isinstance(journal.get(field), str):
            return False
    previous = journal["previous_certificate_fingerprint"]
    if previous and not re.fullmatch(r"[0-9a-fA-F]{64}", previous):
        return False
    if HardwareSnapshot.from_dict(journal.get("hardware")) is None:
        return False
    new_device_id = journal.get("new_device_id")
    if new_device_id is not None and (not isinstance(new_device_id, str) or not _normalize_device_id(new_device_id)):
        return False
    return True


def _retire_device_id(settings: Any) -> None:
    try:
        _device_id_path(Path(settings.userdata_root)).unlink()
    except FileNotFoundError:
        pass


def _choose_device_id(settings: Any, journal: dict) -> str:
    """A fresh logical Drone id for a new identity (random, locally administered, MAC-shaped).

    Deliberately not derived from the NIC: cloned VMs and spoofed MACs would hand every
    clone the same "unique" id, so uniqueness must not depend on the hardware report.
    """
    if journal.get("new_device_id"):
        return str(journal["new_device_id"])
    # A clone is a new device even when its copied configuration pins the source's id via
    # DRONE_DEVICE_ID; the retired id is remembered in the binding so the pin cannot revive it.
    userdata_root = Path(settings.userdata_root)
    _retire_device_id(settings)
    _device_id_path(userdata_root).parent.mkdir(parents=True, exist_ok=True)
    octets = bytearray(secrets.token_bytes(6))
    octets[0] = (octets[0] | 0x02) & 0xFE
    candidate = ":".join(f"{octet:02x}" for octet in octets)
    _write_persisted_machine_id(userdata_root, candidate)
    return candidate


def _retire_old_identity(settings: Any, journal: dict) -> None:
    """Destroy the inherited private key/certificate (and the leaf it signed)."""
    previous = str(journal.get("previous_certificate_fingerprint") or "").lower()
    cert_file, key_file = settings.drone_cert_file, settings.drone_key_file
    current = _identity_pair_status(settings)
    resuming = bool(journal.get("steps", {}).get("retired"))
    # On a resumed run only the *inherited* certificate may be removed; a valid pair with a
    # different fingerprint is the new identity a previous attempt already produced.
    if current["present"] and (not resuming or not current["valid"] or current["fingerprint"] == previous):
        _wipe_file(key_file)
        _wipe_file(cert_file)
    tls_dir = Path(settings.tls_self_signed_dir)
    for name in ("server.key", "server.crt"):
        _wipe_file(tls_dir / name, strict=False)


def _provision(
    settings: Any,
    hardware: HardwareSnapshot,
    reason: str,
    previous: Optional[Dict[str, Any]],
    journal: Optional[dict] = None,
) -> IdentityResult:
    """Create (or recreate) the identity. ``reason``: initial | clone | operator_new_identity."""
    destructive = reason != "initial"
    resumed = journal is not None
    if journal is None:
        previous_pair = _identity_pair_status(settings)
        journal = {
            "state": State.CLONE_DETECTED.value if reason == "clone" else State.PROVISIONING.value,
            "reason": reason,
            "started_at": _now_iso(),
            "previous_certificate_fingerprint": (previous or {}).get("identity_certificate_fingerprint")
            or previous_pair.get("fingerprint", ""),
            "previous_device_id": (previous or {}).get("device_id") or settings.device_id,
            "previous_installation_id": (previous or {}).get("installation_id", ""),
            "hardware": hardware.to_dict(),
            "steps": {},
        }
        _record_journal(settings, journal)
    steps = journal.setdefault("steps", {})
    _publish(State(journal["state"]), STATUS_RECOVERY, reason, False)
    if reason == "clone":
        _log(f"Clone reprovisioning {'resumed' if resumed else 'initiated'}")
    elif reason == "initial":
        _log("Provisioning a new installation identity")
    else:
        _log("Provisioning a new identity at administrator request")
    journal["state"] = State.PROVISIONING.value
    _record_journal(settings, journal)
    _publish(State.PROVISIONING, STATUS_RECOVERY, reason, False)

    if destructive:
        _retire_old_identity(settings, journal)
        steps["retired"] = True
        _record_journal(settings, journal)

        from_swarm = _swarm_reset_module()
        result = from_swarm.reset_swarm_identity_state(settings)
        incomplete = list(result.get("incomplete", []))
        steps["swarm_reset"] = {"incomplete": incomplete}
        _record_journal(settings, journal)
        if incomplete:
            # Imported credentials / enrollment keys from the old swarm are still usable; the
            # journal makes the next start repeat the (idempotent) cleanup.
            raise IdentityError("inherited credentials could not be cleared: " + ", ".join(incomplete))

        new_device_id = _choose_device_id(settings, journal)
        journal["new_device_id"] = new_device_id
        _record_journal(settings, journal)
        settings = dataclasses.replace(settings, device_id=new_device_id)

        tailnet = _reset_tailnet()
        if tailnet.get("status") == "error":
            # The inherited node identity could not be cleared. Without a confirmed hold the
            # clone would keep presenting it, so only a successful hold lets provisioning go on.
            try:
                held = bool(_tailnet_module().hold_local_node())
            except Exception:  # noqa: BLE001
                held = False
            if not held:
                raise IdentityError("the inherited Tailscale node identity could neither be cleared nor taken offline")
            tailnet = {"status": "held", "detail": tailnet.get("detail", "")}
        steps["tailnet"] = tailnet
        journal["tailnet_status"] = tailnet.get("status")
        _record_journal(settings, journal)
    elif journal.get("new_device_id"):
        settings = dataclasses.replace(settings, device_id=journal["new_device_id"])

    manager = _certificate_manager(settings)
    pair = _identity_pair_status(settings)
    previous_fingerprint = str(journal.get("previous_certificate_fingerprint") or "").lower()
    if not pair["valid"] or (destructive and pair["fingerprint"] == previous_fingerprint):
        manager._generate_local_certificate(settings.drone_cert_file, settings.drone_key_file)
        pair = _identity_pair_status(settings)
    if not pair["valid"]:
        raise IdentityError("the new Drone identity certificate could not be verified")
    if destructive and previous_fingerprint and pair["fingerprint"] == previous_fingerprint:
        raise IdentityError("the new Drone identity certificate matches the inherited one")
    _log("New cryptographic identity created")
    steps["certificate"] = True
    _record_journal(settings, journal)

    origin = {"initial": "initial", "clone": "clone"}.get(reason, "operator")
    event = {"initial": "installation_provisioned", "clone": "clone_provisioned"}.get(reason, "identity_replaced")
    existing_history = list((previous or {}).get("history") or [])
    retired_ids = list((previous or {}).get("retired_device_ids") or [])
    if destructive:
        old_id = str(journal.get("previous_device_id") or "")
        if old_id and old_id != settings.device_id and old_id not in retired_ids:
            retired_ids.append(old_id)
    binding = _new_binding(
        settings,
        hardware,
        pair["fingerprint"],
        origin,
        existing_history
        + [
            _history_entry(
                event,
                hardware=(hardware.fingerprint or "")[:12],
                previous_certificate=previous_fingerprint[:12],
                tailnet=journal.get("tailnet_status"),
            )
        ],
    )
    if retired_ids:
        binding["retired_device_ids"] = retired_ids[-_HISTORY_LIMIT:]
    if journal.get("tailnet_status") == "held":
        binding["tailnet_reset_pending"] = True
    _save_binding(settings, binding)
    try:
        _journal_path(settings).unlink()
    except FileNotFoundError:
        pass
    try:
        _recovery_path(settings).unlink()
    except FileNotFoundError:
        pass
    if destructive:
        _log("Clone provisioning completed" if reason == "clone" else "Identity replacement completed")
    status = {"initial": STATUS_NEW_INSTALLATION, "clone": STATUS_CLONE}.get(reason, STATUS_VERIFIED)
    _publish(State.READY, status, reason, True, origin)
    _log("Swarm networking enabled (unpaired, independent identity)" if destructive else "Swarm networking enabled")
    return IdentityResult(settings, State.READY, status, origin=origin, reason=reason)


def _swarm_reset_module():
    try:
        from ..transfer import swarm_reset
    except ImportError:  # pragma: no cover - direct script execution fallback
        from transfer import swarm_reset  # type: ignore
    return swarm_reset


def _tailnet_module():
    try:
        from ..device import tailnet_service
    except ImportError:  # pragma: no cover - direct script execution fallback
        from device import tailnet_service  # type: ignore
    return tailnet_service


def _reset_tailnet() -> dict:
    try:
        result = _tailnet_module().reset_local_node_identity()
    except Exception as error:  # noqa: BLE001 - LAN functionality must not depend on Tailscale
        return {"status": "error", "detail": error.__class__.__name__}
    status = result.get("status")
    if status == "reset":
        _log("Inherited Tailscale node identity removed locally; tailnet enrollment required")
    elif status == "held":
        _log("Inherited Tailscale node taken offline; local reset will be retried on next start")
    return result


# ----------------------------------------------------------------------- recovery


def _enter_recovery(settings: Any, reason: str, binding: Optional[dict], detail: str = "") -> IdentityResult:
    held = False
    try:
        held = bool(_tailnet_module().hold_local_node())
    except Exception:  # noqa: BLE001
        held = False
    marker = _read_json(_recovery_path(settings))
    since = marker.get("since") if isinstance(marker, dict) else None
    _atomic_write_json(
        _recovery_path(settings),
        {"reason": reason, "detail": detail, "since": since or _now_iso(), "tailnet_held": held or bool(isinstance(marker, dict) and marker.get("tailnet_held"))},
    )
    _publish(State.RECOVERY_REQUIRED, STATUS_RECOVERY, reason, False)
    _log(f"Identity recovery required ({reason}); swarm networking is held until an administrator resolves it")
    return IdentityResult(settings, State.RECOVERY_REQUIRED, STATUS_RECOVERY, reason=reason, swarm_networking_enabled=False)


def _clear_recovery(settings: Any) -> dict:
    marker = _read_json(_recovery_path(settings))
    try:
        _recovery_path(settings).unlink()
    except FileNotFoundError:
        pass
    return marker if isinstance(marker, dict) else {}


def _release_after_recovery(settings: Any) -> None:
    """Drop a stale recovery marker; bring back a tailnet node we took offline while holding."""
    marker = _clear_recovery(settings)
    if marker.get("tailnet_held"):
        try:
            _tailnet_module().release_local_node()
        except Exception:  # noqa: BLE001 - best effort
            pass


def resolve_recovery(settings: Any, action: str, provider: Optional[HardwareIdentityProvider] = None, actor: str = "") -> dict:
    """Administrator decision for RECOVERY_REQUIRED: ``keep`` this identity or ``new`` identity.

    ``new`` is also accepted when nothing is pending ("provision a new identity on this machine").

    The caller must have authenticated an administrator (and re-verified their password).
    Returns a summary; the Drone should restart afterwards so every subsystem starts
    under the resolved identity.
    """
    if action not in {"keep", "new"}:
        raise ValueError("action must be 'keep' or 'new'")
    provider = provider or HardwareIdentityProvider()
    with _ProvisioningLock(settings):
        if _externally_managed(settings):
            raise ValueError("identity is managed externally on this Drone")
        marker = _read_json(_recovery_path(settings))
        pending = marker is not _ABSENT and marker is not _CORRUPT
        if not pending and action == "keep":
            raise ValueError("no identity recovery is pending")
        # ``new`` also works with nothing pending: it is how an administrator replaces the
        # identity of a machine cloned before hardware binding existed (see docs/drone-identity.md).
        reason = str(marker.get("reason") or "") if pending else ""
        binding, _problem = _load_binding(settings)
        hardware = provider.collect()
        if action == "new":
            journal = _read_json(_journal_path(settings))
            journal = journal if isinstance(journal, dict) else None
            result = _provision(
                settings, hardware, "operator_new_identity" if journal is None else str(journal.get("reason") or "clone"),
                binding, journal,
            )
            _log("Administrator provisioned a new identity")
            return {"action": "new", "status": result.status, "device_id": result.settings.device_id}
        if reason == "provisioning_failed":
            raise ValueError("an unfinished provisioning can only be resolved by provisioning a new identity")
        pair = _identity_pair_status(settings)
        if not pair["valid"]:
            raise ValueError("the existing identity certificate is not usable; provision a new identity instead")
        history = list((binding or {}).get("history") or [])
        history.append(_history_entry("identity_kept_on_new_hardware", via="administrator", reason=reason, actor=str(actor)[:64],
                                      hardware=(hardware.fingerprint or "")[:12]))
        if binding is None:
            binding = _new_binding(settings, hardware, pair["fingerprint"], "adopted", history)
        else:
            # Even an explicit decision to keep this identity cannot replace a recorded
            # hardware baseline with an unreadable one. A later clone still needs it.
            if hardware.usable:
                binding["hardware"] = hardware
            binding.update(identity_certificate_fingerprint=pair["fingerprint"], history=history)
        _save_binding(settings, binding)
        _release_after_recovery(settings)
        _publish(State.READY, STATUS_VERIFIED, "", True, binding.get("origin", ""))
        _log("Administrator kept the existing identity on this hardware")
        return {"action": "keep", "status": STATUS_VERIFIED, "device_id": binding["device_id"]}


# ------------------------------------------------------------------ main entry


def ensure_identity(settings: Any, provider: Optional[HardwareIdentityProvider] = None) -> IdentityResult:
    """Validate (or provision) this Drone's identity. Idempotent; call before any networking."""
    _publish(State.VALIDATING, "", "", False)
    _log("Validating hardware binding")
    managed = _externally_managed(settings)
    if managed:
        _publish(State.READY, STATUS_MANAGED, managed, True)
        _log(f"Identity lifecycle is managed externally ({managed}); clone detection skipped")
        return IdentityResult(settings, State.READY, STATUS_MANAGED, reason=managed)
    provider = provider or HardwareIdentityProvider()
    try:
        with _ProvisioningLock(settings):
            return _ensure_locked(settings, provider)
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        _log(f"Identity provisioning did not complete ({error.__class__.__name__}); it will be retried on the next start")
        try:
            _ensure_dir(settings)
            _atomic_write_json(
                _recovery_path(settings),
                {"reason": "provisioning_failed", "detail": error.__class__.__name__, "since": _now_iso()},
            )
            journal = _read_json(_journal_path(settings))
            if isinstance(journal, dict) and journal.get("reason") != "initial":
                # A half-reprovisioned clone must not keep presenting the inherited tailnet node.
                _tailnet_module().hold_local_node()
        except Exception:  # noqa: BLE001 - the failure is already reported above
            pass
        _publish(State.RECOVERY_REQUIRED, STATUS_RECOVERY, "provisioning_failed", False)
        return IdentityResult(settings, State.RECOVERY_REQUIRED, STATUS_RECOVERY, reason="provisioning_failed", swarm_networking_enabled=False)


def _ensure_locked(settings: Any, provider: HardwareIdentityProvider) -> IdentityResult:
    hardware = provider.collect()
    journal = _read_json(_journal_path(settings))
    if journal is _CORRUPT or (isinstance(journal, dict) and not _valid_journal(journal)):
        # The journal contains no trustworthy intent. It may be a torn provisioning write,
        # or simply a damaged file on the original machine. Re-derive the decision from the
        # durable binding and present hardware before doing anything destructive.
        binding, problem = _load_binding(settings)
        if binding is None:
            return _enter_recovery(settings, problem or "journal_unreadable", None)
        verdict = compare_hardware(binding.get("hardware"), hardware)
        if verdict == DIFFERENT:
            _log("High-confidence hardware mismatch detected with an unreadable provisioning journal")
            return _provision(settings, hardware, "clone", binding)
        pair = _identity_pair_status(settings)
        if (
            verdict == SAME
            and pair["valid"]
            and pair["fingerprint"] == binding["identity_certificate_fingerprint"]
            and settings.device_id == binding["device_id"]
        ):
            # The bound original is intact. A corrupt journal alone must never retire it.
            result = _verified(settings, binding, hardware, pair, hardware_verified=True)
            if result.state == State.READY:
                _journal_path(settings).unlink()
                _fsync_directory(identity_dir(settings))
                _log("Unreadable provisioning journal discarded after verifying the existing identity")
            return result
        return _enter_recovery(settings, "journal_unreadable", binding)
    if isinstance(journal, dict):
        binding, _ = _load_binding(settings)
        _log("Resuming an interrupted identity provisioning")
        return _provision(settings, hardware, str(journal.get("reason") or "clone"), binding, journal)

    binding, problem = _load_binding(settings)
    pair = _identity_pair_status(settings)

    if binding is None and problem is None:
        if not pair["present"]:
            return _provision(settings, hardware, "initial", None)
        return _migrate_existing(settings, hardware, pair)
    if binding is None:
        return _enter_recovery(settings, problem or "binding_invalid", None)

    stored: Optional[HardwareSnapshot] = binding.get("hardware")
    verdict = compare_hardware(stored, hardware)
    if stored is None or not stored.components:
        if verdict == BASELINE:
            return _adopt_baseline(settings, binding, hardware, pair)
        return _verified(settings, binding, hardware, pair, hardware_verified=False)
    if verdict == SAME:
        _release_after_recovery(settings)
        return _verified(settings, binding, hardware, pair, hardware_verified=True)

    _log(f"Hardware evidence differs from the recorded binding (verdict={verdict}, "
         f"stored={(stored.fingerprint or '')[:12]}, current={(hardware.fingerprint or '')[:12] or 'unavailable'})")
    # An authorization is for a measured hardware change. A temporarily unreadable or
    # contradictory reading must neither consume it nor replace the saved binding with
    # missing evidence: that would make a subsequent disk image impossible to detect.
    if verdict == DIFFERENT and hardware.usable and hardware_change_authorization(settings):
        return _rebind_authorized(settings, binding, hardware, pair)
    if verdict == DIFFERENT:
        _log("High-confidence hardware mismatch detected")
        return _provision(settings, hardware, "clone", binding)
    return _enter_recovery(settings, "insufficient_evidence" if verdict == INSUFFICIENT else "contradictory_evidence", binding)


def _migrate_existing(settings: Any, hardware: HardwareSnapshot, pair: dict) -> IdentityResult:
    """An installation that predates the binding: keep identity and swarm, record a baseline."""
    if not pair["valid"]:
        # Same repair ensure_certificate() has always done for a missing/expired certificate.
        _certificate_manager(settings)._generate_local_certificate(settings.drone_cert_file, settings.drone_key_file)
        pair = _identity_pair_status(settings)
        if not pair["valid"]:
            raise IdentityError("the Drone identity certificate could not be repaired")
    binding = _new_binding(
        settings, hardware, pair["fingerprint"], "migrated",
        [_history_entry("binding_migrated", hardware=(hardware.fingerprint or "")[:12], verification="hardware" if hardware.components else "unavailable")],
    )
    _save_binding(settings, binding)
    _log("Existing installation verified; hardware baseline recorded" if hardware.components
         else "Existing installation recorded; hardware identifiers unavailable")
    status = STATUS_VERIFIED if hardware.components else STATUS_UNAVAILABLE
    _publish(State.READY, status, "migrated", True, "migrated")
    _log("Swarm networking enabled")
    return IdentityResult(settings, State.READY, status, origin="migrated", reason="migrated")


def _adopt_baseline(settings: Any, binding: dict, hardware: HardwareSnapshot, pair: dict) -> IdentityResult:
    binding["hardware"] = hardware
    binding["history"].append(_history_entry("hardware_baseline_established", hardware=(hardware.fingerprint or "")[:12]))
    _save_binding(settings, binding)
    return _verified(settings, binding, hardware, pair, hardware_verified=True)


def _rebind_authorized(settings: Any, binding: dict, hardware: HardwareSnapshot, pair: dict) -> IdentityResult:
    if not pair["valid"]:
        return _enter_recovery(settings, "certificate_invalid", binding)
    if pair["fingerprint"] != binding["identity_certificate_fingerprint"]:
        return _enter_recovery(settings, "certificate_mismatch", binding)
    binding["hardware"] = hardware
    binding["identity_certificate_fingerprint"] = pair["fingerprint"]
    binding["history"].append(_history_entry("hardware_rebound", via="pre-authorization", hardware=(hardware.fingerprint or "")[:12]))
    _save_binding(settings, binding)
    _consume_authorization(settings)
    _release_after_recovery(settings)
    _log("Hardware change accepted under administrator pre-authorization; identity preserved")
    return _verified(settings, binding, hardware, pair, hardware_verified=bool(hardware.components))


def _verified(settings: Any, binding: dict, hardware: HardwareSnapshot, pair: dict, hardware_verified: bool) -> IdentityResult:
    """Reconcile binding, certificate and device id; write only when something differs."""
    changed = False
    if pair["valid"] and pair["fingerprint"] != binding["identity_certificate_fingerprint"]:
        # A valid certificate the binding never recorded (swapped in from another installation,
        # or an unrecorded rotation) must not silently become this Drone's identity; the
        # administrator-initiated rotation path updates the binding itself.
        return _enter_recovery(settings, "certificate_mismatch", binding)
    if not pair["valid"]:
        # Missing/corrupt/expired certificate on the *same* machine: re-issue it (peers must
        # re-pin, as with any certificate rotation) -- never fall back to an unchecked key.
        _certificate_manager(settings)._generate_local_certificate(settings.drone_cert_file, settings.drone_key_file)
        pair = _identity_pair_status(settings)
        if not pair["valid"]:
            raise IdentityError("the Drone identity certificate could not be repaired")
        binding["history"].append(_history_entry("certificate_reissued", certificate=pair["fingerprint"][:12]))
        changed = True
    if pair["fingerprint"] != binding["identity_certificate_fingerprint"]:
        # Only reachable right after the re-issue above.
        binding["identity_certificate_fingerprint"] = pair["fingerprint"]
        changed = True
    if binding["device_id"] != settings.device_id:
        if _env_pinned_device_id() and settings.device_id not in (binding.get("retired_device_ids") or []):
            binding["device_id"] = settings.device_id
            binding["history"].append(_history_entry("device_id_pinned_by_environment"))
        else:
            # The persisted id file was lost or edited; the binding is the source of truth.
            _device_id_path(Path(settings.userdata_root)).parent.mkdir(parents=True, exist_ok=True)
            _write_persisted_machine_id(Path(settings.userdata_root), binding["device_id"])
            settings = dataclasses.replace(settings, device_id=binding["device_id"])
        changed = True
    if binding.get("tailnet_reset_pending"):
        outcome = _reset_tailnet()
        if outcome.get("status") != "held":
            binding.pop("tailnet_reset_pending", None)
            changed = True
    if changed:
        _save_binding(settings, binding)
    status = STATUS_VERIFIED if hardware_verified else STATUS_UNAVAILABLE
    _publish(State.READY, status, "", True, binding.get("origin", ""))
    if hardware_verified:
        _log("Existing installation verified")
    else:
        _log("Hardware identifiers unavailable; existing identity kept (verification unavailable)")
    _log("Swarm networking enabled")
    return IdentityResult(settings, State.READY, status, origin=str(binding.get("origin") or ""))


def record_certificate_rotation(settings: Any) -> None:
    """Keep the binding current after an administrator-initiated certificate rotation."""
    if _externally_managed(settings) or not _binding_path(settings).exists():
        return
    with _ProvisioningLock(settings):
        binding, _problem = _load_binding(settings)
        pair = _identity_pair_status(settings)
        if binding is None or not pair["valid"] or pair["fingerprint"] == binding["identity_certificate_fingerprint"]:
            return
        binding["identity_certificate_fingerprint"] = pair["fingerprint"]
        binding["history"].append(_history_entry("certificate_rotated", certificate=pair["fingerprint"][:12], via="administrator"))
        _save_binding(settings, binding)


# --------------------------------------------------------------------- diagnostics


def record_collision(settings: Any, source: str, detail: str = "") -> None:
    """Log a duplicate-identity observation. Never changes trust or revokes anyone."""
    safe_source = "".join(ch for ch in str(source) if ch.isalnum() or ch in "-_.")[:48]
    _log(f"Duplicate identity observed ({safe_source}); the connection was isolated and no trust was changed")
    try:
        try:
            from ..storage.state_store import append_event, database_path
        except ImportError:  # pragma: no cover - direct script execution fallback
            from storage.state_store import append_event, database_path  # type: ignore
        append_event(
            database_path(Path(settings.userdata_root)),
            _COLLISION_EVENT_NAMESPACE,
            {"at": _now_iso(), "source": safe_source, "detail": str(detail)[:200]},
            max_events=100,
        )
    except Exception:  # noqa: BLE001 - diagnostics are best effort
        pass


def status_payload(settings: Any, provider: Optional[HardwareIdentityProvider] = None) -> dict:
    with _STATUS_LOCK:
        current = dict(_CURRENT)
    managed = _externally_managed(settings)
    binding, problem = _load_binding(settings) if not managed else (None, None)
    recovery = _read_json(_recovery_path(settings)) if not managed else _ABSENT
    recovery = recovery if isinstance(recovery, dict) else None
    hardware = (provider or HardwareIdentityProvider()).collect()
    stored: Optional[HardwareSnapshot] = binding.get("hardware") if binding else None
    payload: Dict[str, Any] = {
        "state": current.get("state"),
        "status": current.get("status") or (STATUS_MANAGED if managed else ""),
        "reason": current.get("reason", ""),
        "swarm_networking_enabled": bool(current.get("swarm_networking_enabled", True)),
        "device_id": settings.device_id,
        "managed_externally": managed,
        "hardware": hardware.describe(),
        "hardware_verification": "verified" if stored and stored.components else "unavailable",
        "hardware_change_authorization": hardware_change_authorization(settings),
        "recovery": (
            {
                "reason": recovery.get("reason"),
                "since": recovery.get("since"),
                "actions": ["new"] if recovery.get("reason") == "provisioning_failed" else ["keep", "new"],
            }
            if recovery
            else None
        ),
        "binding_problem": problem,
    }
    if binding:
        payload.update(
            installation_id=binding["installation_id"],
            provisioned_at=binding.get("provisioned_at"),
            origin=binding.get("origin"),
            certificate_fingerprint=binding["identity_certificate_fingerprint"],
            bound_hardware=stored.describe() if stored else None,
            history=[
                {key: value for key, value in entry.items() if key in {"at", "event", "via", "reason", "verification", "tailnet"}}
                for entry in binding["history"][-10:]
                if isinstance(entry, dict)
            ],
        )
    return payload


_STATUS_LABELS = {
    STATUS_VERIFIED: "Identity verified",
    STATUS_NEW_INSTALLATION: "New installation provisioned",
    STATUS_CLONE: "Clone automatically provisioned",
    STATUS_UNAVAILABLE: "Identity verification unavailable (no unique hardware identifier)",
    STATUS_RECOVERY: "Identity recovery required (swarm networking held)",
    STATUS_MANAGED: "Identity managed externally",
}


def status_label(status: str) -> str:
    return _STATUS_LABELS.get(status, "Identity not yet validated")


def current_status() -> str:
    with _STATUS_LOCK:
        return str(_CURRENT.get("status") or "")
