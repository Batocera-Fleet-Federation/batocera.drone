"""HTTP-server TLS material resolution + locally-signed leaf generation.

Extracted from ``drone_api.py``. ``_resolve_tls_material`` returns the (cert, key) paths to
bind the HTTPS server with. Default installs keep the peer-pinned Drone identity
certificate stable as a local CA and reconcile a hostname/IP-correct server leaf
at startup. Changing DHCP or Tailscale addresses therefore never rotates swarm
identity.
"""

import os
import secrets
import ssl
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Tuple

try:
    from ..common.settings import Settings
    from ..transfer.drone_tls import DroneCertificateManager, certificate_alt_names, certificate_common_name
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from transfer.drone_tls import (  # type: ignore
        DroneCertificateManager,
        certificate_alt_names,
        certificate_common_name,
    )


def load_peer_cert_everywhere(server, cert_path: Path) -> None:
    """Load a newly-trusted peer certificate into every live TLS listener.

    Pairing/cert-trust events are handled by whichever one listener happened
    to receive that specific HTTP request, but the peer being trusted needs
    to be recognized on *every* listener -- most importantly the dedicated
    peer-mTLS listener, regardless of which listener the browser/admin action
    that triggered this actually hit. ``server.all_tls_servers`` (set by
    ``create_server``) is the same shared list object on every listener;
    falls back to just ``server`` itself if that attribute is absent (e.g. in
    tests that build a bare handler without a real multi-listener server).
    """
    for target in getattr(server, "all_tls_servers", None) or [server]:
        ssl_context = getattr(target, "ssl_context", None)
        if ssl_context is None:
            continue
        try:
            ssl_context.load_verify_locations(cafile=str(cert_path))
        except (ssl.SSLError, OSError):
            continue


def _generate_self_signed_cert(cert_file: Path, key_file: Path) -> None:
    """Legacy helper retained for import compatibility.

    Production startup uses ``_generate_server_leaf`` below so the stable
    Drone identity can sign a replaceable hostname-aware leaf.
    """
    cert_file.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048",
            "-keyout", str(key_file), "-out", str(cert_file), "-days", "3650",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _generate_server_leaf(
    settings: Settings,
    cert_file: Path,
    key_file: Path,
    ca_cert_file: Path,
    ca_key_file: Path,
    alt_names: Iterable[str],
) -> None:
    """Atomically issue a server leaf from the stable Drone identity CA."""
    cert_file.parent.mkdir(parents=True, exist_ok=True)
    san = ",".join(dict.fromkeys(alt_names))
    with tempfile.TemporaryDirectory(prefix="drone-server-tls-", dir=str(cert_file.parent)) as work:
        work_dir = Path(work)
        temp_key = work_dir / "server.key"
        request_file = work_dir / "server.csr"
        leaf_file = work_dir / "server-leaf.crt"
        chain_file = work_dir / "server.crt"
        extensions_file = work_dir / "server-ext.cnf"
        extensions_file.write_text(
            "[server_cert]\n"
            f"subjectAltName={san}\n"
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n",
            encoding="utf-8",
        )
        commands = (
            [
                "openssl", "req", "-new", "-nodes", "-newkey", "rsa:2048",
                "-keyout", str(temp_key), "-out", str(request_file),
                "-subj", f"/CN={certificate_common_name(settings)}",
            ],
            [
                "openssl", "x509", "-req", "-in", str(request_file),
                "-CA", str(ca_cert_file), "-CAkey", str(ca_key_file),
                "-set_serial", f"0x{secrets.token_hex(16)}",
                "-days", str(max(1, int(settings.drone_cert_days))), "-sha256",
                "-extfile", str(extensions_file), "-extensions", "server_cert",
                "-out", str(leaf_file),
            ],
        )
        for command in commands:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        chain_file.write_text(
            leaf_file.read_text(encoding="utf-8") + ca_cert_file.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        temp_key.chmod(0o600)
        chain_file.chmod(0o644)
        os.replace(temp_key, key_file)
        os.replace(chain_file, cert_file)


def _server_leaf_is_current(
    cert_file: Path,
    key_file: Path,
    ca_cert_file: Path,
    required_alt_names: Iterable[str],
) -> bool:
    if not cert_file.is_file() or not key_file.is_file() or not ca_cert_file.is_file():
        return False
    try:
        decoded = ssl._ssl._test_decode_cert(str(cert_file))  # type: ignore[attr-defined]
        present = {
            ("DNS:" if str(kind).lower() == "dns" else "IP:") + str(value)
            for kind, value in decoded.get("subjectAltName", ())
            if str(kind).lower() in {"dns", "ip address"}
        }
        if not set(required_alt_names).issubset(present):
            return False
        not_after = datetime.strptime(decoded["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        if (not_after - datetime.now(timezone.utc)).days <= 30:
            return False
        verification = subprocess.run(
            ["openssl", "verify", "-CAfile", str(ca_cert_file), str(cert_file)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if verification.returncode != 0:
            return False
        # Loading the pair is a cheap, portable way to verify that the private
        # key actually belongs to the leaf before the real listeners bind.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
        return True
    except (KeyError, OSError, TypeError, ValueError, ssl.SSLError):
        return False


def _resolve_tls_material(settings: Settings) -> Tuple[Path, Path]:
    cert_file = settings.tls_cert_file
    key_file = settings.tls_key_file

    if cert_file and key_file:
        return cert_file, key_file

    if not settings.tls_self_signed:
        raise RuntimeError("TLS_CERT_FILE and TLS_KEY_FILE are required when TLS_SELF_SIGNED is disabled")

    cert_file = settings.tls_self_signed_dir / "server.crt"
    key_file = settings.tls_self_signed_dir / "server.key"
    identity = DroneCertificateManager(settings).ensure_certificate()
    if identity.get("status") != "loaded":
        raise RuntimeError(str(identity.get("error") or "Drone identity certificate is unavailable"))

    required_alt_names = certificate_alt_names(settings)
    if not key_file.is_file() or not _server_leaf_is_current(
        cert_file,
        key_file,
        settings.drone_cert_file,
        required_alt_names,
    ):
        _generate_server_leaf(
            settings,
            cert_file,
            key_file,
            settings.drone_cert_file,
            settings.drone_key_file,
            required_alt_names,
        )

    return cert_file, key_file


# DroneCertificateManager (local self-signed cert lifecycle + rotation)
# now lives in transfer/drone_tls.py (re-exported below).


# drone<->peer connectivity (cert trust/pinning, peer HTTP client, health, pairing)
# now lives in transfer/peer_connectivity.py (re-exported below).
