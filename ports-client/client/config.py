"""Local connection settings for talking to the Drone running on this same device.

Deliberately does not import ``app.common.settings`` (see ports-client's code-
separation rule in the plan: this client only ever talks to Drone over HTTP,
never via a Python import, so it can't share a process/runtime with the
Drone service). Instead it re-derives the handful of env vars that determine
where Drone is listening and which cert it's serving, using the exact same
env var names and defaults as ``app/common/settings.py`` so a stock
install needs zero extra configuration.
"""

import os
from dataclasses import dataclass
from pathlib import Path

API_PREFIX = "/v1/api"


def _env_bool(default: bool, *names: str) -> bool:
    for name in names:
        raw = os.environ.get(name)
        if raw is not None:
            return raw.strip().lower() not in ("0", "false", "no", "off", "")
    return default


@dataclass(frozen=True)
class ClientConfig:
    host: str
    https_port: int
    http_only: bool
    ca_cert_file: Path
    session_cookie_path: Path

    @property
    def base_url(self) -> str:
        scheme = "http" if self.http_only else "https"
        return f"{scheme}://{self.host}:{self.https_port}{API_PREFIX}"

    @classmethod
    def from_env(cls) -> "ClientConfig":
        https_port = int(os.environ.get("HTTPS_PORT", os.environ.get("PORT", "443")))
        http_only = _env_bool(False, "HTTP_ONLY", "DRONE_APP_HTTP_ONLY")

        explicit_cert = os.environ.get("TLS_CERT_FILE")
        if explicit_cert:
            ca_cert_file = Path(explicit_cert)
        else:
            self_signed_dir = Path(os.environ.get("TLS_SELF_SIGNED_DIR", "/userdata/system/certs"))
            ca_cert_file = self_signed_dir / "server.crt"

        state_dir = Path(os.environ.get("USERDATA_ROOT", "/userdata")) / "system" / "drone-app"
        session_cookie_path = Path(
            os.environ.get("DRONE_PORTS_CLIENT_SESSION_FILE", str(state_dir / "ports-client-session.json"))
        )

        return cls(
            host=os.environ.get("DRONE_PORTS_CLIENT_HOST", "127.0.0.1"),
            https_port=https_port,
            http_only=http_only,
            ca_cert_file=ca_cert_file,
            session_cookie_path=session_cookie_path,
        )
