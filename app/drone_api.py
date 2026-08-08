import base64
import hmac
import html
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import uuid
import xml.etree.ElementTree as ET
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, RLock, Thread
from threading import Event
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import parse_qs
from urllib.parse import quote
from urllib.parse import unquote
from urllib.parse import urlparse

DRONE_REMOTE_REBOOT_EXIT_CODE = 76
APP_DIR = Path(__file__).resolve().parent

try:
    from .app_version import drone_app_version as _drone_app_version
    from .web.api_routes import ApiRoutesMixin
    from .set_screen_mode import set_screen_mode as _set_screen_mode_helper
    from .set_volume import set_audio_volume as _set_audio_volume_helper
    from .transfer.network_identity import (
        drone_network_payload as _build_drone_network_payload,
        drone_reachable_url as _build_drone_reachable_url,
        drone_report_host as _build_drone_report_host,
        drone_scheme as _drone_scheme,
        get_local_certificate_ips as _build_local_certificate_ips,
        get_local_ip_addresses as _build_local_ip_addresses,
        get_router_ip_address as _build_router_ip_address,
        hostname_override_values as _hostname_override_values,
        is_ip_literal as _is_ip_literal,
    )
    from .transfer import local_network as _local_network
    from .device.game_activity import commit_game_log_cursors as _commit_game_log_cursors
    from .device.game_activity import collect_game_logs as _build_game_log_payload
    from .device.game_activity import collect_game_event_sessions as _collect_game_event_sessions
    from .device.game_activity import delete_game_event_spool as _delete_game_event_spool
    from .device.game_activity import GameProcessMonitor
    from .device.game_activity import load_gameplay_history as _load_gameplay_history
    from .device.game_activity import pending_game_event_count as _pending_game_event_count
    from .device.emulator_configs import (
        list_emulator_config_files as _list_emulator_config_files,
        read_emulator_config_file as _read_emulator_config_file,
    )
    from .web.openapi_spec import build_openapi_spec
    from .web.route_config import API_PREFIX, api_url
    from .storage.rom_metadata_store import (
        ROM_METADATA_CACHE_VERSION,
        ArtworkCacheRow,
        search_rom_entries,
        rom_cache_has_entries,
        rom_cache_ready,
        list_rom_rows_by_system,
        _empty_rom_metadata_cache,
        _clear_pending_rom_metadata_changes,
        _clear_sqlite_asset_metadata_cache,
        _purge_asset_cache_keep_fingerprint,
        _read_preserved_asset_fingerprint,
        _load_rom_metadata_cache,
        _persist_rom_metadata_cache,
        _read_pending_rom_metadata_changes,
        _read_rom_metadata_cache_state,
        _read_sqlite_asset_systems,
        _rom_metadata_cache_path,
        _update_rom_metadata_cache_state,
    )
    from .roms.rom_fs_watcher import RomFilesystemWatcher
    from .storage import saves_store as _saves_store
    from .storage import movies_store as _movies_store
    from .storage import movie_cast_tokens as _movie_cast_tokens
    from .movies import cast_stream as _movie_cast_stream
    from .common import http_range as _http_range
    from .storage.state_store import (
        append_event as _append_state_event,
        database_path as _state_database_path,
        database_path_for_legacy_file as _state_database_path_for_legacy_file,
        load_payload as _load_state_payload,
        save_payload as _save_state_payload,
    )
    from .transfer.transfer_files import (
        bios_md5_exists as _bios_md5_exists,
        collision_safe_target as _collision_safe_target,
        rom_exists as _rom_exists,
        rom_fingerprint_exists as _rom_fingerprint_exists,
        safe_rom_relative_path as _safe_rom_relative_path,
    )
    from .transport import (
        DirectPublicTransport,
        DownloadRequest,
        TransferContext,
        TransportSelector,
    )
    from .transport.lan import LanDirectTransport
    from .web.ui_routes import UiRoutesMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from app_version import drone_app_version as _drone_app_version  # type: ignore
    from web.api_routes import ApiRoutesMixin  # type: ignore
    from set_screen_mode import set_screen_mode as _set_screen_mode_helper  # type: ignore
    from set_volume import set_audio_volume as _set_audio_volume_helper  # type: ignore
    from transfer.network_identity import (  # type: ignore
        drone_network_payload as _build_drone_network_payload,
        drone_reachable_url as _build_drone_reachable_url,
        drone_report_host as _build_drone_report_host,
        drone_scheme as _drone_scheme,
        get_local_certificate_ips as _build_local_certificate_ips,
        get_local_ip_addresses as _build_local_ip_addresses,
        get_router_ip_address as _build_router_ip_address,
        hostname_override_values as _hostname_override_values,
        is_ip_literal as _is_ip_literal,
    )
    from transfer import local_network as _local_network  # type: ignore
    from device.game_activity import commit_game_log_cursors as _commit_game_log_cursors  # type: ignore
    from device.game_activity import collect_game_logs as _build_game_log_payload  # type: ignore
    from device.game_activity import collect_game_event_sessions as _collect_game_event_sessions  # type: ignore
    from device.game_activity import delete_game_event_spool as _delete_game_event_spool  # type: ignore
    from device.game_activity import GameProcessMonitor  # type: ignore
    from device.game_activity import load_gameplay_history as _load_gameplay_history  # type: ignore
    from device.game_activity import pending_game_event_count as _pending_game_event_count  # type: ignore
    from device.emulator_configs import (  # type: ignore
        list_emulator_config_files as _list_emulator_config_files,
        read_emulator_config_file as _read_emulator_config_file,
    )
    from web.openapi_spec import build_openapi_spec  # type: ignore
    from web.route_config import API_PREFIX, api_url  # type: ignore
    from storage.rom_metadata_store import (  # type: ignore
        ROM_METADATA_CACHE_VERSION,
        ArtworkCacheRow,
        search_rom_entries,
        rom_cache_has_entries,
        rom_cache_ready,
        list_rom_rows_by_system,
        _empty_rom_metadata_cache,
        _clear_pending_rom_metadata_changes,
        _clear_sqlite_asset_metadata_cache,
        _purge_asset_cache_keep_fingerprint,
        _read_preserved_asset_fingerprint,
        _load_rom_metadata_cache,
        _persist_rom_metadata_cache,
        _read_pending_rom_metadata_changes,
        _read_rom_metadata_cache_state,
        _read_sqlite_asset_systems,
        _rom_metadata_cache_path,
        _update_rom_metadata_cache_state,
    )
    from roms.rom_fs_watcher import RomFilesystemWatcher  # type: ignore
    from storage import saves_store as _saves_store  # type: ignore
    from storage import movies_store as _movies_store  # type: ignore
    from storage import movie_cast_tokens as _movie_cast_tokens  # type: ignore
    from movies import cast_stream as _movie_cast_stream  # type: ignore
    from common import http_range as _http_range  # type: ignore
    from storage.state_store import (  # type: ignore
        append_event as _append_state_event,
        database_path as _state_database_path,
        database_path_for_legacy_file as _state_database_path_for_legacy_file,
        load_payload as _load_state_payload,
        save_payload as _save_state_payload,
    )
    from transfer.transfer_files import (  # type: ignore
        bios_md5_exists as _bios_md5_exists,
        collision_safe_target as _collision_safe_target,
        rom_exists as _rom_exists,
        rom_fingerprint_exists as _rom_fingerprint_exists,
        safe_rom_relative_path as _safe_rom_relative_path,
    )
    from transport import (  # type: ignore
        DirectPublicTransport,
        DownloadRequest,
        TransferContext,
        TransportSelector,
    )
    from transport.lan import LanDirectTransport  # type: ignore
    from web.ui_routes import UiRoutesMixin  # type: ignore

# --- Re-exports from modules extracted out of this file (refactor in progress).
# These names historically lived in drone_api.py; they now live in focused
# sibling modules and are imported back here so existing call sites and
# ``from app.drone_api import <name>`` keep working. See CLAUDE.md.
try:
    from .common.http_cache import (
        ExpiringKeyCache,
        ExpiringLRUCache,
        html_bytes,
        json_bytes,
        valid_segment,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from common.http_cache import (  # type: ignore
        ExpiringKeyCache,
        ExpiringLRUCache,
        html_bytes,
        json_bytes,
        valid_segment,
    )

try:
    from .common.fingerprint import (
        FINGERPRINT_ALGORITHM,
        FINGERPRINT_SAMPLE_BYTES,
        FINGERPRINT_SMALL_FILE_BYTES,
    )
    from .common.fingerprint import build_directory_stats as _fp_build_directory_stats
    from .common.fingerprint import build_fingerprint as _fp_build_fingerprint
    from .common.fingerprint import build_md5 as _fp_build_md5
    from .common.fingerprint import build_unique_id as _fp_build_unique_id
    from .common.logging_setup import (
        _TeeRotatingStream,
        _TimestampFormatter,
        _configure_rotating_logs,
        _drone_log,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from common.fingerprint import (  # type: ignore
        FINGERPRINT_ALGORITHM,
        FINGERPRINT_SAMPLE_BYTES,
        FINGERPRINT_SMALL_FILE_BYTES,
    )
    from common.fingerprint import build_directory_stats as _fp_build_directory_stats  # type: ignore
    from common.fingerprint import build_fingerprint as _fp_build_fingerprint  # type: ignore
    from common.fingerprint import build_md5 as _fp_build_md5  # type: ignore
    from common.fingerprint import build_unique_id as _fp_build_unique_id  # type: ignore
    from common.logging_setup import (  # type: ignore
        _TeeRotatingStream,
        _TimestampFormatter,
        _configure_rotating_logs,
        _drone_log,
    )

try:
    from .common.auth import (
        DRONE_AUTH_BLOCK_DURATION_SECONDS,
        DRONE_AUTH_BLOCK_ENABLED,
        DRONE_AUTH_BLOCK_THRESHOLD,
        DRONE_AUTH_BLOCK_WINDOW_SECONDS,
        DRONE_LOG_UNAUTHORIZED_REQUESTS,
        DRONE_UNAUTH_RATE_LIMIT_ENABLED,
        DRONE_UNAUTH_RATE_LIMIT_REQUESTS,
        DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS,
        DroneCredentialStore,
        SessionAuth,
        SessionStore,
        is_ip_blocked,
        record_unauthorized_response,
        _AUTH_401_BUCKETS,
        _AUTH_BLOCKED_IPS,
        _AUTH_BLOCK_LOCK,
        _UNAUTH_RATE_LIMIT_BUCKETS,
        _UNAUTH_RATE_LIMIT_LOCK,
        _auth_block_exempt_ip,
        _is_external_client_ip,
        _unauthenticated_request_allowed,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from common.auth import (  # type: ignore
        DRONE_AUTH_BLOCK_DURATION_SECONDS,
        DRONE_AUTH_BLOCK_ENABLED,
        DRONE_AUTH_BLOCK_THRESHOLD,
        DRONE_AUTH_BLOCK_WINDOW_SECONDS,
        DRONE_LOG_UNAUTHORIZED_REQUESTS,
        DRONE_UNAUTH_RATE_LIMIT_ENABLED,
        DRONE_UNAUTH_RATE_LIMIT_REQUESTS,
        DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS,
        DroneCredentialStore,
        SessionAuth,
        SessionStore,
        is_ip_blocked,
        record_unauthorized_response,
        _AUTH_401_BUCKETS,
        _AUTH_BLOCKED_IPS,
        _AUTH_BLOCK_LOCK,
        _UNAUTH_RATE_LIMIT_BUCKETS,
        _UNAUTH_RATE_LIMIT_LOCK,
        _auth_block_exempt_ip,
        _is_external_client_ip,
        _unauthenticated_request_allowed,
    )


try:
    from .common.settings import (
        Settings,
        _env_bool,
        _parse_port_list,
        _require_any_env,
        _require_env,
    )
    from .common.device_identity import (
        _DEVICE_ID_PATTERN,
        _PHYSICAL_INTERFACE_PRIORITIES,
        _VIRTUAL_INTERFACE_NAMES,
        _VIRTUAL_INTERFACE_PREFIXES,
        _device_id_path,
        _fake_machine_id,
        _interface_priority,
        _machine_id,
        _normalize_device_id,
        _physical_mac_candidates,
        _read_persisted_machine_id,
        _runtime_machine_id,
        _write_persisted_machine_id,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from common.settings import (  # type: ignore
        Settings,
        _env_bool,
        _parse_port_list,
        _require_any_env,
        _require_env,
    )
    from common.device_identity import (  # type: ignore
        _DEVICE_ID_PATTERN,
        _PHYSICAL_INTERFACE_PRIORITIES,
        _VIRTUAL_INTERFACE_NAMES,
        _VIRTUAL_INTERFACE_PREFIXES,
        _device_id_path,
        _fake_machine_id,
        _interface_priority,
        _machine_id,
        _normalize_device_id,
        _physical_mac_candidates,
        _read_persisted_machine_id,
        _runtime_machine_id,
        _write_persisted_machine_id,
    )


try:
    from .roms.scrapers import (
        LAUNCHBOX_API_BASE,
        LAUNCHBOX_FIELD_TYPES,
        LAUNCHBOX_IMAGE_BASE,
        LAUNCHBOX_PLATFORM_ALIASES,
        MOBYGAMES_PLATFORM_ALIASES,
        SCRAPER_USER_AGENT,
        LaunchBoxClient,
        MobyGamesClient,
        TheGamesDBScraper,
        _clean_rom_title,
        _launchbox_platform_for_system,
        _normalize_platform_key,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from roms.scrapers import (  # type: ignore
        LAUNCHBOX_API_BASE,
        LAUNCHBOX_FIELD_TYPES,
        LAUNCHBOX_IMAGE_BASE,
        LAUNCHBOX_PLATFORM_ALIASES,
        MOBYGAMES_PLATFORM_ALIASES,
        SCRAPER_USER_AGENT,
        LaunchBoxClient,
        MobyGamesClient,
        TheGamesDBScraper,
        _clean_rom_title,
        _launchbox_platform_for_system,
        _normalize_platform_key,
    )


try:
    from .device.device_control import (
        _apply_audio_volume,
        _apply_screen_mode,
        _emulationstation_restart_command,
        _emulator_kill_command,
        _ensure_rom_write_access,
        _get_audio_volume,
        _get_screen_mode,
        _kill_running_emulator,
        _parse_batocera_theme_name,
        _parse_es_systems_cfg,
        _parse_es_theme_name,
        _request_rom_permission_repair,
        _request_screen_mode_service_control,
        _request_service_control,
        _request_volume_service_control,
        _resolve_es_settings_file,
        _resolve_es_systems_effective,
        _resolve_theme_dir,
        _restart_emulationstation,
        _set_screen_mode,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from device.device_control import (  # type: ignore
        _apply_audio_volume,
        _apply_screen_mode,
        _emulationstation_restart_command,
        _emulator_kill_command,
        _ensure_rom_write_access,
        _get_audio_volume,
        _get_screen_mode,
        _kill_running_emulator,
        _parse_batocera_theme_name,
        _parse_es_systems_cfg,
        _parse_es_theme_name,
        _request_rom_permission_repair,
        _request_screen_mode_service_control,
        _request_service_control,
        _request_volume_service_control,
        _resolve_es_settings_file,
        _resolve_es_systems_effective,
        _resolve_theme_dir,
        _restart_emulationstation,
        _set_screen_mode,
    )


try:
    from .common.http_errors import _format_http_error
except ImportError:
    if __package__ not in (None, ""):
        raise
    from common.http_errors import _format_http_error  # type: ignore


try:
    from .device.system_metrics import (
        SPEED_TEST_DEFAULT_BASE_URL,
        _collect_gpu_info,
        _collect_mounted_disk_metrics,
        _collect_performance_metrics,
        _decode_mountinfo_path,
        _read_text_file,
        _sample_speed,
        _speed_test_raw_request,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from device.system_metrics import (  # type: ignore
        SPEED_TEST_DEFAULT_BASE_URL,
        _collect_gpu_info,
        _collect_mounted_disk_metrics,
        _collect_performance_metrics,
        _decode_mountinfo_path,
        _read_text_file,
        _sample_speed,
        _speed_test_raw_request,
    )


try:
    from .roms.gamelist import (
        _artwork_identity,
        _database_rom_metadata_fields,
        _find_gamelist_entry_by_game_id,
        _first_metadata_value,
        _gamelist_details,
        _gamelist_game_id,
        _gamelist_metadata_for_reference,
        _looks_like_placeholder_image,
        _normalize_gamelist_rom_path,
        _relative_artwork_path,
        _remove_child,
        _set_child_text,
        _text_or_empty,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from roms.gamelist import (  # type: ignore
        _artwork_identity,
        _database_rom_metadata_fields,
        _find_gamelist_entry_by_game_id,
        _first_metadata_value,
        _gamelist_details,
        _gamelist_game_id,
        _gamelist_metadata_for_reference,
        _looks_like_placeholder_image,
        _normalize_gamelist_rom_path,
        _relative_artwork_path,
        _remove_child,
        _set_child_text,
        _text_or_empty,
    )


try:
    from .common.self_update import (
        DRONE_LATEST_ARCHIVE_URL,
        DRONE_SELF_UPDATE_EXIT_CODE,
        _download_latest_drone_app,
        _drone_work_dir,
        _overlay_drone_release_tree,
        _restart_drone_process_soon,
        _start_drone_auto_update_poller,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from common.self_update import (  # type: ignore
        DRONE_LATEST_ARCHIVE_URL,
        DRONE_SELF_UPDATE_EXIT_CODE,
        _download_latest_drone_app,
        _drone_work_dir,
        _overlay_drone_release_tree,
        _restart_drone_process_soon,
        _start_drone_auto_update_poller,
    )


try:
    from .roms.rom_inventory import (
        BIOS_INVENTORY_FINGERPRINT_ALGORITHM,
        ROM_INVENTORY_FINGERPRINT_ALGORITHM,
        _artwork_cache_entry_key,
        _bios_cache_entry_key,
        _bios_inventory_fingerprint,
        _normalize_rom_inventory_path,
        _rom_cache_entry_key,
        _rom_inventory_fingerprint,
        _rom_inventory_fingerprint_from_cache_state,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from roms.rom_inventory import (  # type: ignore
        BIOS_INVENTORY_FINGERPRINT_ALGORITHM,
        ROM_INVENTORY_FINGERPRINT_ALGORITHM,
        _artwork_cache_entry_key,
        _bios_cache_entry_key,
        _bios_inventory_fingerprint,
        _normalize_rom_inventory_path,
        _rom_cache_entry_key,
        _rom_inventory_fingerprint,
        _rom_inventory_fingerprint_from_cache_state,
    )


try:
    from .transfer.drone_network import (
        _drone_advertised_api_port,
        _drone_network_payload,
        _drone_reachable_url,
        _drone_report_host,
        _get_local_certificate_ips,
        _get_local_ip_addresses,
        _get_router_ip_address,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from transfer.drone_network import (  # type: ignore
        _drone_advertised_api_port,
        _drone_network_payload,
        _drone_reachable_url,
        _drone_report_host,
        _get_local_certificate_ips,
        _get_local_ip_addresses,
        _get_router_ip_address,
    )


try:
    from .common.mock_userdata import (
        _looks_like_pure_mock_userdata,
        _mock_userdata_marker,
        _real_data_roots,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from common.mock_userdata import (  # type: ignore
        _looks_like_pure_mock_userdata,
        _mock_userdata_marker,
        _real_data_roots,
    )


try:
    from .transfer.drone_network import (
        _certificate_pem_fingerprint,
        _network_mode,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from transfer.drone_network import (  # type: ignore
        _certificate_pem_fingerprint,
        _network_mode,
    )


try:
    from .device.system_info import (
        _collect_system_info_payload,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from device.system_info import (  # type: ignore
        _collect_system_info_payload,
    )


try:
    from .web.server_tls import (
        _generate_self_signed_cert,
        _resolve_tls_material,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from web.server_tls import (  # type: ignore
        _generate_self_signed_cert,
        _resolve_tls_material,
    )


try:
    from .roms.rom_scanner import (
        _complete_local_rom_metadata_cache,
        _hash_rom_metadata_batches,
        _poll_rom_metadata_cache,
        _poll_rom_metadata_once,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from roms.rom_scanner import (  # type: ignore
        _complete_local_rom_metadata_cache,
        _hash_rom_metadata_batches,
        _poll_rom_metadata_cache,
        _poll_rom_metadata_once,
    )


try:
    from .roms.rom_metadata_state import (
        _begin_rom_metadata_activity,
        _build_rom_metadata_snapshot_from_cache,
        _end_rom_metadata_activity,
        _mark_rom_metadata_upload_clean,
        _rom_metadata_cache_status,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from roms.rom_metadata_state import (  # type: ignore
        _begin_rom_metadata_activity,
        _build_rom_metadata_snapshot_from_cache,
        _end_rom_metadata_activity,
        _mark_rom_metadata_upload_clean,
        _rom_metadata_cache_status,
    )


try:
    from .transfer.peer_workers import _start_local_network_workers
except ImportError:
    if __package__ not in (None, ""):
        raise
    from transfer.peer_workers import _start_local_network_workers  # type: ignore


try:
    from .transfer.download_manager import (
        DownloadManager,
        _directpublic_fetch,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from transfer.download_manager import (  # type: ignore
        DownloadManager,
        _directpublic_fetch,
    )


try:
    from .transfer.torrent_manager import TorrentManager
except ImportError:
    if __package__ not in (None, ""):
        raise
    from transfer.torrent_manager import TorrentManager  # type: ignore


try:
    from .device import vpn_manager as _vpn_manager
except ImportError:
    if __package__ not in (None, ""):
        raise
    from device import vpn_manager as _vpn_manager  # type: ignore


try:
    from .device import network_share_manager as _network_share_manager
except ImportError:
    if __package__ not in (None, ""):
        raise
    from device import network_share_manager as _network_share_manager  # type: ignore


try:
    from .device import nfs_export_manager as _nfs_export_manager
except ImportError:
    if __package__ not in (None, ""):
        raise
    from device import nfs_export_manager as _nfs_export_manager  # type: ignore


try:
    from .device import smtp_manager as _smtp_manager
except ImportError:
    if __package__ not in (None, ""):
        raise
    from device import smtp_manager as _smtp_manager  # type: ignore


try:
    from .transfer.peer_download import (
        _cached_rom_fingerprint_exists,
        _download_artwork_from_peer,
        _download_bios_from_peer,
        _download_rom_folder_from_peer,
        _download_rom_from_peer,
        _download_save_from_peer,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from transfer.peer_download import (  # type: ignore
        _cached_rom_fingerprint_exists,
        _download_artwork_from_peer,
        _download_bios_from_peer,
        _download_rom_folder_from_peer,
        _download_rom_from_peer,
        _download_save_from_peer,
    )


try:
    from .transfer.download_errors import DownloadCancelled
except ImportError:
    if __package__ not in (None, ""):
        raise
    from transfer.download_errors import DownloadCancelled  # type: ignore


try:
    from .transfer.drone_tls import DroneCertificateManager
except ImportError:
    if __package__ not in (None, ""):
        raise
    from transfer.drone_tls import DroneCertificateManager  # type: ignore


try:
    from .transfer.peer_connectivity import (
        _check_peer,
        _drone_client_ssl_context,
        _is_ssl_url_error,
        _local_pair_peer,
        _local_peer_cert_cache_path,
        _peer_address,
        _peer_api_port,
        _peer_get_json,
        _peer_health_url,
        _peer_ssl_diagnostic,
        _peer_trust_cafile,
        _public_local_peer,
        _save_local_peer_certificate,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from transfer.peer_connectivity import (  # type: ignore
        _check_peer,
        _drone_client_ssl_context,
        _is_ssl_url_error,
        _local_pair_peer,
        _local_peer_cert_cache_path,
        _peer_address,
        _peer_api_port,
        _peer_get_json,
        _peer_health_url,
        _peer_ssl_diagnostic,
        _peer_trust_cafile,
        _public_local_peer,
        _save_local_peer_certificate,
    )


try:
    from .common.logtail import (
        _read_file_tail,
        _tail_lines,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from common.logtail import (  # type: ignore
        _read_file_tail,
        _tail_lines,
    )


try:
    from .device.automation import (
        AUTOMATION_POLL_SECONDS,
        AUTOMATION_STATE_NAMESPACE,
        DEFAULT_IDLE_GAME_EXIT_MINUTES,
        DEFAULT_IDLE_VOLUME_MINUTES,
        DEFAULT_IDLE_VOLUME_TARGET,
        INPUT_ACTIVITY_FILENAME,
        _input_activity_file_path,
        _load_automation_config,
        _normalize_idle_game_exit_config,
        _normalize_idle_volume_config,
        _read_last_input_activity,
        _reset_idle_game_exit_armed_state,
        _reset_idle_volume_armed_state,
        _reset_wifi_recovery_check_state,
        _run_idle_game_exit_automation_once,
        _run_idle_volume_automation_once,
        _run_wifi_recovery_automation_once,
        _save_automation_config,
        _start_automation_poller,
        _wifi_recovery_status,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from device.automation import (  # type: ignore
        AUTOMATION_POLL_SECONDS,
        AUTOMATION_STATE_NAMESPACE,
        DEFAULT_IDLE_GAME_EXIT_MINUTES,
        DEFAULT_IDLE_VOLUME_MINUTES,
        DEFAULT_IDLE_VOLUME_TARGET,
        INPUT_ACTIVITY_FILENAME,
        _input_activity_file_path,
        _load_automation_config,
        _normalize_idle_game_exit_config,
        _normalize_idle_volume_config,
        _read_last_input_activity,
        _reset_idle_game_exit_armed_state,
        _reset_idle_volume_armed_state,
        _reset_wifi_recovery_check_state,
        _run_idle_game_exit_automation_once,
        _run_idle_volume_automation_once,
        _run_wifi_recovery_automation_once,
        _save_automation_config,
        _start_automation_poller,
        _wifi_recovery_status,
    )


_ROM_METADATA_POLLER_STARTED = False
_ROM_METADATA_WATCHER_STARTED = False
_ROM_METADATA_WATCHER = None
_SAVES_METADATA_WATCHER = None
_MOVIES_METADATA_WATCHER = None
# File-only rotating stream for the Drone's own narration log; configured in
# _configure_rotating_logs. _DRONE_ACTIVITY_LOG_STREAM now lives in logging_setup.py
_LOCAL_NETWORK_WORKERS_STARTED = False
_GAME_PROCESS_MONITOR_STARTED = False
_GAME_PROCESS_MONITOR = None
_AUTOMATION_POLLER_STARTED = False
# Last input-activity timestamp (from the privileged input monitor) for which the
# idle-volume automation already lowered the volume. Cleared on fresh input so the
# automation re-arms; keeps us from fighting a user who raises the volume manually.
# _IDLE_VOLUME_LAST_ARMED_ACTIVITY moved to device/automation.py.
# Shared mutable runtime singletons now live in common/runtime_state.py (re-exported):
try:
    from .common.runtime_state import (
        _GAMELIST_WRITE_LOCK,
        _ROM_METADATA_ACTIVE,
        _ROM_METADATA_LOCK,
        _ROM_METADATA_WAKE,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from common.runtime_state import (  # type: ignore
        _GAMELIST_WRITE_LOCK,
        _ROM_METADATA_ACTIVE,
        _ROM_METADATA_LOCK,
        _ROM_METADATA_WAKE,
    )
_DOWNLOAD_MANAGER = None
_TORRENT_MANAGER = None
_VPN_AUTO_CONNECT_ATTEMPTED = False
_VPN_SHARING_POLLER_STARTED = False
_VPN_SELF_HEAL_POLLER_STARTED = False
_NETWORK_SHARE_BOOT_REPLAY_ATTEMPTED = False
_NETWORK_SHARE_WATCHDOG_STARTED = False
_NFS_EXPORT_BOOT_REPLAY_ATTEMPTED = False
_SMTP_BOOTSTRAP_ATTEMPTED = False
_SMTP_SHARING_POLLER_STARTED = False
_AUDIT_EMAIL_POLLER_STARTED = False
# _PERFORMANCE_METRICS_LAST_SAMPLE moved to device/system_metrics.py.
# LAUNCHBOX_API_BASE / LAUNCHBOX_IMAGE_BASE / SCRAPER_USER_AGENT moved to scrapers.py.
try:  # ARTWORK_FIELDS now lives in roms/gamelist.py (re-exported for back-compat)
    from .roms.gamelist import ARTWORK_FIELDS
except ImportError:
    if __package__ not in (None, ""):
        raise
    from roms.gamelist import ARTWORK_FIELDS  # type: ignore

try:  # ARTWORK_DUPLICATE_FILTER now lives in roms/gamelist.py (re-exported for back-compat)
    from .roms.gamelist import ARTWORK_DUPLICATE_FILTER
except ImportError:
    if __package__ not in (None, ""):
        raise
    from roms.gamelist import ARTWORK_DUPLICATE_FILTER  # type: ignore
# DOWNLOAD_TERMINAL_STATUSES now lives in transfer/download_manager.py (its only user).
# DOWNLOAD_PROGRESS_PUSH_SECONDS now lives in transfer/download_manager.py (its only user).
PEER_CHECK_TIMEOUT_SECONDS = float(os.environ.get("DRONE_PEER_CHECK_TIMEOUT_SECONDS", "3"))
# Browsing/copying a peer's inventory can scan a large library to build a page,
# which far exceeds the quick health-check timeout. Give inventory reads a much
# longer budget so big libraries don't surface as "read operation timed out".
PEER_INVENTORY_TIMEOUT_SECONDS = float(os.environ.get("DRONE_PEER_INVENTORY_TIMEOUT_SECONDS", "120"))
# SPEED_TEST_DEFAULT_BASE_URL moved to device/system_metrics.py (re-exported above).
ROM_METADATA_POLL_SECONDS = int(os.environ.get("ROM_METADATA_POLL_SECONDS", "300"))
ROM_METADATA_INITIAL_DELAY_SECONDS = int(os.environ.get("ROM_METADATA_INITIAL_DELAY_SECONDS", "60"))
ROM_METADATA_PROGRESS_SECONDS = float(os.environ.get("ROM_METADATA_PROGRESS_SECONDS", "30"))
ROM_METADATA_PROGRESS_FILES = int(os.environ.get("ROM_METADATA_PROGRESS_FILES", "250"))
ROM_METADATA_FINGERPRINT_BATCH_SIZE = max(1, int(os.environ.get("ROM_METADATA_FINGERPRINT_BATCH_SIZE", "250")))
# Cross-drone fingerprint constants + build_* helpers now live in fingerprint.py
# (FINGERPRINT_ALGORITHM / *_SAMPLE_BYTES / *_SMALL_FILE_BYTES, re-exported above).
# Wall-clock budget for fingerprinting within a single poll. Fingerprinting is cheap
# (constant I/O per file) and resumable, so this is a safety guard that rarely trips.
ROM_METADATA_HASH_BUDGET_SECONDS = max(0.0, float(os.environ.get("ROM_METADATA_HASH_BUDGET_SECONDS", "120")))
ROM_METADATA_HASH_ROMS_ENABLED = os.environ.get("ROM_METADATA_HASH_ROMS_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
# Real-time inotify watcher that wakes the metadata poller when ROM files change.
ROM_METADATA_WATCH_ENABLED = os.environ.get("ROM_METADATA_WATCH_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
# Coalesce a burst of filesystem events: wait for this much quiet before waking
# the poller, but never delay longer than the max even during a long bulk copy.
ROM_METADATA_WATCH_DEBOUNCE_SECONDS = max(0.5, float(os.environ.get("ROM_METADATA_WATCH_DEBOUNCE_SECONDS", "10")))
ROM_METADATA_WATCH_MAX_DELAY_SECONDS = max(
    ROM_METADATA_WATCH_DEBOUNCE_SECONDS,
    float(os.environ.get("ROM_METADATA_WATCH_MAX_DELAY_SECONDS", "60")),
)
# Auth + rate-limit constants (DRONE_AUTH_BLOCK_* / DRONE_UNAUTH_RATE_LIMIT_* /
# DRONE_LOG_UNAUTHORIZED_REQUESTS) now live in auth.py (re-exported above).
# LAUNCHBOX_PLATFORM_ALIASES + LAUNCHBOX_FIELD_TYPES moved to scrapers.py.

# Env-parsing helpers (_require_env/_require_any_env/_env_bool/_parse_port_list)
# now live in settings.py; the machine-id cluster (_machine_id/_fake_machine_id/
# _physical_mac_candidates/...) now lives in device_identity.py. Both re-exported above.


# _clean_rom_title / _normalize_platform_key / _launchbox_platform_for_system moved to
# scrapers.py (re-exported above; _clean_rom_title is also used by the ROM scanner).


# Gamelist XML / ROM-metadata-field helpers (_gamelist_details, _text_or_empty,
# _database_rom_metadata_fields, _find_gamelist_entry_by_game_id, ...) now live in
# roms/gamelist.py (re-exported above).


# _read_file_tail + _tail_lines now live in common/logtail.py (re-exported above).


# LaunchBoxClient / TheGamesDBScraper / MobyGamesClient (+ MOBYGAMES_PLATFORM_ALIASES)
# now live in scrapers.py (re-exported near the top of this module).


# Settings (the frozen env-loaded config dataclass) now lives in settings.py
# (re-exported near the top of this module).


# Logging primitives (_TimestampFormatter, _TeeRotatingStream,
# _configure_rotating_logs, _drone_log) and the _DRONE_ACTIVITY_LOG_STREAM global
# now live in logging_setup.py (re-exported near the top of this module).


# DroneCredentialStore, SessionAuth/SessionStore, the 401 brute-force blocker
# and the unauthenticated-request rate limiter now live in auth.py (re-exported
# near the top of this module).


# ExpiringLRUCache / ExpiringKeyCache / json_bytes / html_bytes / valid_segment
# now live in http_cache.py (re-exported near the top of this module).


# Device-control helpers (theme/screen/volume/service-control group 1) moved to
# device_control.py (re-exported above).


# Idle-volume automation + input-activity tracking now lives in device/automation.py
# (re-exported above).


try:
    from .roms.rom_artwork_apply import RomArtworkApplyMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from roms.rom_artwork_apply import RomArtworkApplyMixin  # type: ignore


try:
    from .roms.rom_artwork_gamelist import RomArtworkGamelistMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from roms.rom_artwork_gamelist import RomArtworkGamelistMixin  # type: ignore


try:
    from .roms.rom_scan import RomScanMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from roms.rom_scan import RomScanMixin  # type: ignore


try:
    from .roms.rom_systems import RomSystemsSearchMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from roms.rom_systems import RomSystemsSearchMixin  # type: ignore


try:
    from .roms.rom_asset_bios import RomAssetBiosMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from roms.rom_asset_bios import RomAssetBiosMixin  # type: ignore


try:
    from .roms.rom_duplicates import RomDuplicatesMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from roms.rom_duplicates import RomDuplicatesMixin  # type: ignore


class RomRepository(RomAssetBiosMixin, RomSystemsSearchMixin, RomScanMixin, RomArtworkGamelistMixin, RomArtworkApplyMixin, RomDuplicatesMixin):
    def __init__(self, roms_root: Path, bios_root: Path, rom_search_cache_ttl_seconds: int = 300, settings=None):
        self.roms_root = roms_root
        self.bios_root = bios_root
        # Settings are required to read the relational SQLite cache. When absent
        # (e.g. unit tests constructing a bare repository) the cache-backed paths
        # transparently fall back to scanning the filesystem.
        self.settings = settings
        self.rom_search_cache_ttl_seconds = rom_search_cache_ttl_seconds
        self._search_cache_lock = Lock()
        self._search_index: List[dict] = []
        self._search_index_expires_at = 0.0
        self._missing_artwork_cache_lock = Lock()
        self._missing_artwork_cache: Dict[str, dict] = {}

    @staticmethod
    def should_include_system(name: str) -> bool:
        lowered = str(name or "").strip().lower()
        return bool(lowered) and not (lowered.endswith(".old") or ".old." in lowered)

    @staticmethod
    def build_unique_id(path: Path) -> str:
        return _fp_build_unique_id(path)

    @staticmethod
    def build_fingerprint(path: Path) -> str:
        """Sampled cross-drone content fingerprint (``sample-fp-v1``).

        Implementation lives in ``fingerprint.build_fingerprint``; kept as a
        static method so existing ``RomRepository.build_fingerprint`` call sites
        and the tests that patch it keep working.
        """
        return _fp_build_fingerprint(path)

    @staticmethod
    def build_md5(path: Path) -> str:
        """Full-file MD5 for BIOS identity (delegates to ``fingerprint.build_md5``)."""
        return _fp_build_md5(path)

    @staticmethod
    def build_directory_stats(path: Path) -> Tuple[int, int]:
        return _fp_build_directory_stats(path)

    @staticmethod
    def should_ignore_rom_file(file_name: str, system: Optional[str] = None) -> bool:
        lower = str(file_name or "").strip().lower()
        if lower.startswith(".") or lower in {"_info.txt", "gamelist.xml", ".keep", ".gitkeep", "readme.md"}:
            return True
        if lower.endswith(".sh.keys"):
            return True
        ignored_extensions = {
            ".xml", ".txt", ".md", ".nfo", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
            ".mp4", ".mkv", ".avi", ".mov", ".pdf", ".cue", ".m3u", ".json", ".db",
        }
        if lower.endswith(tuple(ignored_extensions)):
            return True
        return False

    @staticmethod
    def should_ignore_rom_path(path: Path) -> bool:
        ignored_dirs = {
            "images", "videos", "manuals", "media", "downloaded_images", "covers",
            "boxart", "fanart", "marquee", "thumbs", "screenshots",
        }
        if any(part.startswith(".") or part.lower() in ignored_dirs for part in path.parts):
            return True
        return RomRepository.should_ignore_rom_file(path.name)

    @staticmethod
    def iter_files(path: Path) -> Iterable[Path]:
        if not path.exists() or not path.is_dir():
            return []
        return [entry for entry in sorted(path.iterdir(), key=lambda p: p.name.lower()) if entry.is_file()]

    # ROM filesystem-listing methods now live in the RomScanMixin
    # (roms/rom_scan.py), composed onto RomRepository.

    # system-listing + search + gamelist-read methods now live in the
    # RomSystemsSearchMixin (roms/rom_systems.py), composed onto RomRepository.

    # asset + BIOS listing methods now live in the RomAssetBiosMixin
    # (roms/rom_asset_bios.py), composed onto RomRepository.


OPENAPI_SPEC = build_openapi_spec(_drone_app_version(), API_PREFIX)

# ==================== Decoupled service functions ====================
# Module-level, settings-parameterized versions of handler logic so the same implementation
# backs both the legacy stdlib handler methods and the FastAPI routes (app/api_app.py). Kept in
# this module to reuse the existing helpers without an import cycle.

try:
    from .web.handlers_peer import HandlersPeerMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from web.handlers_peer import HandlersPeerMixin  # type: ignore


try:
    from .web.handlers_network_share import HandlersNetworkShareMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from web.handlers_network_share import HandlersNetworkShareMixin  # type: ignore


try:
    from .web.handlers_content import HandlersContentMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from web.handlers_content import HandlersContentMixin  # type: ignore


try:
    from .web.handlers_artwork import HandlersArtworkMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from web.handlers_artwork import HandlersArtworkMixin  # type: ignore


try:
    from .web.handlers_network import HandlersNetworkMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from web.handlers_network import HandlersNetworkMixin  # type: ignore


try:
    from .web.handlers_config import HandlersConfigMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from web.handlers_config import HandlersConfigMixin  # type: ignore


try:
    from .web.handlers_diagnostics import HandlersDiagnosticsMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from web.handlers_diagnostics import HandlersDiagnosticsMixin  # type: ignore


try:
    from .web.handlers_downloads import HandlersDownloadsMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from web.handlers_downloads import HandlersDownloadsMixin  # type: ignore


try:
    from .web.handlers_torrents import HandlersTorrentsMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from web.handlers_torrents import HandlersTorrentsMixin  # type: ignore


try:
    from .web.handlers_vpn import HandlersVpnMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from web.handlers_vpn import HandlersVpnMixin  # type: ignore


try:
    from .web.handlers_config_backup import HandlersConfigBackupMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from web.handlers_config_backup import HandlersConfigBackupMixin  # type: ignore


try:
    from .web.handlers_smtp import HandlersSmtpMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from web.handlers_smtp import HandlersSmtpMixin  # type: ignore


try:
    from .web.handlers_notifications import HandlersNotificationsMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from web.handlers_notifications import HandlersNotificationsMixin  # type: ignore


try:
    from .web.handlers_system import HandlersSystemMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from web.handlers_system import HandlersSystemMixin  # type: ignore


try:
    from .web.handlers_theme import ThemeMetaMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from web.handlers_theme import ThemeMetaMixin  # type: ignore


try:
    from .web.handlers_es_collections import HandlersEsCollectionsMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from web.handlers_es_collections import HandlersEsCollectionsMixin  # type: ignore


try:
    from .web.handlers_auth import HandlersAuthMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from web.handlers_auth import HandlersAuthMixin  # type: ignore


try:
    from .web.handlers_movies import HandlersMoviesMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from web.handlers_movies import HandlersMoviesMixin  # type: ignore


class RomRequestHandler(HandlersAuthMixin, HandlersSystemMixin, HandlersDownloadsMixin, HandlersTorrentsMixin, HandlersVpnMixin, HandlersConfigBackupMixin, HandlersSmtpMixin, HandlersNotificationsMixin, HandlersDiagnosticsMixin, HandlersConfigMixin, HandlersNetworkMixin, HandlersArtworkMixin, HandlersContentMixin, HandlersMoviesMixin, ThemeMetaMixin, HandlersEsCollectionsMixin, HandlersPeerMixin, HandlersNetworkShareMixin, ApiRoutesMixin, UiRoutesMixin, BaseHTTPRequestHandler):
    server_version = "DroneApp/4.0"
    openapi_spec = OPENAPI_SPEC
    # Per-connection idle timeout (applied to the socket in BaseHTTPRequestHandler.setup).
    # The TLS handshake is now deferred to this worker thread (do_handshake_on_connect=False),
    # so this bounds both the handshake and per-request reads/writes: a stalled or silent
    # client is dropped instead of holding a thread forever. It is a per-operation idle
    # timeout, not a total-transfer cap, so large peer ROM transfers with flowing data are
    # unaffected. Overridable via env for slow networks.
    timeout = max(15, int(os.environ.get("DRONE_REQUEST_TIMEOUT_SECONDS", "120")))

    def __init__(
        self,
        *args,
        settings: Settings,
        auth: SessionAuth,
        repository: RomRepository,
        image_cache: ExpiringLRUCache,
        image_miss_cache: ExpiringKeyCache,
        json_cache: ExpiringLRUCache,
        **kwargs,
    ):
        self.settings = settings
        self.auth = auth
        self.repository = repository
        self.image_cache = image_cache
        self.image_miss_cache = image_miss_cache
        self.json_cache = json_cache
        super().__init__(*args, **kwargs)

    def log_request(self, code="-", size="-") -> None:
        client_ip = self.client_address[0] if self.client_address else "-"
        message = f'{client_ip} - "{self.requestline}" {code} {size}'
        print(message, file=sys.stdout, flush=True)

    def log_error(self, format: str, *args) -> None:
        message = format % args if args else format
        client_ip = self.client_address[0] if self.client_address else "-"
        print(f"{client_ip} - {message}", file=sys.stderr, flush=True)

    def _guess_content_type(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".js":
            return "application/javascript"
        if suffix == ".css":
            return "text/css"
        if suffix == ".svg":
            return "image/svg+xml"
        if suffix == ".png":
            return "image/png"
        if suffix in (".jpg", ".jpeg"):
            return "image/jpeg"
        if suffix == ".webp":
            return "image/webp"
        if suffix == ".gif":
            return "image/gif"
        if suffix == ".woff":
            return "font/woff"
        if suffix == ".woff2":
            return "font/woff2"
        if suffix == ".ttf":
            return "font/ttf"
        if suffix == ".otf":
            return "font/otf"
        if suffix == ".mp4":
            return "video/mp4"
        if suffix == ".webm":
            return "video/webm"
        if suffix == ".mkv":
            return "video/x-matroska"
        if suffix in (".mov", ".qt"):
            return "video/quicktime"
        if suffix == ".avi":
            return "video/x-msvideo"
        return "application/octet-stream"

    def _send_unauthorized(self) -> None:
        has_cookie = bool(self.headers.get("Cookie"))
        if DRONE_LOG_UNAUTHORIZED_REQUESTS or has_cookie:
            self.log_error(
                '401 unauthorized "%s" cookie_present=%s',
                self.path.split("?", 1)[0],
                "yes" if has_cookie else "no",
            )
        self.send_response(401)
        # Deliberately NOT "WWW-Authenticate: Basic" -- that header is what makes
        # a browser pop its own native credential dialog, exactly the invasive
        # UX this session-cookie login replaces. X-Drone-Auth-Required is a
        # same-origin-only marker the SPA's own fetch() handling checks for --
        # see drone.js's _handleApiUnauthorized.
        self.send_header("X-Drone-Auth-Required", "1")
        self.send_header("Content-Type", "application/json")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(json_bytes({"error": "unauthorized"}))
        client_ip = self.client_address[0] if self.client_address else "-"
        record_unauthorized_response(client_ip)

    def _reject_if_ip_blocked(self) -> bool:
        """Reject (403) and log every request from an IP blocked for 401 brute force."""
        client_ip = self.client_address[0] if self.client_address else "-"
        if not is_ip_blocked(client_ip):
            return False
        print(
            f"Blocked request: ip={client_ip} {self.command} {self.path.split('?', 1)[0]}",
            file=sys.stdout,
            flush=True,
        )
        try:
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", str(int(DRONE_AUTH_BLOCK_DURATION_SECONDS)))
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(json_bytes({"error": "blocked"}))
        except Exception:
            pass
        return True

    def _send_rate_limited(self) -> None:
        self.log_error('429 rate limited "%s"', self.path.split("?", 1)[0])
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        self.send_header("Retry-After", str(int(DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(json_bytes({"error": "rate_limited"}))

    def _rate_limit_unauthenticated_external_request(self) -> bool:
        if self.auth.authenticate_request(self.headers) is not None:
            return False
        try:
            cert = self.connection.getpeercert() if hasattr(self.connection, "getpeercert") else None
        except Exception:
            cert = None
        if cert:
            return False
        client_ip = self.client_address[0] if self.client_address else "-"
        if _unauthenticated_request_allowed(client_ip):
            return False
        self._send_rate_limited()
        return True

    def _send_security_headers(self, cache_control: str = "no-store") -> None:
        # cache_control defaults to "no-store" for every JSON/HTML response
        # (the right default when most of what this app serves is
        # session-gated and changes often). A handful of cacheable-by-nature
        # responses (scraped artwork, ROM images -- see
        # HandlersPeerMixin._stream_cached_image) pass a real Cache-Control
        # value instead; sending both here and a second Cache-Control header
        # from the caller would produce two header lines that most clients
        # concatenate into one comma-joined value, and "no-store" wins that
        # combination regardless of what else is in it -- silently defeating
        # the caching the caller asked for. One call site, one header.
        image_sources = ["'self'", "data:", "https:"]
        if self.settings.use_fake_data:
            image_sources.append("https:")
            fake_base = (self.settings.fake_image_base_url or "").strip()
            if fake_base:
                parsed = urlparse(fake_base)
                if parsed.scheme and parsed.netloc:
                    image_sources.append(f"{parsed.scheme}://{parsed.netloc}")
                elif fake_base.startswith("https://") or fake_base.startswith("http://"):
                    image_sources.append(fake_base.rstrip("/"))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        self.send_header("Cache-Control", cache_control)
        # CSP keeps UI/resource loading strict while still allowing bundled Swagger assets
        # and (script-src/connect-src's www.gstatic.com) the Google Cast Sender SDK the
        # movie player modal's Chromecast button loads -- see drone.js's loadCastSenderSdk.
        # Harmless to always allow even when casting is disabled (settings.cast_enabled):
        # the SDK is only ever fetched client-side when the movie player modal opens.
        self.send_header(
            "Content-Security-Policy",
            f"default-src 'self'; img-src {' '.join(image_sources)}; style-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net https://www.gstatic.com; "
            "font-src 'self' data: https://cdn.jsdelivr.net https://fonts.gstatic.com; connect-src 'self' https://unpkg.com https://cdn.jsdelivr.net https://www.gstatic.com; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )

    def _build_fake_image_url(self, seed: str, width: int = 640, height: int = 360) -> str:
        template = (self.settings.fake_image_base_url or "https://picsum.photos/seed/{seed}/{width}/{height}").strip()
        safe_seed = re.sub(r"[^a-zA-Z0-9._-]+", "-", seed).strip("-") or "image"
        if "{" in template and "}" in template:
            return template.format(seed=quote(safe_seed, safe=""), width=width, height=height)
        base = template.rstrip("/")
        return f"{base}/{quote(safe_seed, safe='')}/{width}/{height}"

    def _redirect_to_fake_image(self, seed: str, width: int = 640, height: int = 360) -> None:
        location = self._build_fake_image_url(seed=seed, width=width, height=height)
        self.send_response(302)
        self.send_header("Location", location)
        self._send_security_headers()
        self.end_headers()

    def _fake_theme_asset_url(self, relative_path: str) -> str:
        lowered = relative_path.lower()
        if lowered.endswith(".svg"):
            return self._build_fake_image_url(seed=f"theme-{relative_path}", width=800, height=450)
        if lowered.endswith(".png"):
            return self._build_fake_image_url(seed=f"theme-{relative_path}", width=800, height=450)
        if lowered.endswith(".jpg") or lowered.endswith(".jpeg") or lowered.endswith(".webp") or lowered.endswith(".gif"):
            return self._build_fake_image_url(seed=f"theme-{relative_path}", width=800, height=450)
        return api_url(f"/theme/assets/{quote(relative_path, safe='/')}")

    def _send_json(self, status_code: int, payload: dict, cache_key: Optional[str] = None, extra_headers: Optional[Dict[str, str]] = None) -> None:
        if status_code == 200 and cache_key:
            cached = self.json_cache.get(cache_key)
            if cached is None:
                body = json_bytes(payload)
                self.json_cache.put(cache_key, body)
            else:
                body = cached["data"]
        else:
            body = json_bytes(payload)

        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        if status_code == 200 and cache_key:
            self.send_header("Cache-Control", "private, max-age=3600")
        for header_name, header_value in (extra_headers or {}).items():
            self.send_header(header_name, header_value)
        self.end_headers()
        self.wfile.write(body)

    # HandlersDownloadsMixin methods now live in web/handlers_downloads.py (composed onto RomRequestHandler).

    def _send_html(self, status_code: int, html: str) -> None:
        body = html_bytes(html)
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status_code: int) -> None:
        self.send_response(status_code)
        self.send_header("Content-Length", "0")
        self._send_security_headers()
        self.end_headers()

    def _handle_content_file(self, relative_path: str) -> None:
        content_root = Path(__file__).resolve().parent.parent / "content"
        rel = str(relative_path or "").replace("\\", "/").lstrip("/")
        if not rel or ".." in Path(rel).parts:
            raise FileNotFoundError()
        target = (content_root / rel).resolve()
        if content_root.resolve() not in target.parents or not target.exists() or not target.is_file():
            raise FileNotFoundError()
        self._stream_file(target, self._guess_content_type(target))

    def _read_json_body(self) -> dict:
        length_value = self.headers.get("Content-Length", "0").strip()
        try:
            length = int(length_value or "0")
        except Exception:
            raise ValueError("invalid content length")
        if length < 0 or length > (256 * 1024):
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValueError("invalid JSON body")
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def _rom_fingerprint_cache_path(self) -> Path:
        return (self.settings.userdata_root / "system" / "drone-app" / "rom_fingerprint_cache.json").resolve()

    def _load_json_file(self, path: Path, fallback):
        return _load_state_payload(
            _state_database_path(self.settings.userdata_root),
            path.name,
            fallback,
            legacy_path=path,
        )

    def _save_json_state(self, path: Path, payload) -> None:
        _save_state_payload(
            _state_database_path(self.settings.userdata_root),
            path.name,
            payload,
        )
        path.unlink(missing_ok=True)

    # HandlersSystemMixin methods now live in web/handlers_system.py (composed onto RomRequestHandler).

    def _handle_public_health(self) -> None:
        self._send_json(
            200,
            {
                "status": "ok",
                "drone_id": self.settings.device_id,
                "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            },
        )

    def _handle_rom_fingerprint(self, system: str, unique_id: str) -> None:
        system_dir = self.repository.get_system_dir(system)
        rom = self.repository.find_rom_by_unique_id(system, unique_id)
        rom_path = str(rom.get("relative_path") or rom.get("rom_path") or rom.get("rom_file") or rom.get("name") or "")
        target = (system_dir / rom_path).resolve()
        if not target.exists() or not target.is_file() or (target != system_dir and system_dir not in target.parents):
            raise FileNotFoundError()
        stat = target.stat()
        cache_path = self._rom_fingerprint_cache_path()
        cache = self._load_json_file(cache_path, {})
        key = f"{system}:{unique_id}:{stat.st_size}:{int(stat.st_mtime)}"
        fingerprint_value = cache.get(key) if isinstance(cache, dict) else None
        if not fingerprint_value:
            fingerprint_value = self.repository.build_fingerprint(target)
            cache = {key: fingerprint_value}
        self._save_json_state(cache_path, cache)
        self._send_json(200, {"system": system, "unique_id": unique_id, "fingerprint": fingerprint_value, "cached": bool(cache.get(key))})

    def _peer_request_authorized(self) -> bool:
        if self.settings.http_only:
            if _env_bool(False, "DRONE_LOCAL_ALLOW_INSECURE_HTTP"):
                return True
            self._send_json(403, {"error": "local-network peer API requires HTTPS and a paired client certificate"})
            return False
        try:
            der = self.connection.getpeercert(binary_form=True) if hasattr(self.connection, "getpeercert") else None
        except Exception:
            der = None
        fingerprint = hashlib.sha256(der).hexdigest() if der else ""
        trusted = {
            str(peer.get("certificate_fingerprint") or "").strip().lower()
            for peer in _local_network.paired_peers(self.settings)
        }
        if fingerprint and fingerprint.lower() in trusted:
            return True
        self._send_json(403, {"error": "paired client certificate required"})
        return False

    # peer-serving handlers (/peer/* mTLS endpoints) now live in the HandlersPeerMixin
    # (web/handlers_peer.py), composed onto RomRequestHandler.

    def _handle_search(self, query: str, system: Optional[str] = None) -> None:
        query = query.strip()
        if not query:
            self._send_json(400, {"error": "missing query parameter q"})
            return
        system_filter = system.strip() if system else None
        if system_filter:
            system_filter = valid_segment(system_filter)
        results = self.repository.search_roms(query, system_filter=system_filter)
        if not self.settings.downloads_enabled:
            for item in results:
                item["is_downloadable"] = False
        cache_key = f"json:/search?q={query.lower()}&system={(system_filter or '').lower()}"
        self._send_json(200, {"query": query, "system": system_filter, "results": results}, cache_key=cache_key)

    # _build_theme_meta now lives in web/handlers_theme.py (ThemeMetaMixin, composed onto RomRequestHandler).

    # HandlersContentMixin methods now live in web/handlers_content.py (composed onto RomRequestHandler).

    # HandlersArtworkMixin methods now live in web/handlers_artwork.py (composed onto RomRequestHandler).

    # HandlersDiagnosticsMixin methods now live in web/handlers_diagnostics.py (composed onto RomRequestHandler).

def _build_handler(
    settings: Settings,
    auth: SessionAuth,
    repository: RomRepository,
    image_cache: ExpiringLRUCache,
    image_miss_cache: ExpiringKeyCache,
    json_cache: ExpiringLRUCache,
):
    def factory(*args, **kwargs):
        return RomRequestHandler(
            *args,
            settings=settings,
            auth=auth,
            repository=repository,
            image_cache=image_cache,
            image_miss_cache=image_miss_cache,
            json_cache=json_cache,
            **kwargs,
        )

    return factory


# _generate_self_signed_cert, _resolve_tls_material now live in web/server_tls.py (re-exported below).


# _collect_system_info_payload now live in device/system_info.py (re-exported below).


def _resolve_userdata_path(settings: Settings, candidate: str) -> Path:
    if candidate == "/userdata":
        return settings.userdata_root.resolve()
    if candidate.startswith("/userdata/"):
        return (settings.userdata_root / candidate[len("/userdata/") :]).resolve()
    return Path(candidate).resolve()


def _kick_asset_metadata_sync_after_download(settings: Settings, repository: "RomRepository", config: dict, reason: str) -> None:
    """Wake the local ROM-metadata poller so a just-downloaded asset is picked up
    without waiting out the full poll interval."""
    _ROM_METADATA_WAKE.set()


# DownloadManager (queue + transport-tier dispatch) + _directpublic_fetch now live
# in transfer/download_manager.py (re-exported below). The running singleton
# (_DOWNLOAD_MANAGER / _get_download_manager) stays here.


def _get_download_manager() -> Optional["DownloadManager"]:
    return _DOWNLOAD_MANAGER


def _get_torrent_manager() -> Optional["TorrentManager"]:
    return _TORRENT_MANAGER


def _resolve_asset_root(settings: Settings, kind: str) -> Optional[Path]:
    """Map an asset kind to the local root directory it lives under."""
    kind = str(kind or "").strip().lower()
    if kind == "rom":
        return settings.roms_root
    if kind == "bios":
        return settings.bios_root
    if kind in ("save", "saves"):
        return settings.saves_root
    return None


# Direct-peer asset downloads (_download_*_from_peer) now live in
# transfer/peer_download.py (re-exported below).


def _start_rom_metadata_poller(settings: Settings, repository: "RomRepository") -> None:
    poll_seconds = max(30, int(settings.rom_metadata_poll_seconds or ROM_METADATA_POLL_SECONDS))
    initial_delay_seconds = max(
        0,
        int(os.environ.get("ROM_METADATA_INITIAL_DELAY_SECONDS", str(ROM_METADATA_INITIAL_DELAY_SECONDS))),
    )
    print(
        f"Asset metadata poller starting: poll_seconds={poll_seconds} initial_delay_seconds={initial_delay_seconds}",
        file=sys.stdout,
        flush=True,
    )

    def loop() -> None:
        if initial_delay_seconds:
            print(
                f"Asset metadata poll delayed at startup: seconds={initial_delay_seconds}",
                file=sys.stdout,
                flush=True,
            )
            if _ROM_METADATA_WAKE.wait(initial_delay_seconds):
                _ROM_METADATA_WAKE.clear()
        while True:
            poll_started = time.monotonic()
            try:
                _poll_rom_metadata_once(settings, repository)
            except (HTTPError, URLError) as error:
                status_part = f" status={error.code}" if isinstance(error, HTTPError) else ""
                print(
                    f"ROM metadata sync failed:{status_part} error={_format_http_error(error)} duration_ms={int((time.monotonic() - poll_started) * 1000)}",
                    file=sys.stderr,
                    flush=True,
                )
            except Exception as error:
                print(
                    f"ROM metadata sync failed: error={_format_http_error(error)} duration_ms={int((time.monotonic() - poll_started) * 1000)}",
                    file=sys.stderr,
                    flush=True,
                )
            if _ROM_METADATA_WAKE.wait(poll_seconds):
                _ROM_METADATA_WAKE.clear()

    thread = Thread(target=loop, name="rom-metadata-poller", daemon=True)
    thread.start()
    print("Asset metadata poller thread started", file=sys.stdout, flush=True)


def _start_rom_metadata_watcher(settings: Settings) -> None:
    """Wake the metadata poller in near real time when ROM, saves, or movie
    files change.

    Best-effort: if inotify is unavailable the periodic poll still covers
    changes, so a failure here is logged and otherwise ignored.
    """
    global _ROM_METADATA_WATCHER, _SAVES_METADATA_WATCHER, _MOVIES_METADATA_WATCHER
    watcher = RomFilesystemWatcher(
        settings.roms_root,
        _ROM_METADATA_WAKE.set,
        debounce_seconds=ROM_METADATA_WATCH_DEBOUNCE_SECONDS,
        max_delay_seconds=ROM_METADATA_WATCH_MAX_DELAY_SECONDS,
    )
    if watcher.start():
        _ROM_METADATA_WATCHER = watcher
    # Watch the saves tree too so a created/updated/deleted save wakes the poller in
    # near real time; the periodic poll still covers it if inotify is unavailable.
    saves_watcher = RomFilesystemWatcher(
        settings.saves_root,
        _ROM_METADATA_WAKE.set,
        debounce_seconds=ROM_METADATA_WATCH_DEBOUNCE_SECONDS,
        max_delay_seconds=ROM_METADATA_WATCH_MAX_DELAY_SECONDS,
    )
    if saves_watcher.start():
        _SAVES_METADATA_WATCHER = saves_watcher
    # And the movies tree -- previously the only asset type with no watcher at
    # all, so a new/moved movie sat invisible until the next periodic poll
    # (up to rom_metadata_poll_seconds later, no way to force it sooner short
    # of the ROM-oriented "Purge Cache & Resync" button). Same near-real-time
    # wake as ROMs/saves now.
    movies_watcher = RomFilesystemWatcher(
        settings.movies_root,
        _ROM_METADATA_WAKE.set,
        debounce_seconds=ROM_METADATA_WATCH_DEBOUNCE_SECONDS,
        max_delay_seconds=ROM_METADATA_WATCH_MAX_DELAY_SECONDS,
    )
    if movies_watcher.start():
        _MOVIES_METADATA_WATCHER = movies_watcher


def _ensure_game_event_spool(settings: Settings) -> None:
    """Prepare the durable process-monitor event spool and remove the legacy hook."""
    target = (settings.userdata_root / "system" / "scripts" / "drone-game-event.sh").resolve()
    spool = (settings.userdata_root / "system" / "drone-app" / "game-events").resolve()
    try:
        spool.mkdir(parents=True, exist_ok=True)
        try:
            spool.chmod(0o2775)
        except OSError:
            pass
        if target.exists():
            target.unlink()
            print(f"Legacy gameplay event hook removed: {target}", file=sys.stdout, flush=True)
    except OSError as error:
        print(f"Gameplay event spool setup skipped: {_format_http_error(error)}", file=sys.stderr, flush=True)


class DroneThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    # Per-IP throttle so a chatty unpaired peer (or scanner) can't flood the log
    # with one identical line per connection attempt.
    _drop_log_lock = Lock()
    _drop_log_last: Dict[str, float] = {}
    _DROP_LOG_INTERVAL_SECONDS = 60.0

    def handle_error(self, request, client_address):
        # The public HTTPS port is constantly probed by internet scanners sending
        # non-TLS or malformed payloads, which surface as SSL/connection errors during
        # request handling. socketserver's default dumps a full traceback for each,
        # spamming stderr. Log a single concise line for these benign cases instead;
        # fall back to the noisy traceback only for genuinely unexpected errors.
        error = sys.exc_info()[1]
        if isinstance(error, (ssl.SSLError, ConnectionError, BrokenPipeError, TimeoutError, OSError)):
            ip = client_address[0] if isinstance(client_address, (tuple, list)) and client_address else client_address
            now = time.monotonic()
            cls = DroneThreadingHTTPServer
            with cls._drop_log_lock:
                last = cls._drop_log_last.get(str(ip))
                if last is not None and now - last < cls._DROP_LOG_INTERVAL_SECONDS:
                    return
                cls._drop_log_last[str(ip)] = now
            hint = ""
            reason = str(error).lower()
            if "certificate" in reason and not _is_external_client_ip(str(ip)):
                # On a LAN this is almost always another Drone that this one has not
                # paired with (or that is not running HTTPS) trying to transfer.
                hint = " — this looks like a Drone on your network that is not paired with this one (or is not running HTTPS). Pair it under Admin > Integration > Local Network. (repeats from this IP are suppressed for 60s)"
            print(
                f"Dropped untrusted/insecure connection from {ip}: {error.__class__.__name__}: {error}{hint}",
                file=sys.stderr,
                flush=True,
            )
            return
        super().handle_error(request, client_address)


def _apply_server_tls(settings: Settings, server: ThreadingHTTPServer, *, peer_mtls: bool = False) -> None:
    """Build and attach this listener's TLS context.

    ``peer_mtls=True`` is for the one dedicated peer-to-peer listener: it asks
    for (``CERT_OPTIONAL``) and trusts paired-peer client certificates, same as
    this whole server used to do on every listener. Every other listener
    (browser/admin-facing, including the compatibility ports) uses
    ``peer_mtls=False``: ``CERT_NONE``, so browsers are never sent a
    ``CertificateRequest`` and never see a client-certificate picker. Splitting
    this by listener (rather than by request path) is required because the TLS
    handshake completes before any HTTP path is visible.
    """
    if settings.http_only:
        return
    if settings.drone_mtls_mode == "managed" and not (settings.drone_cert_file.exists() and settings.drone_key_file.exists()):
        raise RuntimeError("managed Drone mTLS mode requires DRONE_CERT_FILE and DRONE_KEY_FILE")
    if settings.drone_cert_file.exists() and settings.drone_key_file.exists():
        cert_file, key_file = settings.drone_cert_file, settings.drone_key_file
    else:
        cert_file, key_file = _resolve_tls_material(settings)
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    if peer_mtls:
        ssl_context.verify_mode = ssl.CERT_OPTIONAL
        if settings.drone_mtls_ca_file and settings.drone_mtls_ca_file.is_file():
            ssl_context.load_verify_locations(cafile=str(settings.drone_mtls_ca_file))
        for peer in _local_network.paired_peers(settings):
            raw_cert_path = str(peer.get("certificate_path") or "").strip()
            # is_file() (not exists()) is deliberate: an empty/missing certificate_path
            # collapses to Path("") == Path("."), which *exists* as a directory, and a
            # directory (or empty) path makes load_verify_locations raise IsADirectoryError
            # — an OSError the ssl.SSLError handler below does NOT catch, crashing startup.
            if not raw_cert_path:
                continue
            cert_path = Path(raw_cert_path)
            if cert_path.is_file():
                try:
                    ssl_context.load_verify_locations(cafile=str(cert_path))
                except (ssl.SSLError, OSError):
                    continue
        # Belt-and-suspenders: also trust every cert in the local-peer-certs store
        # so a paired peer stays trusted across restarts even if its record's
        # certificate_path drifts or is missing. Pairing also injects new certs
        # into this live context (see _handle_peer_pair), so post-startup pairings
        # work without a restart too.
        local_certs_dir = _local_peer_cert_cache_path(settings, "x").parent
        if local_certs_dir.exists():
            for cert_file_path in sorted(local_certs_dir.glob("*.crt")):
                if not cert_file_path.is_file():
                    continue
                try:
                    ssl_context.load_verify_locations(cafile=str(cert_file_path))
                except (ssl.SSLError, OSError):
                    continue
    else:
        ssl_context.verify_mode = ssl.CERT_NONE
    server.ssl_context = ssl_context  # type: ignore[attr-defined]
    # do_handshake_on_connect=False is critical: wrapping the LISTENING socket otherwise
    # makes accept() perform the TLS handshake on the single serve_forever thread, so one
    # silent client (e.g. an internet scanner that opens 443 and never speaks) blocks
    # accept() forever and wedges the whole server. Deferring the handshake lets accept()
    # return immediately; the handshake then runs in the per-request worker thread under
    # RomRequestHandler.timeout, where a stall costs only that one thread.
    server.socket = ssl_context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)


def _redirect_location(https_port: int, host_header: str, client_ip: str, path: str) -> str:
    """Build the ``Location`` header for the plain-HTTP redirect listener.

    Uses whatever host the client actually sent in its own ``Host`` header
    (so ``batocera.local``, a raw LAN IP, a Tailscale name, etc. all keep
    working unchanged) rather than any hostname this Drone thinks of itself
    -- falls back to the observed client IP only when the request carried no
    ``Host`` header at all. The port is suffixed only when it isn't the
    implicit default (443), so the common case produces a plain
    ``https://host/path`` URL.
    """
    hostname = host_header.split(":", 1)[0] if host_header else (client_ip or "")
    port_suffix = "" if https_port == 443 else f":{https_port}"
    return f"https://{hostname}{port_suffix}{path}"


class _HttpRedirectHandler(BaseHTTPRequestHandler):
    """The entire plain-HTTP listener (``settings.http_redirect_port``, default
    80): every request gets a 301 to the same host/path over HTTPS, nothing
    else is ever served here. A deliberately separate, minimal class -- not
    ``RomRequestHandler`` -- so the real app surface is never reachable over
    an unencrypted connection even by accident.
    """

    def __init__(self, *args, settings: Settings, **kwargs):
        self.settings = settings
        super().__init__(*args, **kwargs)

    def _send_redirect(self) -> None:
        client_ip = self.client_address[0] if self.client_address else ""
        location = _redirect_location(self.settings.https_port, self.headers.get("Host", ""), client_ip, self.path)
        self.send_response(301)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._send_redirect()

    do_HEAD = do_GET
    do_POST = do_GET
    do_PUT = do_GET
    do_DELETE = do_GET
    do_PATCH = do_GET
    do_OPTIONS = do_GET

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        # Silenced deliberately: port 80 is one of the most commonly scanned
        # ports on the internet and every real hit here is a one-line
        # redirect with nothing to diagnose -- logging each one would flood
        # stderr.log with scanner noise, the same reasoning behind
        # DroneThreadingHTTPServer.handle_error's per-IP throttle on the TLS
        # listeners below.
        pass


def _build_http_redirect_handler(settings: Settings):
    def factory(*args, **kwargs):
        return _HttpRedirectHandler(*args, settings=settings, **kwargs)

    return factory


# Video suffixes this app scans as movies (see storage/movies_store.py's
# _VIDEO_SUFFIXES) mapped to a Content-Type -- duplicated here, not imported,
# on purpose: this handful of lines is the entire content-type surface
# _CastHttpHandler needs, and keeping it self-contained (rather than reaching
# for RomRequestHandler._guess_content_type, which knows about two dozen
# unrelated file types this listener will never serve) keeps this
# security-sensitive class easy to read start to finish in one place.
_CAST_VIDEO_CONTENT_TYPES = {
    ".mp4": "video/mp4", ".mkv": "video/x-matroska", ".webm": "video/webm",
    ".mov": "video/quicktime", ".qt": "video/quicktime", ".avi": "video/x-msvideo",
    ".m4v": "video/x-m4v", ".wmv": "video/x-ms-wmv", ".flv": "video/x-flv",
    ".mpg": "video/mpeg", ".mpeg": "video/mpeg", ".m2ts": "video/mp2t",
    ".ts": "video/mp2t", ".3gp": "video/3gpp",
}


class _CastHttpHandler(BaseHTTPRequestHandler):
    """A second, deliberately separate plain-HTTP listener
    (``settings.cast_http_port``, bound whenever ``settings.cast_enabled``
    is true -- on by default, see that field's docstring in
    ``common/settings.py``) that serves *exactly one* movie-stream surface,
    gated by a single-movie-scoped token from
    ``storage/movie_cast_tokens.py``: a Range-aware direct-file route plus
    the playlist/segments of an FFmpeg compatibility stream when the source
    container or codecs cannot play on the receiver. Every other path (and
    there is no admin surface, no movie list, no artwork, nothing else at all
    here) gets a bare 404.

    This exists because a Chromecast or AirPlay receiver fetches the video
    file itself, directly -- no browser, no session cookie, and it can't
    click through this Drone's self-signed HTTPS certificate either. A
    deliberately separate, minimal class -- not ``RomRequestHandler`` --
    same reasoning as ``_HttpRedirectHandler`` above: the real app surface
    (session-gated browsing, admin actions, every other file this Drone
    holds) must never become reachable over an unencrypted connection just
    because this one narrow exception exists for it. The narrowness (a
    single narrow surface, gated by a token only an already-authenticated request
    could have minted) is what makes "on by default" an acceptable default
    here, the same way ``_HttpRedirectHandler`` (which serves no real
    content at all) already is.
    """

    # HTTP/1.1, unlike every other listener in this app (which all inherit
    # BaseHTTPRequestHandler's HTTP/1.0 default and are fine there, because
    # browsers cope with it). A Chromecast's media player does not: on an
    # HTTP/1.0 progressive stream it commonly buffers forever without ever
    # starting playback -- the exact "TV shows a permanent loading spinner"
    # symptom this was reported with, once the URL itself was reachable.
    # Safe to raise here specifically because every response this handler
    # can produce carries an accurate Content-Length (200/206 the real body
    # length, 404/204 an explicit 0), which is what makes keep-alive framing
    # unambiguous; do NOT copy this to a handler that streams without one.
    protocol_version = "HTTP/1.1"
    server_version = "DroneAppCast/1.0"
    # Same per-connection idle-timeout reasoning as RomRequestHandler.timeout
    # -- with keep-alive now in play, a silent receiver would otherwise hold
    # a thread indefinitely waiting for a next request that never comes.
    timeout = max(15, int(os.environ.get("DRONE_REQUEST_TIMEOUT_SECONDS", "120")))

    def __init__(self, *args, settings: Settings, **kwargs):
        self.settings = settings
        # Guards against emitting a second response into a connection whose
        # body is already partly written (a mid-stream failure). Harmless
        # under HTTP/1.0, where the close ended the message anyway; under
        # keep-alive it would corrupt the *next* response on the socket.
        self._response_started = False
        super().__init__(*args, **kwargs)

    def _send_404(self) -> None:
        if self._response_started:
            self.close_connection = True
            return
        try:
            self._response_started = True
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_GET(self) -> None:
        self._response_started = False
        try:
            raw_path, _, raw_query = self.path.partition("?")
            parts = [part for part in raw_path.split("/") if part]
            direct_stream = (
                len(parts) == 4
                and parts[0] == "public"
                and parts[1] == "movies"
                and parts[3] == "cast-stream"
            )
            hls_asset = (
                len(parts) == 6
                and parts[0] == "public"
                and parts[1] == "movies"
                and parts[3] == "cast-hls"
            )
            airplay_page = (
                len(parts) == 4
                and parts[0] == "public"
                and parts[1] == "movies"
                and parts[3] == "airplay"
            )
            if not direct_stream and not hls_asset and not airplay_page:
                self._send_404()
                return
            entry_key = parts[2]
            query = parse_qs(raw_query)
            token = parts[4] if hls_asset else query.get("token", [""])[0]
            if not _movie_cast_tokens.verify(self.settings, entry_key, token):
                # Worth a log line: this is what a receiver hits when a token
                # has expired, and it is otherwise indistinguishable (from
                # the TV's side) from the movie simply not playing.
                self._log_cast(f"404 rejected token for {entry_key}")
                self._send_404()
                return
            if airplay_page:
                self._send_airplay_page(entry_key, token, query.get("delivery", [""])[0])
            elif hls_asset:
                try:
                    target, content_type = _movie_cast_stream.resolve_hls_asset(
                        self.settings, entry_key, token, parts[5]
                    )
                except FileNotFoundError:
                    self._log_cast(f"404 unknown HLS asset for {entry_key}")
                    self._send_404()
                    return
                self._stream_range(
                    target,
                    content_type=content_type,
                    cache_control="no-cache" if target.suffix == ".m3u8" else "public, max-age=43200",
                )
            else:
                try:
                    target = _movies_store.resolve_movie_stream_path(self.settings.movies_root, entry_key)
                except FileNotFoundError:
                    self._log_cast(f"404 unknown movie {entry_key}")
                    self._send_404()
                    return
                self._stream_range(target)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away mid-response -- nothing to send
        except Exception as error:  # noqa: BLE001 - a cast request must never crash this listener's thread
            self.log_error("cast-stream request failed: %s: %s", error.__class__.__name__, str(error))
            self._send_404()

    do_HEAD = do_GET

    def _send_cast_cors_headers(self) -> None:
        """Google's default media receiver runs the playback page on Google's
        own origin, so anything it fetches is cross-origin to it -- without
        these it can reject the media before playing a frame. Safe to be
        permissive: this listener only ever serves a movie whose
        single-movie-scoped token the caller already had."""
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range, Content-Type, Accept-Encoding")
        self.send_header(
            "Access-Control-Expose-Headers",
            "Accept-Ranges, Content-Length, Content-Range, Content-Type",
        )

    def _send_airplay_page(self, entry_key: str, token: str, delivery: str) -> None:
        """Serve a minimal HTTP-origin AirPlay controller for one movie.

        Safari 18+ upgrades HTTP media embedded by an HTTPS page to HTTPS.
        The cast listener cannot use HTTPS because a TV cannot trust Drone's
        private ``.local`` certificate, so the controller itself must be the
        top-level HTTP document. The same movie-scoped token gates both this
        page and every media request it can issue.
        """
        if delivery == "hls":
            try:
                _movie_cast_stream.resolve_hls_asset(self.settings, entry_key, token, "index.m3u8")
            except FileNotFoundError:
                self._log_cast(f"404 unavailable AirPlay HLS stream for {entry_key}")
                self._send_404()
                return
            media_path = f"/public/movies/{entry_key}/cast-hls/{token}/index.m3u8"
            media_type = "application/x-mpegURL"
        elif delivery == "direct":
            try:
                target = _movies_store.resolve_movie_stream_path(self.settings.movies_root, entry_key)
            except FileNotFoundError:
                self._send_404()
                return
            media_path = f"/public/movies/{entry_key}/cast-stream?token={token}"
            media_type = _CAST_VIDEO_CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        else:
            self._log_cast(f"404 invalid AirPlay delivery for {entry_key}")
            self._send_404()
            return

        safe_media_path = html.escape(media_path, quote=True)
        safe_media_type = html.escape(media_type, quote=True)
        body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Drone AirPlay</title>
  <style>
    :root {{ color-scheme: dark; font-family: system-ui, sans-serif; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #10151d; color: #f8f9fa; }}
    main {{ width: min(92vw, 850px); text-align: center; }}
    video {{ width: 100%; max-height: 68vh; background: #000; border-radius: .5rem; }}
    button {{ margin-top: 1rem; padding: .8rem 1.2rem; border: 0; border-radius: .45rem; font: inherit; font-weight: 650; }}
    button:not(:disabled) {{ cursor: pointer; background: #0d6efd; color: white; }}
    #status {{ min-height: 1.5rem; color: #b8c0cc; }}
  </style>
</head>
<body>
  <main>
    <h1>Drone AirPlay</h1>
    <p id="status">Loading the TV-compatible stream…</p>
    <video id="airplayVideo" controls playsinline preload="auto" x-webkit-airplay="allow">
      <source src="{safe_media_path}" type="{safe_media_type}">
    </video>
    <button id="airplayButton" type="button" disabled>Preparing video…</button>
  </main>
  <script>
    const video = document.getElementById("airplayVideo");
    const button = document.getElementById("airplayButton");
    const status = document.getElementById("status");
    const ready = () => {{
      button.disabled = false;
      button.textContent = "Choose AirPlay device";
      status.textContent = "The stream is ready.";
    }};
    video.addEventListener("loadedmetadata", ready, {{ once: true }});
    video.addEventListener("canplay", ready, {{ once: true }});
    video.addEventListener("error", () => {{
      button.disabled = true;
      button.textContent = "Stream unavailable";
      status.textContent = "Safari could not load the prepared stream. Return to Drone and try again.";
    }});
    video.addEventListener("webkitcurrentplaybacktargetiswirelesschanged", () => {{
      if (!video.webkitCurrentPlaybackTargetIsWireless) {{
        status.textContent = "AirPlay disconnected.";
        return;
      }}
      status.textContent = "AirPlay connected. Starting playback on the TV…";
      const playback = video.play();
      if (playback && typeof playback.catch === "function") {{
        playback.catch(() => {{
          status.textContent = "AirPlay connected. Press Play in the video controls to begin.";
        }});
      }}
    }});
    button.addEventListener("click", () => {{
      if (typeof video.webkitShowPlaybackTargetPicker !== "function") {{
        status.textContent = "Open this page in Safari to use AirPlay.";
        return;
      }}
      video.webkitShowPlaybackTargetPicker();
    }});
    if (video.readyState >= 1) ready();
  </script>
</body>
</html>
""".encode("utf-8")
        self._response_started = True
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; media-src 'self'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self._log_cast(f"200 AirPlay controller {delivery} {entry_key}")
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        # CORS preflight -- answered for any path (it reveals nothing; the
        # token check still gates the actual GET).
        try:
            self._response_started = True
            self.send_response(204)
            self._send_cast_cors_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _stream_range(
        self,
        path: Path,
        *,
        content_type: Optional[str] = None,
        cache_control: Optional[str] = None,
    ) -> None:
        file_size = path.stat().st_size
        start, end, status = _http_range.parse_range_header(self.headers.get("Range"), file_size)
        length = end - start + 1
        content_type = content_type or _CAST_VIDEO_CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        self._response_started = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        self._send_cast_cors_headers()
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()
        self._log_cast(f"{status} {content_type} bytes {start}-{end}/{file_size} {path.name}")
        if self.command == "HEAD":
            return
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _log_cast(self, message: str) -> None:
        """Cast requests DO get logged (one concise line each), unlike
        ``_HttpRedirectHandler``'s deliberately silent listener. Casting
        fails on the receiver, off-device, where there is nothing to inspect
        -- so "did the TV ever actually fetch anything, and what did we
        answer?" is the single most useful question when it doesn't work,
        and without this the answer is unknowable. Volume is bounded: a
        media player issues a modest number of range requests, not a flood.
        """
        client_ip = self.client_address[0] if self.client_address else "-"
        print(f"cast-stream {client_ip} {self.command} {message}", file=sys.stdout, flush=True)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass  # request-line noise; the useful signal is _log_cast's own lines


def _build_cast_http_handler(settings: Settings):
    def factory(*args, **kwargs):
        return _CastHttpHandler(*args, settings=settings, **kwargs)

    return factory


def create_server(settings: Settings) -> ThreadingHTTPServer:
    global _ROM_METADATA_POLLER_STARTED, _ROM_METADATA_WATCHER_STARTED, _LOCAL_NETWORK_WORKERS_STARTED, _GAME_PROCESS_MONITOR_STARTED, _GAME_PROCESS_MONITOR, _DOWNLOAD_MANAGER, _TORRENT_MANAGER, _AUTOMATION_POLLER_STARTED, _VPN_AUTO_CONNECT_ATTEMPTED, _VPN_SHARING_POLLER_STARTED, _VPN_SELF_HEAL_POLLER_STARTED, _SMTP_BOOTSTRAP_ATTEMPTED, _SMTP_SHARING_POLLER_STARTED, _AUDIT_EMAIL_POLLER_STARTED, _NETWORK_SHARE_BOOT_REPLAY_ATTEMPTED, _NETWORK_SHARE_WATCHDOG_STARTED, _NFS_EXPORT_BOOT_REPLAY_ATTEMPTED
    roms_root, bios_root = _real_data_roots(settings)
    repository = RomRepository(
        roms_root,
        bios_root,
        rom_search_cache_ttl_seconds=settings.rom_search_cache_ttl_seconds,
        settings=settings,
    )
    credential_store = DroneCredentialStore(
        settings.credentials_file,
        settings.username,
        settings.password,
        state_database_file=_state_database_path(settings.userdata_root),
    )
    session_store = SessionStore(_state_database_path(settings.userdata_root))
    auth = SessionAuth(credential_store=credential_store, session_store=session_store)
    cert_state = DroneCertificateManager(settings).ensure_certificate()
    if cert_state.get("error"):
        message = f"Drone certificate setup: {cert_state.get('error')}"
        if settings.drone_mtls_mode == "managed":
            raise RuntimeError(message)
        print(message, file=sys.stderr, flush=True)

    image_cache = ExpiringLRUCache(
        ttl_seconds=settings.image_cache_ttl_seconds,
        max_items=settings.image_cache_max_items,
        max_bytes=settings.image_cache_max_bytes,
    )
    image_miss_cache = ExpiringKeyCache(settings.image_miss_cache_ttl_seconds)
    json_cache = ExpiringLRUCache(
        ttl_seconds=settings.json_cache_ttl_seconds,
        max_items=settings.json_cache_max_items,
        max_bytes=settings.json_cache_max_bytes,
    )
    if _DOWNLOAD_MANAGER is None:
        _DOWNLOAD_MANAGER = DownloadManager(settings, repository)
    if _TORRENT_MANAGER is None:
        _TORRENT_MANAGER = TorrentManager(settings)
    if not _VPN_AUTO_CONNECT_ATTEMPTED:
        _VPN_AUTO_CONNECT_ATTEMPTED = True
        # Backgrounded: connect() can block for several seconds (spawning
        # openvpn, waiting for it to daemonize) and must never delay the
        # server from accepting its first request.
        Thread(target=_vpn_manager.maybe_auto_connect, args=(settings,), name="drone-vpn-auto-connect", daemon=True).start()
    if not _VPN_SHARING_POLLER_STARTED:
        _VPN_SHARING_POLLER_STARTED = True
        # Backgrounded forever-loop: the only way to learn a peer revoked
        # sharing is to periodically ask it (Drones are outbound-only, no push
        # channel) -- see run_sharing_revocation_poller's own docstring.
        Thread(target=_vpn_manager.run_sharing_revocation_poller, args=(settings,), name="drone-vpn-sharing-revocation", daemon=True).start()
    if not _VPN_SELF_HEAL_POLLER_STARTED:
        _VPN_SELF_HEAL_POLLER_STARTED = True
        # Backgrounded forever-loop: detects and recovers from a broken tunnel
        # (explicit connection errors, or a decrypt/replay-error flood) without
        # anyone needing to notice and manually reconnect -- see
        # run_self_heal_poller's own docstring for the rate-limiting.
        Thread(target=_vpn_manager.run_self_heal_poller, args=(settings,), name="drone-vpn-self-heal", daemon=True).start()
    if not _NFS_EXPORT_BOOT_REPLAY_ATTEMPTED:
        _NFS_EXPORT_BOOT_REPLAY_ATTEMPTED = True
        # Recreate source-side bind mounts and exact-peer exports after a
        # service or machine restart. This is independent of client mount
        # replay and never delays the HTTP listeners from becoming available.
        Thread(target=_nfs_export_manager.restore_exports, args=(settings,), name="drone-nfs-export-boot-replay", daemon=True).start()
    if not _NETWORK_SHARE_BOOT_REPLAY_ATTEMPTED:
        _NETWORK_SHARE_BOOT_REPLAY_ATTEMPTED = True
        # Backgrounded for the same reason as VPN's auto-connect above: mounting
        # a peer's network filesystem can block for real time and must never delay the
        # server accepting its first request.
        Thread(target=_network_share_manager.maybe_reconnect_all_on_boot, args=(settings,), name="drone-network-share-boot-replay", daemon=True).start()
    if not _NETWORK_SHARE_WATCHDOG_STARTED:
        _NETWORK_SHARE_WATCHDOG_STARTED = True
        # Backgrounded forever-loop: re-mounts a referenced peer's share if it
        # drops, same shape as VPN's self-heal poller above.
        Thread(target=_network_share_manager.run_watchdog_poller, args=(settings,), name="drone-network-share-watchdog", daemon=True).start()
    if not _SMTP_BOOTSTRAP_ATTEMPTED:
        _SMTP_BOOTSTRAP_ATTEMPTED = True
        # Backgrounded for the same reason as VPN's auto-connect above: a
        # peer fetch must never delay the server accepting its first request.
        Thread(target=_smtp_manager.maybe_bootstrap_smtp, args=(settings,), name="drone-smtp-bootstrap", daemon=True).start()
    if not _SMTP_SHARING_POLLER_STARTED:
        _SMTP_SHARING_POLLER_STARTED = True
        # Same reasoning as VPN's sharing-revocation poller: outbound-only,
        # no push channel, so revocation can only be learned by periodically asking.
        Thread(target=_smtp_manager.run_sharing_revocation_poller, args=(settings,), name="drone-smtp-sharing-revocation", daemon=True).start()
    if not _AUDIT_EMAIL_POLLER_STARTED:
        _AUDIT_EMAIL_POLLER_STARTED = True
        # The "cron style job every ~5 minutes" -- there is no OS cron
        # anywhere in this app; every periodic feature is an in-process
        # thread on this exact shape (see the VPN pollers just above).
        Thread(target=_smtp_manager.run_audit_email_digest_poller, args=(settings,), name="drone-audit-email-digest", daemon=True).start()
    _ensure_game_event_spool(settings)
    if not _GAME_PROCESS_MONITOR_STARTED:
        poll_seconds = max(0.25, float(os.environ.get("GAME_PROCESS_POLL_SECONDS", "2")))
        _GAME_PROCESS_MONITOR = GameProcessMonitor(settings, poll_seconds=poll_seconds)
        _GAME_PROCESS_MONITOR.start()
        _GAME_PROCESS_MONITOR_STARTED = True

    handler_factory = _build_handler(
        settings=settings,
        auth=auth,
        repository=repository,
        image_cache=image_cache,
        image_miss_cache=image_miss_cache,
        json_cache=json_cache,
    )

    server = DroneThreadingHTTPServer(("0.0.0.0", settings.https_port), handler_factory)
    server.auth = auth  # type: ignore[attr-defined]
    _apply_server_tls(settings, server, peer_mtls=False)

    compatibility_servers = []
    for compatibility_port in settings.compatibility_https_ports:
        try:
            compatibility_server = DroneThreadingHTTPServer(("0.0.0.0", compatibility_port), handler_factory)
            compatibility_server.auth = auth  # type: ignore[attr-defined]
            _apply_server_tls(settings, compatibility_server, peer_mtls=False)
        except OSError as error:
            print(
                f"Drone compatibility listener skipped on port {compatibility_port}: {error}",
                file=sys.stderr,
                flush=True,
            )
            continue
        compatibility_thread = Thread(
            target=compatibility_server.serve_forever,
            name=f"drone-compat-listener-{compatibility_port}",
            daemon=True,
        )
        compatibility_thread.start()
        compatibility_server.thread = compatibility_thread  # type: ignore[attr-defined]
        compatibility_servers.append(compatibility_server)
        scheme = "http" if settings.http_only else "https"
        print(f"Serving Drone compatibility listener on {scheme}://0.0.0.0:{compatibility_port}", flush=True)
    server.compatibility_servers = compatibility_servers  # type: ignore[attr-defined]

    # The one dedicated peer-to-peer mTLS listener: CERT_OPTIONAL + paired-peer
    # trust lives here only, so browsers on the ports above never get asked for
    # a client certificate. api_routes.py's routing guard restricts this
    # listener to /peer/* + bare /health; if the bind itself fails, fall back
    # to the pre-split behavior (peer traffic allowed on the main listener too)
    # rather than silently going P2P-dark behind a healthy-looking browser UI.
    peer_mtls_server = None
    try:
        peer_mtls_server = DroneThreadingHTTPServer(("0.0.0.0", settings.peer_mtls_port), handler_factory)
        peer_mtls_server.auth = auth  # type: ignore[attr-defined]
        _apply_server_tls(settings, peer_mtls_server, peer_mtls=True)
    except OSError as error:
        print(
            f"Drone peer-mTLS listener skipped on port {settings.peer_mtls_port} (falling back to "
            f"serving /peer/* on the main listener): {error}",
            file=sys.stderr,
            flush=True,
        )
        peer_mtls_server = None
    else:
        peer_mtls_thread = Thread(
            target=peer_mtls_server.serve_forever,
            name="drone-peer-mtls-listener",
            daemon=True,
        )
        peer_mtls_thread.start()
        peer_mtls_server.thread = peer_mtls_thread  # type: ignore[attr-defined]
        scheme = "http" if settings.http_only else "https"
        print(f"Serving Drone peer-mTLS listener on {scheme}://0.0.0.0:{settings.peer_mtls_port}", flush=True)

    # Plain-HTTP -> HTTPS redirect listener. Skipped in http_only mode (there is
    # no HTTPS to redirect to there) and when disabled via http_redirect_port=0.
    # Never TLS-wrapped -- this is the one listener deliberately left unencrypted,
    # and it only ever serves a 301 (see _HttpRedirectHandler).
    http_redirect_server = None
    if not settings.http_only and settings.http_redirect_port > 0:
        try:
            http_redirect_server = DroneThreadingHTTPServer(
                ("0.0.0.0", settings.http_redirect_port), _build_http_redirect_handler(settings)
            )
        except OSError as error:
            print(
                f"HTTP->HTTPS redirect listener skipped on port {settings.http_redirect_port}: {error}",
                file=sys.stderr,
                flush=True,
            )
            http_redirect_server = None
        else:
            http_redirect_thread = Thread(
                target=http_redirect_server.serve_forever,
                name="drone-http-redirect-listener",
                daemon=True,
            )
            http_redirect_thread.start()
            http_redirect_server.thread = http_redirect_thread  # type: ignore[attr-defined]
            print(
                f"Serving HTTP->HTTPS redirect on http://0.0.0.0:{settings.http_redirect_port}",
                flush=True,
            )
    server.http_redirect_server = http_redirect_server  # type: ignore[attr-defined]

    # Cast-stream listener: on by default (settings.cast_enabled), opt-out
    # via DRONE_CAST_ENABLED=0. Unlike http_redirect_server above, this one
    # plain-HTTP listener does serve real content (a token-gated movie
    # stream, see _CastHttpHandler) -- acceptable on by default because
    # that content is gated by a single-movie-scoped token only an
    # already-authenticated session could have minted, not left wide open.
    cast_http_server = None
    if settings.cast_enabled:
        try:
            cast_http_server = DroneThreadingHTTPServer(
                ("0.0.0.0", settings.cast_http_port), _build_cast_http_handler(settings)
            )
        except OSError as error:
            print(
                f"Cast-stream listener skipped on port {settings.cast_http_port}: {error}",
                file=sys.stderr,
                flush=True,
            )
            cast_http_server = None
        else:
            cast_http_thread = Thread(
                target=cast_http_server.serve_forever,
                name="drone-cast-http-listener",
                daemon=True,
            )
            cast_http_thread.start()
            cast_http_server.thread = cast_http_thread  # type: ignore[attr-defined]
            print(
                f"Serving cast-stream listener on http://0.0.0.0:{settings.cast_http_port} (token-gated, movies only)",
                flush=True,
            )
    server.cast_http_server = cast_http_server  # type: ignore[attr-defined]

    all_tls_servers = [server, *compatibility_servers] + ([peer_mtls_server] if peer_mtls_server else [])
    for tls_server in all_tls_servers:
        tls_server.all_tls_servers = all_tls_servers  # type: ignore[attr-defined]
        tls_server.is_peer_mtls_listener = False  # type: ignore[attr-defined]
    if peer_mtls_server is not None:
        peer_mtls_server.is_peer_mtls_listener = True  # type: ignore[attr-defined]

    if not _LOCAL_NETWORK_WORKERS_STARTED:
        _start_local_network_workers(settings)
        _LOCAL_NETWORK_WORKERS_STARTED = True
    if not _AUTOMATION_POLLER_STARTED:
        _start_automation_poller(settings)
        _AUTOMATION_POLLER_STARTED = True
    if settings.rom_metadata_poll_seconds == 0:
        print("Asset metadata poller disabled: ROM_METADATA_POLL_SECONDS=0", file=sys.stdout, flush=True)
    elif not _ROM_METADATA_POLLER_STARTED:
        _start_rom_metadata_poller(settings, repository)
        _ROM_METADATA_POLLER_STARTED = True
    else:
        print("Asset metadata poller already started", file=sys.stdout, flush=True)

    # Near-real-time ROM change detection wakes the poller above; only useful
    # when the poller is running.
    if settings.rom_metadata_poll_seconds == 0 or not ROM_METADATA_WATCH_ENABLED:
        if not ROM_METADATA_WATCH_ENABLED:
            print("ROM filesystem watcher disabled: ROM_METADATA_WATCH_ENABLED=0", file=sys.stdout, flush=True)
    elif not _ROM_METADATA_WATCHER_STARTED:
        _start_rom_metadata_watcher(settings)
        _ROM_METADATA_WATCHER_STARTED = True

    return server


def main() -> None:
    settings = Settings.from_env()
    try:
        if settings.use_fake_data:
            try:
                from .mock_data import seed_mock_userdata
            except ImportError:
                from mock_data import seed_mock_userdata  # type: ignore

            seed_mock_userdata(settings.userdata_root)
            print(f"USE_FAKE_DATA enabled: seeded fake dataset at {settings.userdata_root}")
        _configure_rotating_logs(settings)
        server = create_server(settings)
        _start_drone_auto_update_poller(settings)
        # Optional, opt-in (DRONE_API_FASTAPI_BRIDGE=1): start the FastAPI typed-API bridge.
        # Fully guarded — any failure leaves it inactive and the stdlib server serves everything.
        try:
            try:
                from .web.api_bridge import maybe_start as _maybe_start_api_bridge
            except ImportError:
                from web.api_bridge import maybe_start as _maybe_start_api_bridge  # type: ignore
            _maybe_start_api_bridge(settings)
        except Exception as _bridge_error:  # noqa: BLE001
            print(f"FastAPI bridge startup skipped: {_bridge_error}", file=sys.stderr, flush=True)
        print(f"Log files: {settings.log_dir / settings.stdout_log_file}, {settings.log_dir / settings.stderr_log_file}")
        server_auth = getattr(server, "auth", None)
        credential_store = getattr(server_auth, "credential_store", None)
        safe_username = credential_store.load().get("username") if credential_store else settings.username
        print(f"Auth username: {safe_username}")
        scheme = "http" if settings.http_only else "https"
        print(f"Serving Drone App on {scheme}://0.0.0.0:{settings.https_port}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        print("Drone App shutdown requested", file=sys.stderr, flush=True)
        raise
    except BaseException:
        print("Drone App fatal error:", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
