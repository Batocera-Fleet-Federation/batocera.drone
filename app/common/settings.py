"""Runtime configuration for the Drone, loaded from environment variables.

Extracted from ``drone_api.py``. ``Settings.from_env()`` is the single place where
environment variables become a typed, frozen ``Settings`` object that is threaded
through the rest of the app. The small ``_require_env`` / ``_env_bool`` /
``_parse_port_list`` helpers live here too; per-device machine identity lives in
``device_identity.py``.

Pure stdlib aside from the device-identity helpers it imports.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

try:
    from .device_identity import _fake_machine_id, _machine_id, _normalize_device_id
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.device_identity import _fake_machine_id, _machine_id, _normalize_device_id  # type: ignore


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} environment variable must be set")
    return value


def _require_any_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    joined = " or ".join(names)
    raise RuntimeError(f"{joined} environment variable must be set")


def _env_bool(default: bool, *names: str) -> bool:
    for name in names:
        value = os.environ.get(name)
        if value is None:
            continue
        return value.strip().lower() not in ("0", "false", "no", "off")
    return default


def _parse_port_list(value: Optional[str]) -> Tuple[int, ...]:
    ports = []
    for raw in re.split(r"[,;\s]+", str(value or "")):
        raw = raw.strip()
        if not raw:
            continue
        try:
            port = int(raw)
        except ValueError:
            continue
        if 1 <= port <= 65535 and port not in ports:
            ports.append(port)
    return tuple(ports)


@dataclass(frozen=True)
class Settings:
    userdata_root: Path
    roms_root: Path
    bios_root: Path
    saves_root: Path
    username: Optional[str]
    password: Optional[str]
    credentials_file: Path
    https_port: int
    compatibility_https_ports: Tuple[int, ...]
    advertised_api_port: int

    image_cache_ttl_seconds: int
    image_miss_cache_ttl_seconds: int
    image_cache_max_items: int
    image_cache_max_bytes: int

    json_cache_ttl_seconds: int
    json_cache_max_items: int
    json_cache_max_bytes: int

    tls_cert_file: Optional[Path]
    tls_key_file: Optional[Path]
    tls_self_signed: bool
    tls_self_signed_dir: Path
    log_dir: Path
    stdout_log_file: str
    stderr_log_file: str
    activity_log_file: str
    log_max_bytes: int
    log_backup_count: int
    rom_search_cache_ttl_seconds: int
    downloads_enabled: bool
    admin_enabled: bool
    themes_root: Path
    batocera_conf_file: Path
    es_settings_file: Path
    es_systems_file: Path
    batocera_theme_name: Optional[str]
    http_only: bool
    use_fake_data: bool
    fake_image_base_url: Optional[str]
    device_id: str
    rom_metadata_poll_seconds: int
    hostname_override: Optional[str]
    public_ip_override: Optional[str]
    drone_cert_file: Path
    drone_key_file: Path
    drone_cert_days: int
    drone_mtls_enabled: bool
    drone_mtls_mode: str
    drone_mtls_ca_file: Optional[Path]

    @classmethod
    def from_env(cls) -> "Settings":
        https_port_value = os.environ.get("HTTPS_PORT", os.environ.get("PORT", "443"))
        advertised_api_port_value = (
            os.environ.get("DRONE_ADVERTISED_API_PORT")
            or os.environ.get("DRONE_PUBLIC_API_PORT")
            or https_port_value
        )
        compatibility_https_ports = _parse_port_list(os.environ.get("DRONE_COMPAT_HTTPS_PORTS", "8443"))
        cert_value = os.environ.get("TLS_CERT_FILE")
        key_value = os.environ.get("TLS_KEY_FILE")
        use_fake_data = _env_bool(False, "USE_FAKE_DATA")
        userdata_root = Path(os.environ.get("USERDATA_ROOT", "/userdata"))
        default_drone_cert = userdata_root / "system" / "drone-app" / "certs" / "drone.crt"
        default_drone_key = userdata_root / "system" / "drone-app" / "certs" / "drone.key"

        configured_device_id = _normalize_device_id(
            os.environ.get("DRONE_DEVICE_ID") or os.environ.get("OVERMIND_DEVICE_ID")
        )

        return cls(
            userdata_root=userdata_root,
            roms_root=Path(os.environ.get("ROMS_ROOT", "/userdata/roms")),
            bios_root=Path(os.environ.get("BIOS_ROOT", "/userdata/bios")),
            saves_root=Path(os.environ.get("SAVES_ROOT", "/userdata/saves")),
            username=os.environ.get("DRONE_APP_USERNAME") or None,
            password=os.environ.get("DRONE_APP_PASSWORD") or None,
            credentials_file=Path(os.environ.get("DRONE_CREDENTIALS_FILE", str(userdata_root / "system" / "drone-app" / "credentials.json"))),
            https_port=int(https_port_value),
            compatibility_https_ports=tuple(port for port in compatibility_https_ports if port != int(https_port_value)),
            advertised_api_port=int(advertised_api_port_value),
            image_cache_ttl_seconds=int(os.environ.get("IMAGE_CACHE_TTL_SECONDS", "3600")),
            image_miss_cache_ttl_seconds=int(os.environ.get("IMAGE_MISS_CACHE_TTL_SECONDS", "300")),
            image_cache_max_items=int(os.environ.get("IMAGE_CACHE_MAX_ITEMS", "1000")),
            image_cache_max_bytes=int(os.environ.get("IMAGE_CACHE_MAX_BYTES", str(256 * 1024 * 1024))),
            json_cache_ttl_seconds=int(os.environ.get("JSON_CACHE_TTL_SECONDS", "3600")),
            json_cache_max_items=int(os.environ.get("JSON_CACHE_MAX_ITEMS", "2000")),
            json_cache_max_bytes=int(os.environ.get("JSON_CACHE_MAX_BYTES", str(64 * 1024 * 1024))),
            tls_cert_file=Path(cert_value) if cert_value else None,
            tls_key_file=Path(key_value) if key_value else None,
            tls_self_signed=os.environ.get("TLS_SELF_SIGNED", "1") not in ("0", "false", "False"),
            tls_self_signed_dir=Path(os.environ.get("TLS_SELF_SIGNED_DIR", "/userdata/system/certs")),
            log_dir=Path(os.environ.get("LOG_DIR", "./logs")),
            stdout_log_file=os.environ.get("STDOUT_LOG_FILE", "stdout.log"),
            stderr_log_file=os.environ.get("STDERR_LOG_FILE", "stderr.log"),
            activity_log_file=os.environ.get("ACTIVITY_LOG_FILE", os.environ.get("OVERMIND_LOG_FILE", "drone.log")),
            log_max_bytes=int(os.environ.get("LOG_MAX_BYTES", str(5 * 1024 * 1024))),
            log_backup_count=int(os.environ.get("LOG_BACKUP_COUNT", "5")),
            rom_search_cache_ttl_seconds=int(os.environ.get("ROM_SEARCH_CACHE_TTL_SECONDS", "300")),
            downloads_enabled=_env_bool(True, "ALLOW_CONTENT_DOWNLOAD", "DOWNLOAD", "DOWNLOADS_ENABLED"),
            admin_enabled=_env_bool(True, "ALLOW_ADMIN"),
            themes_root=Path(os.environ.get("THEMES_ROOT", "/userdata/themes")),
            batocera_conf_file=Path(os.environ.get("BATOCERA_CONF_FILE", "/userdata/system/batocera.conf")),
            es_settings_file=Path(
                os.environ.get("ES_SETTINGS_FILE", "/userdata/system/configs/emulationstation/es_settings.cfg")
            ),
            es_systems_file=Path(
                os.environ.get("ES_SYSTEMS_FILE", "/usr/share/emulationstation/es_systems.cfg")
            ),
            batocera_theme_name=os.environ.get("BATOCERA_THEME_NAME"),
            http_only=_env_bool(False, "HTTP_ONLY", "DRONE_APP_HTTP_ONLY"),
            use_fake_data=use_fake_data,
            fake_image_base_url=os.environ.get("FAKE_IMAGE_BASE_URL"),
            device_id=configured_device_id or (_fake_machine_id() if use_fake_data else _machine_id(userdata_root)),
            rom_metadata_poll_seconds=max(0, int(os.environ.get("ROM_METADATA_POLL_SECONDS", "300"))),
            hostname_override=(os.environ.get("HOSTNAME_OVERRIDE") or "").strip() or None,
            public_ip_override=(os.environ.get("DRONE_PUBLIC_IP_OVERRIDE") or "").strip() or None,
            drone_cert_file=Path(os.environ.get("DRONE_CERT_FILE", os.environ.get("TLS_CERT_FILE", str(default_drone_cert)))),
            drone_key_file=Path(os.environ.get("DRONE_KEY_FILE", os.environ.get("TLS_KEY_FILE", str(default_drone_key)))),
            drone_cert_days=int(os.environ.get("DRONE_CERT_DAYS", "825")),
            drone_mtls_enabled=_env_bool(False, "DRONE_MTLS_ENABLED", "DRONE_TO_DRONE_MTLS_ENABLED"),
            drone_mtls_mode=(os.environ.get("DRONE_MTLS_MODE") or "self-signed").strip().lower(),
            drone_mtls_ca_file=Path(os.environ["DRONE_MTLS_CA_FILE"]) if os.environ.get("DRONE_MTLS_CA_FILE") else None,
        )
