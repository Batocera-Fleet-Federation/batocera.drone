"""HTTP-server TLS material resolution + self-signed cert generation.

Extracted from ``drone_api.py``. ``_resolve_tls_material`` returns the (cert, key) paths to
bind the HTTPS server with (managed, provided, or self-signed via ``DroneCertificateManager``);
``_generate_self_signed_cert`` is the openssl fallback.
"""

import ssl
import subprocess
from pathlib import Path
from typing import Tuple

try:
    from ..common.settings import Settings
    from ..transfer.drone_tls import DroneCertificateManager
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from transfer.drone_tls import DroneCertificateManager  # type: ignore


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
    cert_file.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "openssl",
        "req",
        "-x509",
        "-nodes",
        "-newkey",
        "rsa:2048",
        "-keyout",
        str(key_file),
        "-out",
        str(cert_file),
        "-days",
        "3650",
        "-subj",
        "/CN=localhost",
    ]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _resolve_tls_material(settings: Settings) -> Tuple[Path, Path]:
    cert_file = settings.tls_cert_file
    key_file = settings.tls_key_file

    if cert_file and key_file:
        return cert_file, key_file

    if not settings.tls_self_signed:
        raise RuntimeError("TLS_CERT_FILE and TLS_KEY_FILE are required when TLS_SELF_SIGNED is disabled")

    cert_file = settings.tls_self_signed_dir / "server.crt"
    key_file = settings.tls_self_signed_dir / "server.key"

    if not cert_file.exists() or not key_file.exists():
        _generate_self_signed_cert(cert_file, key_file)

    return cert_file, key_file


# DroneCertificateManager (local self-signed cert lifecycle + rotation)
# now lives in transfer/drone_tls.py (re-exported below).


# drone<->peer connectivity (cert trust/pinning, peer HTTP client, health, pairing)
# now lives in transfer/peer_connectivity.py (re-exported below).
