"""Fresh, identity-signed content inventories for ROM/BIOS transfers.

Uses the application's existing OpenSSL dependency, never peer-supplied keys.
The random request nonce prevents replay without relying on synchronized clocks.
"""
import base64
import hashlib
import hmac
import json
from pathlib import Path, PurePosixPath
import re
import secrets
import ssl
import subprocess
import tempfile
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from . import local_network
    from .peer_connectivity import _peer_trust_cafile, _drone_client_ssl_context, _require_swarm_networking
except ImportError:  # pragma: no cover - flat execution
    from transfer import local_network
    from transfer.peer_connectivity import _peer_trust_cafile, _drone_client_ssl_context, _require_swarm_networking

MAX_INVENTORY_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 10000


def canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def safe_path(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or "\\" in value or "\x00" in value:
        raise ValueError("invalid inventory path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ("", ".", "..") for p in value.split("/")):
        raise ValueError("invalid inventory path")
    return value


def file_digest(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return {"file_size": size, "sha256": digest.hexdigest()}


def openssl(args, data):
    result = subprocess.run(["openssl", *args], input=data, capture_output=True, timeout=30, check=False)
    if result.returncode:
        raise ValueError("signed inventory cryptographic operation failed")
    return result.stdout


def sign_inventory(settings, asset_type, system, relative_path, nonce, target):
    _require_swarm_networking()
    if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{64}", nonce):
        raise ValueError("invalid inventory nonce")
    safe_path(relative_path)
    files, directories = [], []
    folder = target.is_dir()
    if asset_type not in ("roms", "bios") or (folder and asset_type != "roms"):
        raise ValueError("invalid inventory asset")
    for child in (target.rglob("*") if folder else [target]):
        if len(files) + len(directories) >= MAX_ENTRIES:
            raise ValueError("content inventory is too large")
        rel = safe_path(child.relative_to(target).as_posix()) if folder else relative_path
        # Do not sign content outside the selected folder, including symlink dirs.
        if child.is_symlink() or (folder and target not in child.resolve().parents):
            raise ValueError("inventory contains a symlink or escaped path")
        if child.is_dir():
            directories.append(rel)
        elif child.is_file():
            files.append({"relative_path": rel, **file_digest(child)})
        else:
            raise ValueError("inventory contains a non-regular file")
    payload = {"version": 1, "drone_id": settings.device_id, "asset_type": asset_type,
               "system": system, "relative_path": relative_path, "nonce": nonce,
               "entry_type": "folder" if folder else "file", "files": files,
               "directories": directories, "file_size": sum(f["file_size"] for f in files)}
    raw = canonical(payload)
    if len(raw) > MAX_INVENTORY_BYTES // 2:
        raise ValueError("content inventory is too large")
    signature = openssl(["dgst", "-sha256", "-sign", str(settings.drone_key_file)], raw)
    return {"inventory": payload, "signature": base64.b64encode(signature).decode("ascii")}


def verify_inventory(envelope, certificate, expected):
    try:
        payload = envelope["inventory"]
        signature = base64.b64decode(envelope["signature"], validate=True)
        if len(signature) > 16384 or not isinstance(payload, dict):
            raise ValueError("invalid inventory signature")
        with tempfile.TemporaryDirectory(prefix="drone-inventory-") as scratch:
            root = Path(scratch)
            cert = root / "identity.pem"
            cert.write_bytes(certificate)
            public = root / "public.pem"
            public.write_bytes(openssl(["x509", "-in", str(cert), "-pubkey", "-noout"], b""))
            sig = root / "signature"
            sig.write_bytes(signature)
            openssl(["dgst", "-sha256", "-verify", str(public), "-signature", str(sig)], canonical(payload))
        if any(payload.get(k) != v for k, v in expected.items()):
            raise ValueError("signed inventory does not match the requested asset")
        files, directories = payload["files"], payload["directories"]
        if not isinstance(files, list) or not isinstance(directories, list) or len(files) + len(directories) > MAX_ENTRIES:
            raise ValueError("invalid inventory entries")
        seen = set()
        for entry in files:
            rel = safe_path(entry["relative_path"])
            if rel in seen or type(entry["file_size"]) is not int or entry["file_size"] < 0 or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
                raise ValueError("invalid inventory file")
            seen.add(rel)
        for rel in directories:
            safe_path(rel)
            if rel in seen:
                raise ValueError("duplicate inventory entry")
            seen.add(rel)
        if payload["entry_type"] == "file" and (directories or len(files) != 1 or files[0]["relative_path"] != expected["relative_path"]):
            raise ValueError("invalid file inventory")
        if type(payload["file_size"]) is not int or payload["file_size"] != sum(f["file_size"] for f in files):
            raise ValueError("invalid inventory size")
        return payload
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("invalid signed inventory") from error


def fetch_inventory(settings, config, peer_id, address, asset_type, system, relative_path, entry_type):
    _require_swarm_networking()
    safe_path(relative_path)
    if system:
        safe_path(system)
        if "/" in system:
            raise ValueError("invalid inventory system")
    # Trust comes only from persisted pairing, never the caller or the envelope.
    paired = local_network.get_paired_peer(settings, peer_id) or {}
    pin = re.sub(r"[^0-9a-f]", "", str(paired.get("certificate_fingerprint") or "").lower())
    cafile = _peer_trust_cafile(settings, peer_id=peer_id, config=config)
    if not re.fullmatch(r"[0-9a-f]{64}", pin) or not cafile:
        raise ValueError("signed inventory requires a paired peer with a pinned certificate")
    certificate = cafile.read_bytes()
    der = ssl.PEM_cert_to_DER_cert(certificate.decode("ascii"))
    if not hmac.compare_digest(hashlib.sha256(der).hexdigest(), pin):
        raise ValueError("inventory certificate does not match the paired peer pin")
    nonce = secrets.token_hex(32)
    url = f"{address}/v1/api/peer/content-inventory/{asset_type}?" + urlencode({"system": system, "relative_path": relative_path, "nonce": nonce})
    context = _drone_client_ssl_context(settings, url, verify=True, cafile=cafile)
    _require_swarm_networking()
    with urlopen(Request(url, headers={"Accept": "application/json"}), context=context, timeout=120) as response:
        raw = response.read(MAX_INVENTORY_BYTES + 1)
    if len(raw) > MAX_INVENTORY_BYTES:
        raise ValueError("content inventory is too large")
    return verify_inventory(json.loads(raw), certificate, {"version": 1, "drone_id": peer_id,
        "asset_type": asset_type, "system": system, "relative_path": relative_path,
        "nonce": nonce, "entry_type": entry_type})


def verify_file(path, entry):
    try:
        if file_digest(path) != {"file_size": entry["file_size"], "sha256": entry["sha256"]}:
            raise ValueError("download does not match the pinned peer's signed inventory")
    except Exception:
        path.unlink(missing_ok=True)
        raise
