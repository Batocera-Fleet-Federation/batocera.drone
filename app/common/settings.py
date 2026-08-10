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
    movies_root: Path
    music_root: Path
    username: Optional[str]
    password: Optional[str]
    credentials_file: Path
    https_port: int
    compatibility_https_ports: Tuple[int, ...]
    advertised_api_port: int
    peer_mtls_port: int
    advertised_peer_mtls_port: int
    http_redirect_port: int
    cast_enabled: bool
    cast_http_port: int
    cast_transcode_enabled: bool
    cast_ffmpeg_bin: str
    cast_ffprobe_bin: str
    cast_cache_root: Path
    cast_hls_start_timeout_seconds: int

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
    tailscale_oauth_client_id: Optional[str]
    tailscale_oauth_client_secret: Optional[str]

    @classmethod
    def from_env(cls) -> "Settings":
        https_port_value = os.environ.get("HTTPS_PORT", os.environ.get("PORT", "443"))
        advertised_api_port_value = (
            os.environ.get("DRONE_ADVERTISED_API_PORT")
            or os.environ.get("DRONE_PUBLIC_API_PORT")
            or https_port_value
        )
        compatibility_https_ports = _parse_port_list(os.environ.get("DRONE_COMPAT_HTTPS_PORTS", "8443"))
        peer_mtls_port_value = os.environ.get("DRONE_PEER_MTLS_PORT", "8543")
        advertised_peer_mtls_port_value = (
            os.environ.get("DRONE_ADVERTISED_PEER_MTLS_PORT")
            or peer_mtls_port_value
        )
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
            movies_root=Path(os.environ.get("MOVIES_ROOT", "/userdata/movies")),
            music_root=Path(os.environ.get("MUSIC_ROOT", "/userdata/music")),
            username=os.environ.get("DRONE_APP_USERNAME") or None,
            password=os.environ.get("DRONE_APP_PASSWORD") or None,
            credentials_file=Path(os.environ.get("DRONE_CREDENTIALS_FILE", str(userdata_root / "system" / "drone-app" / "credentials.json"))),
            https_port=int(https_port_value),
            compatibility_https_ports=tuple(port for port in compatibility_https_ports if port != int(https_port_value)),
            advertised_api_port=int(advertised_api_port_value),
            peer_mtls_port=int(peer_mtls_port_value),
            advertised_peer_mtls_port=int(advertised_peer_mtls_port_value),
            # Plain-HTTP listener that only ever issues a 301 to the HTTPS
            # equivalent URL -- nothing else is served on it. Set to 0 to
            # disable it (matches the ROM_METADATA_POLL_SECONDS=0 convention
            # elsewhere for "off"), e.g. if port 80 is already in use for
            # something else on this machine.
            http_redirect_port=int(os.environ.get("DRONE_HTTP_REDIRECT_PORT", "80")),
            # On by default (opt-OUT via DRONE_CAST_ENABLED=0): a
            # Chromecast/AirPlay receiver fetches the movie file itself,
            # directly, with no browser and no session cookie -- it cannot
            # pass this Drone's self-signed HTTPS cert check either.
            # cast_http_port is a second, deliberately minimal plain-HTTP
            # listener (see drone_api.py's _CastHttpHandler) that serves
            # *only* a single-movie-token-gated stream surface, never anything
            # session-cookie-gated -- distinct from http_redirect_port,
            # which never serves real content at all. Same "on unless you
            # turn it off" default as http_redirect_port above; the token
            # gate (movie_cast_tokens.py) is what keeps this narrow rather
            # than needing to be opt-in the way a wide-open port would.
            cast_enabled=_env_bool(True, "DRONE_CAST_ENABLED"),
            cast_http_port=int(os.environ.get("DRONE_CAST_HTTP_PORT", "8095")),
            # Google Cast's default receiver only accepts a narrow set of
            # containers/codecs. Batocera includes ffmpeg/ffprobe, so movies
            # outside that set are exposed as an on-demand HLS compatibility
            # stream instead of connecting successfully and buffering forever.
            # This remains optional for unusually resource-constrained systems;
            # direct-compatible MP4/WebM casting still works when it is off.
            cast_transcode_enabled=_env_bool(True, "DRONE_CAST_TRANSCODE_ENABLED"),
            cast_ffmpeg_bin=os.environ.get("DRONE_CAST_FFMPEG_BIN", "ffmpeg"),
            cast_ffprobe_bin=os.environ.get("DRONE_CAST_FFPROBE_BIN", "ffprobe"),
            cast_cache_root=Path(
                os.environ.get(
                    "DRONE_CAST_CACHE_ROOT",
                    str(userdata_root / "system" / "drone-app" / "cast-cache"),
                )
            ),
            cast_hls_start_timeout_seconds=max(
                5, int(os.environ.get("DRONE_CAST_HLS_START_TIMEOUT_SECONDS", "30"))
            ),
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
            # Optional: a Tailscale OAuth client (admin console -> Settings -> OAuth
            # clients), ideally scoped to just `devices:core:write` and tagged to this
            # fleet, so an unattended Drone can disable its own tailnet key expiry
            # after enrolling instead of eventually stranding at NeedsLogin with no
            # one able to paste a fresh auth key. See device/tailnet_service.py.
            tailscale_oauth_client_id=(os.environ.get("DRONE_TAILSCALE_OAUTH_CLIENT_ID") or "").strip() or None,
            tailscale_oauth_client_secret=(os.environ.get("DRONE_TAILSCALE_OAUTH_CLIENT_SECRET") or "").strip() or None,
        )
