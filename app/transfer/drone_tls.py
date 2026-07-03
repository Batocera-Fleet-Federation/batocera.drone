"""Drone TLS certificate lifecycle: self-signed generation, rotation CSR, install, metadata.

Extracted from ``drone_api.py``. ``DroneCertificateManager`` owns the drone's mTLS
identity -- it ensures a self-signed cert exists (via ``openssl``), builds SANs from the
local IPs + hostname overrides, generates a rotation CSR, installs an Overmind-signed
cert, and decodes the current cert's metadata.
"""

import hashlib
import os
import re
import ssl
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from ..common.settings import Settings
    from .drone_network import _get_local_certificate_ips
    from .network_identity import (
        hostname_override_values as _hostname_override_values,
        is_ip_literal as _is_ip_literal,
    )
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from transfer.drone_network import _get_local_certificate_ips  # type: ignore
    from transfer.network_identity import (  # type: ignore
        hostname_override_values as _hostname_override_values,
        is_ip_literal as _is_ip_literal,
    )


class DroneCertificateManager:
    def __init__(self, settings: Settings):
        self.settings = settings

    def ensure_certificate(self) -> dict:
        cert_file = self.settings.drone_cert_file
        key_file = self.settings.drone_key_file
        if cert_file.exists() and key_file.exists():
            metadata = self.metadata()
            if metadata.get("status") == "loaded" and metadata.get("renewal_status") != "expired":
                return metadata
        if self.settings.drone_mtls_mode == "managed":
            return {
                "status": "invalid",
                "error": "managed Drone mTLS mode requires pre-provisioned, unexpired certificate and key files",
                "cert_file": str(cert_file),
                "key_file": str(key_file),
            }
        self._generate_local_certificate(cert_file, key_file)
        return self.metadata()

    def _generate_local_certificate(self, cert_file: Path, key_file: Path) -> None:
        cert_file.parent.mkdir(parents=True, exist_ok=True)
        identity = re.sub(r"[^A-Za-z0-9_.:-]+", "-", self.settings.overmind_device_id).strip("-") or "drone"
        common_name = f"batocera-drone-{identity}"
        alt_names = [
            f"DNS:{common_name}",
            "DNS:localhost",
            "IP:127.0.0.1",
        ]
        for override in _hostname_override_values(self.settings):
            if _is_ip_literal(override):
                alt_names.append(f"IP:{override.strip('[]')}")
            else:
                alt_names.append(f"DNS:{override}")
        for ip in _get_local_certificate_ips():
            alt_names.append(f"IP:{ip}")
        san = ",".join(dict.fromkeys(alt_names))
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
            str(max(1, int(self.settings.drone_cert_days))),
            "-subj",
            f"/CN={common_name}",
            "-addext",
            f"subjectAltName={san}",
            "-addext",
            "extendedKeyUsage=serverAuth,clientAuth",
        ]
        try:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (FileNotFoundError, subprocess.CalledProcessError) as error:
            raise RuntimeError(f"failed to generate Drone certificate with openssl: {error}") from error

    def generate_rotation_csr(self) -> dict:
        cert_file = self.settings.drone_cert_file
        key_file = self.settings.drone_key_file
        pending_key = key_file.with_suffix(key_file.suffix + ".pending")
        pending_csr = cert_file.with_suffix(cert_file.suffix + ".csr")
        cert_file.parent.mkdir(parents=True, exist_ok=True)
        identity = re.sub(r"[^A-Za-z0-9_.:-]+", "-", self.settings.overmind_device_id).strip("-") or "drone"
        common_name = f"batocera-drone-{identity}"
        alt_names = ["DNS:localhost", "IP:127.0.0.1"]
        for override in _hostname_override_values(self.settings):
            alt_names.append(f"IP:{override.strip('[]')}" if _is_ip_literal(override) else f"DNS:{override}")
        for ip in _get_local_certificate_ips():
            alt_names.append(f"IP:{ip}")
        command = [
            "openssl", "req", "-nodes", "-newkey", "rsa:2048",
            "-keyout", str(pending_key), "-out", str(pending_csr),
            "-subj", f"/CN={common_name}",
            "-addext", f"subjectAltName={','.join(dict.fromkeys(alt_names))}",
            "-addext", "extendedKeyUsage=serverAuth,clientAuth",
        ]
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.chmod(pending_key, 0o600)
        return {"csr_pem": pending_csr.read_text(encoding="utf-8"), "pending_key": pending_key, "pending_csr": pending_csr}

    def install_signed_certificate(self, certificate_pem: str, pending_key: Path, ca_certificate_pem: Optional[str] = None) -> dict:
        cert_file = self.settings.drone_cert_file
        key_file = self.settings.drone_key_file
        pending_cert = cert_file.with_suffix(cert_file.suffix + ".signed")
        pending_cert.write_text(certificate_pem, encoding="utf-8")
        ssl.PEM_cert_to_DER_cert(certificate_pem)
        if not pending_key.exists():
            raise RuntimeError("pending private key is missing")
        pending_key.replace(key_file)
        pending_cert.replace(cert_file)
        os.chmod(key_file, 0o600)
        os.chmod(cert_file, 0o644)
        if ca_certificate_pem:
            ca_file = cert_file.with_name("overmind-ca.crt")
            ca_file.write_text(ca_certificate_pem, encoding="utf-8")
            os.chmod(ca_file, 0o644)
        return self.metadata()

    def metadata(self) -> dict:
        cert_file = self.settings.drone_cert_file
        if not cert_file.exists():
            return {"status": "missing", "cert_file": str(cert_file)}
        try:
            pem = cert_file.read_text(encoding="utf-8", errors="ignore")
            der = ssl.PEM_cert_to_DER_cert(pem)
            decoded = ssl._ssl._test_decode_cert(str(cert_file))  # type: ignore[attr-defined]
        except Exception as error:
            return {"status": "invalid", "error": str(error), "cert_file": str(cert_file)}

        def _name(items) -> str:
            parts = []
            for group in items or []:
                for key, value in group:
                    parts.append(f"{key}={value}")
            return ", ".join(parts)

        san = []
        for kind, value in decoded.get("subjectAltName", ()):
            if kind.lower() == "dns":
                san.append(value)
        not_after = decoded.get("notAfter")
        renewal_status = "unknown"
        try:
            expires_at = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
            days_left = (expires_at - datetime.now(timezone.utc)).days
            renewal_status = "expired" if days_left < 0 else ("renew_soon" if days_left <= 30 else "valid")
        except Exception:
            days_left = None
        return {
            "status": "loaded",
            "source": "local_self_signed",
            "fingerprint": hashlib.sha256(der).hexdigest(),
            "public_certificate": pem,
            "subject": _name(decoded.get("subject")),
            "issuer": _name(decoded.get("issuer")),
            "serial_number": decoded.get("serialNumber"),
            "san": san,
            "valid_from": decoded.get("notBefore"),
            "valid_until": not_after,
            "days_until_expiry": days_left,
            "registered_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "last_seen": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "renewal_status": renewal_status,
            "identity": self.settings.overmind_device_id,
            "mtls_enabled": self.settings.drone_mtls_enabled,
            "mtls_mode": self.settings.drone_mtls_mode,
        }
