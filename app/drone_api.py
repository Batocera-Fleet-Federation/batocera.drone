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
    from .overmind.overmind_filesystem import (
        filesystem_events as _build_filesystem_events,
        filesystem_snapshot as _filesystem_snapshot,
    )
    from .overmind.overmind_game_logs import commit_game_log_cursors as _commit_game_log_cursors
    from .overmind.overmind_game_logs import collect_game_logs as _build_game_log_payload
    from .overmind.overmind_game_logs import collect_game_event_sessions as _collect_game_event_sessions
    from .overmind.overmind_game_logs import delete_game_event_spool as _delete_game_event_spool
    from .overmind.overmind_game_logs import GameProcessMonitor
    from .overmind.overmind_game_logs import load_gameplay_history as _load_gameplay_history
    from .overmind.overmind_game_logs import pending_game_event_count as _pending_game_event_count
    from .overmind.overmind_reporting import (
        collect_emulator_configs as _collect_emulator_configs,
        collect_log_sources as _collect_log_sources,
        commit_emulator_config_fingerprints as _commit_emulator_config_fingerprints,
        commit_log_cursors as _commit_log_cursors,
        list_emulator_config_files as _list_emulator_config_files,
        read_emulator_config_file as _read_emulator_config_file,
    )
    from .transfer.peer_selection import select_best_peer as _select_best_peer
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
    from .storage.state_store import (
        append_event as _append_state_event,
        database_path as _state_database_path,
        database_path_for_legacy_file as _state_database_path_for_legacy_file,
        load_events as _load_state_events,
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
    from .transport.mux_client import MuxClient, MuxSession, connect_tls, parse_edge_endpoint
    from .transport import assetfetch as _assetfetch
    from .transport import relay_transfer as _relay_transfer
    from .transport import holepunch as _holepunch
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
    from overmind.overmind_filesystem import (  # type: ignore
        filesystem_events as _build_filesystem_events,
        filesystem_snapshot as _filesystem_snapshot,
    )
    from overmind.overmind_game_logs import commit_game_log_cursors as _commit_game_log_cursors  # type: ignore
    from overmind.overmind_game_logs import collect_game_logs as _build_game_log_payload  # type: ignore
    from overmind.overmind_game_logs import collect_game_event_sessions as _collect_game_event_sessions  # type: ignore
    from overmind.overmind_game_logs import delete_game_event_spool as _delete_game_event_spool  # type: ignore
    from overmind.overmind_game_logs import GameProcessMonitor  # type: ignore
    from overmind.overmind_game_logs import load_gameplay_history as _load_gameplay_history  # type: ignore
    from overmind.overmind_game_logs import pending_game_event_count as _pending_game_event_count  # type: ignore
    from overmind.overmind_reporting import (  # type: ignore
        collect_emulator_configs as _collect_emulator_configs,
        collect_log_sources as _collect_log_sources,
        commit_emulator_config_fingerprints as _commit_emulator_config_fingerprints,
        commit_log_cursors as _commit_log_cursors,
        list_emulator_config_files as _list_emulator_config_files,
        read_emulator_config_file as _read_emulator_config_file,
    )
    from transfer.peer_selection import select_best_peer as _select_best_peer  # type: ignore
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
    from storage.state_store import (  # type: ignore
        append_event as _append_state_event,
        database_path as _state_database_path,
        database_path_for_legacy_file as _state_database_path_for_legacy_file,
        load_events as _load_state_events,
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
    from transport.mux_client import MuxClient, MuxSession, connect_tls, parse_edge_endpoint  # type: ignore
    from transport import assetfetch as _assetfetch  # type: ignore
    from transport import relay_transfer as _relay_transfer  # type: ignore
    from transport import holepunch as _holepunch  # type: ignore
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
        _overmind_log,
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
        _overmind_log,
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
        BasicAuth,
        DroneCredentialStore,
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
        BasicAuth,
        DroneCredentialStore,
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
    from .overmind.overmind_client import (
        _drone_client_ssl_context,
        _format_overmind_error,
        _overmind_delete_json,
        _overmind_get_json,
        _overmind_post_json,
        _overmind_post_json_with_status,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from overmind.overmind_client import (  # type: ignore
        _drone_client_ssl_context,
        _format_overmind_error,
        _overmind_delete_json,
        _overmind_get_json,
        _overmind_post_json,
        _overmind_post_json_with_status,
    )


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
    from .roms.rom_inventory import (
        ROM_METADATA_UPLOAD_CHUNK_SIZE,
        _chunk_rom_metadata_delta,
        _chunk_rom_metadata_inventory,
        _json_payload_size_bytes,
        _rom_metadata_inventory_id,
        _wire_asset_rows,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from roms.rom_inventory import (  # type: ignore
        ROM_METADATA_UPLOAD_CHUNK_SIZE,
        _chunk_rom_metadata_delta,
        _chunk_rom_metadata_inventory,
        _json_payload_size_bytes,
        _rom_metadata_inventory_id,
        _wire_asset_rows,
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
    from .overmind.collectors import (
        OVERMIND_EVENT_TYPES,
        _collect_game_logs,
        _filesystem_events,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from overmind.collectors import (  # type: ignore
        OVERMIND_EVENT_TYPES,
        _collect_game_logs,
        _filesystem_events,
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
    from .overmind.overmind_config import (
        FAKE_OVERMIND_EMAIL,
        FAKE_OVERMIND_PASSWORD,
        FAKE_OVERMIND_TOKEN,
        build_overmind_status,
        mask_secret,
        overmind_config_path,
        overmind_load_config,
        overmind_load_json_file,
        overmind_peer_results_path,
        overmind_public_payload,
        overmind_save_config,
        overmind_swarm_path,
        _load_overmind_config_for_settings,
        _log_overmind_onboarding,
        _mark_overmind_auth_failed,
        _normalize_overmind_link_state,
        _overmind_actions_path_for_settings,
        _overmind_config_path_for_settings,
        _overmind_onboarding_context,
        _overmind_peer_results_path_for_settings,
        _overmind_swarm_path_for_settings,
        _read_json_file,
        _safe_token_fingerprint,
        _save_overmind_runtime_config,
        _strip_fake_overmind_values,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from overmind.overmind_config import (  # type: ignore
        FAKE_OVERMIND_EMAIL,
        FAKE_OVERMIND_PASSWORD,
        FAKE_OVERMIND_TOKEN,
        build_overmind_status,
        mask_secret,
        overmind_config_path,
        overmind_load_config,
        overmind_load_json_file,
        overmind_peer_results_path,
        overmind_public_payload,
        overmind_save_config,
        overmind_swarm_path,
        _load_overmind_config_for_settings,
        _log_overmind_onboarding,
        _mark_overmind_auth_failed,
        _normalize_overmind_link_state,
        _overmind_actions_path_for_settings,
        _overmind_config_path_for_settings,
        _overmind_onboarding_context,
        _overmind_peer_results_path_for_settings,
        _overmind_swarm_path_for_settings,
        _read_json_file,
        _safe_token_fingerprint,
        _save_overmind_runtime_config,
        _strip_fake_overmind_values,
    )


# FAKE_OVERMIND_* moved to overmind/overmind_config.py (re-exported above).
try:
    from .overmind.action_poller import (
        _start_overmind_action_poller,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from overmind.action_poller import (  # type: ignore
        _start_overmind_action_poller,
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
    from .roms.rom_collect import (
        _collect_rom_metadata,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from roms.rom_collect import (  # type: ignore
        _collect_rom_metadata,
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
    from .overmind.rom_sync import (
        _complete_local_rom_metadata_cache,
        _defer_rom_metadata_upload,
        _poll_rom_metadata_once,
        _sync_rom_metadata_to_overmind,
        _sync_rom_metadata_to_overmind_locked,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from overmind.rom_sync import (  # type: ignore
        _complete_local_rom_metadata_cache,
        _defer_rom_metadata_upload,
        _poll_rom_metadata_once,
        _sync_rom_metadata_to_overmind,
        _sync_rom_metadata_to_overmind_locked,
    )


try:
    from .roms.rom_scanner import (
        _hash_rom_metadata_batches,
        _poll_rom_metadata_cache,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from roms.rom_scanner import (  # type: ignore
        _hash_rom_metadata_batches,
        _poll_rom_metadata_cache,
    )


try:
    from .overmind.saves_sync import _sync_saves_to_overmind
except ImportError:
    if __package__ not in (None, ""):
        raise
    from overmind.saves_sync import _sync_saves_to_overmind  # type: ignore


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
    from .overmind.heartbeat_sync import (
        _local_asset_thumbprints,
        _local_saves_thumbprint,
        _maybe_request_asset_push_from_heartbeat,
        _maybe_request_saves_push_from_heartbeat,
        _snapshot_asset_thumbprints,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from overmind.heartbeat_sync import (  # type: ignore
        _local_asset_thumbprints,
        _local_saves_thumbprint,
        _maybe_request_asset_push_from_heartbeat,
        _maybe_request_saves_push_from_heartbeat,
        _snapshot_asset_thumbprints,
    )


try:
    from .transfer.peer_workers import (
        _probe_peer_public_ip,
        _start_local_network_workers,
        _start_peer_health_check_thread,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from transfer.peer_workers import (  # type: ignore
        _probe_peer_public_ip,
        _start_local_network_workers,
        _start_peer_health_check_thread,
    )


try:
    from .overmind.actions import _execute_overmind_action
except ImportError:
    if __package__ not in (None, ""):
        raise
    from overmind.actions import _execute_overmind_action  # type: ignore


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
    from .transfer.edge_relay import (
        _edge_mux_available,
        _edge_stun_addr,
        _edge_token_for,
        _handle_transfer_offer,
        _local_network_snapshot,
        _maybe_holepunch,
        _relay_download_rom,
        _relay_fetch,
        _request_transfer_session,
        _serve_transfer_offer,
        _start_edge_mux_client,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from transfer.edge_relay import (  # type: ignore
        _edge_mux_available,
        _edge_stun_addr,
        _edge_token_for,
        _handle_transfer_offer,
        _local_network_snapshot,
        _maybe_holepunch,
        _relay_download_rom,
        _relay_fetch,
        _request_transfer_session,
        _serve_transfer_offer,
        _start_edge_mux_client,
    )


try:
    from .transfer.peer_download import (
        _best_peer_for_bios,
        _best_peer_for_rom,
        _cached_rom_fingerprint_exists,
        _download_artwork_from_peer,
        _download_bios_from_peer,
        _download_rom_folder_from_peer,
        _download_rom_from_peer,
        _download_save_from_peer,
        _post_download_state,
        _post_rom_sync_activity,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from transfer.peer_download import (  # type: ignore
        _best_peer_for_bios,
        _best_peer_for_rom,
        _cached_rom_fingerprint_exists,
        _download_artwork_from_peer,
        _download_bios_from_peer,
        _download_rom_folder_from_peer,
        _download_rom_from_peer,
        _download_save_from_peer,
        _post_download_state,
        _post_rom_sync_activity,
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
        _fetch_peer_certificate,
        _is_ssl_url_error,
        _local_pair_peer,
        _local_peer_cert_cache_path,
        _peer_address,
        _peer_api_port,
        _peer_cert_cache_dir,
        _peer_cert_cache_path,
        _peer_cert_meta_path,
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
        _fetch_peer_certificate,
        _is_ssl_url_error,
        _local_pair_peer,
        _local_peer_cert_cache_path,
        _peer_address,
        _peer_api_port,
        _peer_cert_cache_dir,
        _peer_cert_cache_path,
        _peer_cert_meta_path,
        _peer_get_json,
        _peer_health_url,
        _peer_ssl_diagnostic,
        _peer_trust_cafile,
        _public_local_peer,
        _save_local_peer_certificate,
    )


try:
    from .overmind.registration import (
        _record_processed_overmind_action,
        _reclaim_overmind_token_after_unauthorized,
        _register_or_claim_overmind_token,
        _report_overmind_action_completion,
        _summarize_overmind_result,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from overmind.registration import (  # type: ignore
        _record_processed_overmind_action,
        _reclaim_overmind_token_after_unauthorized,
        _register_or_claim_overmind_token,
        _report_overmind_action_completion,
        _summarize_overmind_result,
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
        _push_automation_config_to_overmind,
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
        _push_automation_config_to_overmind,
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


_OVERMIND_POLLER_STARTED = False
_ROM_METADATA_POLLER_STARTED = False
_ROM_METADATA_WATCHER_STARTED = False
_ROM_METADATA_WATCHER = None
_SAVES_METADATA_WATCHER = None
# File-only rotating stream for Overmind-related logs; configured in _configure_rotating_logs.
# _OVERMIND_LOG_STREAM now lives in logging_setup.py
_PEER_HEALTH_CHECK_THREAD_STARTED = False
_LOCAL_NETWORK_WORKERS_STARTED = False
_EDGE_MUX_STARTED = False
# _EDGE_MUX_CLIENT (the persistent Edge connection) now lives in transfer/edge_relay.py.
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
        _ASSET_PUSH_REQUESTED,
        _GAMELIST_WRITE_LOCK,
        _ROM_METADATA_ACTIVE,
        _ROM_METADATA_LOCK,
        _ROM_METADATA_WAKE,
        _SAVES_PUSH_REQUESTED,
    )
except ImportError:
    if __package__ not in (None, ""):
        raise
    from common.runtime_state import (  # type: ignore
        _ASSET_PUSH_REQUESTED,
        _GAMELIST_WRITE_LOCK,
        _ROM_METADATA_ACTIVE,
        _ROM_METADATA_LOCK,
        _ROM_METADATA_WAKE,
        _SAVES_PUSH_REQUESTED,
    )
_DOWNLOAD_MANAGER = None
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
# OVERMIND_EVENT_TYPES moved to overmind/collectors.py (re-exported).
# DOWNLOAD_TERMINAL_STATUSES now lives in transfer/download_manager.py (its only user).
PERSISTENT_OVERMIND_LOG_SOURCES = ("drone_stderr", "es_launch_stdout", "es_launch_stderr")
# DOWNLOAD_PROGRESS_PUSH_SECONDS now lives in transfer/download_manager.py (its only user).
PEER_CHECK_TIMEOUT_SECONDS = float(os.environ.get("DRONE_PEER_CHECK_TIMEOUT_SECONDS", "3"))
# Browsing/copying a peer's inventory can scan a large library to build a page,
# which far exceeds the quick health-check timeout. Give inventory reads a much
# longer budget so big libraries don't surface as "read operation timed out".
PEER_INVENTORY_TIMEOUT_SECONDS = float(os.environ.get("DRONE_PEER_INVENTORY_TIMEOUT_SECONDS", "120"))
PEER_CHECK_INTERVAL_SECONDS = int(os.environ.get("DRONE_PEER_CHECK_INTERVAL_SECONDS", "300"))
OVERMIND_SPEED_SAMPLE_SECONDS = int(os.environ.get("OVERMIND_SPEED_SAMPLE_SECONDS", "600"))
# SPEED_TEST_DEFAULT_BASE_URL moved to device/system_metrics.py (re-exported above).
OVERMIND_HEARTBEAT_SECONDS = int(os.environ.get("OVERMIND_POLL_SECONDS", "30"))
OVERMIND_HEARTBEAT_TIMEOUT_SECONDS = max(10, int(os.environ.get("OVERMIND_HEARTBEAT_TIMEOUT_SECONDS", "20")))
OVERMIND_CONFIG_REPORT_SECONDS = int(os.environ.get("OVERMIND_CONFIG_REPORT_SECONDS", "300"))
ROM_METADATA_POLL_SECONDS = int(os.environ.get("ROM_METADATA_POLL_SECONDS", "300"))
ROM_METADATA_INITIAL_DELAY_SECONDS = int(os.environ.get("ROM_METADATA_INITIAL_DELAY_SECONDS", "60"))
ROM_METADATA_PROGRESS_SECONDS = float(os.environ.get("ROM_METADATA_PROGRESS_SECONDS", "30"))
ROM_METADATA_PROGRESS_FILES = int(os.environ.get("ROM_METADATA_PROGRESS_FILES", "250"))
ROM_METADATA_FINGERPRINT_BATCH_SIZE = max(1, int(os.environ.get("ROM_METADATA_FINGERPRINT_BATCH_SIZE", "250")))
# ROM_METADATA_UPLOAD_CHUNK_SIZE moved to roms/rom_inventory.py (re-exported).
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
OVERMIND_UPLOAD_TIMEOUT_SECONDS = max(10, int(os.environ.get("OVERMIND_UPLOAD_TIMEOUT_SECONDS", "60")))
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
# _configure_rotating_logs, _overmind_log) and the _OVERMIND_LOG_STREAM global
# now live in logging_setup.py (re-exported near the top of this module).


# DroneCredentialStore, BasicAuth, the 401 brute-force blocker and the
# unauthenticated-request rate limiter now live in auth.py (re-exported near
# the top of this module).


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


class RomRepository(RomAssetBiosMixin, RomSystemsSearchMixin, RomScanMixin, RomArtworkGamelistMixin, RomArtworkApplyMixin):
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
        return not str(name or "").strip().lower().endswith(".old")

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

# Overmind config public API (overmind_load_config/save/status, mask_secret, ...)
# now lives in overmind/overmind_config.py (re-exported above).


try:
    from .web.handlers_peer import HandlersPeerMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from web.handlers_peer import HandlersPeerMixin  # type: ignore


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
    from .web.handlers_overmind import HandlersOvermindMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from web.handlers_overmind import HandlersOvermindMixin  # type: ignore


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


class RomRequestHandler(HandlersSystemMixin, HandlersDownloadsMixin, HandlersDiagnosticsMixin, HandlersConfigMixin, HandlersOvermindMixin, HandlersNetworkMixin, HandlersArtworkMixin, HandlersContentMixin, ThemeMetaMixin, HandlersEsCollectionsMixin, HandlersPeerMixin, ApiRoutesMixin, UiRoutesMixin, BaseHTTPRequestHandler):
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
        auth: BasicAuth,
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
        return "application/octet-stream"

    def _send_unauthorized(self) -> None:
        has_auth_header = bool(self.headers.get("Authorization"))
        if DRONE_LOG_UNAUTHORIZED_REQUESTS or has_auth_header:
            self.log_error(
                '401 unauthorized "%s" auth_header_present=%s',
                self.path.split("?", 1)[0],
                "yes" if has_auth_header else "no",
            )
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Drone App"')
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
        if self.auth.check(self.headers.get("Authorization")):
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

    def _send_security_headers(self) -> None:
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
        self.send_header("Cache-Control", "no-store")
        # CSP keeps UI/resource loading strict while still allowing bundled Swagger assets.
        self.send_header(
            "Content-Security-Policy",
            f"default-src 'self'; img-src {' '.join(image_sources)}; style-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net; "
            "font-src 'self' data: https://cdn.jsdelivr.net https://fonts.gstatic.com; connect-src 'self' https://unpkg.com https://cdn.jsdelivr.net; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
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

    def _send_json(self, status_code: int, payload: dict, cache_key: Optional[str] = None) -> None:
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

    def _overmind_config_path(self) -> Path:
        return overmind_config_path(self.settings)

    def _overmind_actions_path(self) -> Path:
        return Path(os.environ.get(
            "OVERMIND_ACTION_LOG_FILE",
            str(self.settings.userdata_root / "system" / "drone-app" / "overmind_actions.log"),
        )).resolve()

    def _overmind_swarm_path(self) -> Path:
        return overmind_swarm_path(self.settings)

    def _overmind_peer_results_path(self) -> Path:
        return overmind_peer_results_path(self.settings)

    def _rom_fingerprint_cache_path(self) -> Path:
        return (self.settings.userdata_root / "system" / "drone-app" / "rom_fingerprint_cache.json").resolve()

    def _mask_secret(self, value: str) -> str:
        return mask_secret(value)

    def _load_overmind_config(self) -> dict:
        return overmind_load_config(self.settings)

    def _save_overmind_config(self, payload: dict) -> None:
        overmind_save_config(self.settings, payload)

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

    def _overmind_public_payload(self, config: dict) -> dict:
        return overmind_public_payload(self.settings, config)

    def _load_processed_overmind_actions(self) -> List[dict]:
        return _load_state_events(
            _state_database_path(self.settings.userdata_root),
            "overmind_actions",
            legacy_path=self._overmind_actions_path(),
        )

    # HandlersSystemMixin methods now live in web/handlers_system.py (composed onto RomRequestHandler).

    def _handle_public_health(self) -> None:
        self._send_json(
            200,
            {
                "status": "ok",
                "drone_id": self.settings.overmind_device_id,
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
        if _local_network.is_local_mode(self.settings):
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
            if not _local_network.is_overmind_mode(self.settings):
                self._send_json(403, {"error": "paired client certificate required"})
                return False
        if self.settings.drone_mtls_enabled:
            cert = self.connection.getpeercert() if hasattr(self.connection, "getpeercert") else None
            if not cert:
                self._send_json(403, {"error": "client certificate required"})
                return False
            try:
                der = self.connection.getpeercert(binary_form=True)
            except Exception:
                der = None
            fingerprint = hashlib.sha256(der).hexdigest().lower() if der else ""
            local_match = next(
                (
                    peer
                    for peer in _local_network.paired_peers(self.settings)
                    if str(peer.get("certificate_fingerprint") or "").strip().lower() == fingerprint
                ),
                None,
            )
            if local_match:
                peer_id = str(local_match.get("drone_id") or "")
                approved_path = _peer_cert_cache_path(self.settings, peer_id)
                try:
                    independently_approved = (
                        approved_path.exists()
                        and _certificate_pem_fingerprint(approved_path.read_text(encoding="utf-8", errors="ignore")).lower() == fingerprint
                    )
                except Exception:
                    independently_approved = False
                if not independently_approved:
                    self._send_json(403, {"error": "Local Network pairing trust is inactive in Overmind mode"})
                    return False
        return True

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
    auth: BasicAuth,
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


# _collect_rom_metadata now live in roms/rom_collect.py (re-exported below).


def _kick_asset_metadata_sync_after_download(settings: Settings, repository: "RomRepository", config: dict, reason: str) -> None:
    if not _local_network.is_overmind_mode(settings):
        _ROM_METADATA_WAKE.set()
        return
    base_url = str(config.get("overmind_url") or "").strip().rstrip("/")
    token = str(config.get("overmind_token") or "").strip()
    if not base_url or not token:
        return

    def run() -> None:
        try:
            result = _sync_rom_metadata_to_overmind(settings, repository, config, base_url, token)
            _overmind_log(
                f"Asset metadata follow-up sync completed: reason={reason} status={result.get('status')} changed={result.get('changed')}"
            )
        except Exception as error:
            _overmind_log(
                f"Asset metadata follow-up sync failed: reason={reason} error={_format_overmind_error(error)}"
            )

    Thread(target=run, name="asset-metadata-follow-up-sync", daemon=True).start()


# DownloadManager (queue + transport-tier dispatch) + _directpublic_fetch now live
# in transfer/download_manager.py (re-exported below). The running singleton
# (_DOWNLOAD_MANAGER / _get_download_manager) stays here.


def _get_download_manager() -> Optional["DownloadManager"]:
    return _DOWNLOAD_MANAGER


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


# Edge mux client + relay/hole-punch transfer tiers now live in
# transfer/edge_relay.py (re-exported below).


# Direct-peer asset downloads (state helpers + best-peer ranking + _download_*_from_peer)
# now live in transfer/peer_download.py (re-exported below).


# _execute_overmind_action (Overmind action dispatcher) now lives in overmind/actions.py (re-exported below).


# _start_overmind_action_poller now live in overmind/action_poller.py (re-exported below).


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
                    f"ROM metadata sync failed:{status_part} error={_format_overmind_error(error)} duration_ms={int((time.monotonic() - poll_started) * 1000)}",
                    file=sys.stderr,
                    flush=True,
                )
            except Exception as error:
                print(
                    f"ROM metadata sync failed: error={_format_overmind_error(error)} duration_ms={int((time.monotonic() - poll_started) * 1000)}",
                    file=sys.stderr,
                    flush=True,
                )
            if _ROM_METADATA_WAKE.wait(poll_seconds):
                _ROM_METADATA_WAKE.clear()

    thread = Thread(target=loop, name="rom-metadata-poller", daemon=True)
    thread.start()
    print("Asset metadata poller thread started", file=sys.stdout, flush=True)


def _start_rom_metadata_watcher(settings: Settings) -> None:
    """Wake the metadata poller in near real time when ROM files change.

    Best-effort: if inotify is unavailable the periodic poll still covers
    changes, so a failure here is logged and otherwise ignored.
    """
    global _ROM_METADATA_WATCHER, _SAVES_METADATA_WATCHER
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
        print(f"Gameplay event spool setup skipped: {_format_overmind_error(error)}", file=sys.stderr, flush=True)


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


def _apply_server_tls(settings: Settings, server: ThreadingHTTPServer) -> None:
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
    if settings.drone_mtls_enabled or _local_network.is_local_mode(settings):
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
    server.ssl_context = ssl_context  # type: ignore[attr-defined]
    # do_handshake_on_connect=False is critical: wrapping the LISTENING socket otherwise
    # makes accept() perform the TLS handshake on the single serve_forever thread, so one
    # silent client (e.g. an internet scanner that opens 443 and never speaks) blocks
    # accept() forever and wedges the whole server. Deferring the handshake lets accept()
    # return immediately; the handshake then runs in the per-request worker thread under
    # RomRequestHandler.timeout, where a stall costs only that one thread.
    server.socket = ssl_context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)


def create_server(settings: Settings) -> ThreadingHTTPServer:
    global _OVERMIND_POLLER_STARTED, _ROM_METADATA_POLLER_STARTED, _ROM_METADATA_WATCHER_STARTED, _PEER_HEALTH_CHECK_THREAD_STARTED, _LOCAL_NETWORK_WORKERS_STARTED, _GAME_PROCESS_MONITOR_STARTED, _GAME_PROCESS_MONITOR, _DOWNLOAD_MANAGER, _AUTOMATION_POLLER_STARTED, _EDGE_MUX_STARTED
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
    auth = BasicAuth(settings.username, settings.password, credential_store=credential_store)
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
    _apply_server_tls(settings, server)

    compatibility_servers = []
    for compatibility_port in settings.compatibility_https_ports:
        try:
            compatibility_server = DroneThreadingHTTPServer(("0.0.0.0", compatibility_port), handler_factory)
            compatibility_server.auth = auth  # type: ignore[attr-defined]
            _apply_server_tls(settings, compatibility_server)
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

    if not _OVERMIND_POLLER_STARTED:
        _start_overmind_action_poller(settings, repository)
        _OVERMIND_POLLER_STARTED = True
    if not _PEER_HEALTH_CHECK_THREAD_STARTED:
        _start_peer_health_check_thread(settings)
        _PEER_HEALTH_CHECK_THREAD_STARTED = True
    if not _LOCAL_NETWORK_WORKERS_STARTED:
        _start_local_network_workers(settings)
        _LOCAL_NETWORK_WORKERS_STARTED = True
    if not _AUTOMATION_POLLER_STARTED:
        _start_automation_poller(settings)
        _AUTOMATION_POLLER_STARTED = True
    if settings.edge_enabled and not _EDGE_MUX_STARTED:
        _start_edge_mux_client(settings)
        _EDGE_MUX_STARTED = True
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
