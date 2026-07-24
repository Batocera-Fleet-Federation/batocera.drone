"""OpenAPI contract for the stdlib Drone API routes.

This module intentionally has no FastAPI or Pydantic dependency. The Drone can run on a
plain stdlib path on Batocera, while the optional FastAPI bridge merges these named
schemas into its generated OpenAPI document.
"""

from typing import Any, Dict, Iterable, Optional


Schema = Dict[str, Any]


def _ref(name: str) -> Schema:
    return {"$ref": f"#/components/schemas/{name}"}


def _array(item_schema: Schema) -> Schema:
    return {"type": "array", "items": item_schema}


def _object(
    properties: Optional[Dict[str, Schema]] = None,
    required: Iterable[str] = (),
    *,
    description: Optional[str] = None,
    additional_properties: Any = True,
) -> Schema:
    schema: Schema = {"type": "object", "additionalProperties": additional_properties}
    if description:
        schema["description"] = description
    if properties:
        schema["properties"] = properties
    required_values = list(required)
    if required_values:
        schema["required"] = required_values
    return schema


def _string(description: Optional[str] = None, *, fmt: Optional[str] = None, nullable: bool = False) -> Schema:
    schema: Schema = {"type": "string"}
    if description:
        schema["description"] = description
    if fmt:
        schema["format"] = fmt
    if nullable:
        schema["nullable"] = True
    return schema


def _integer(description: Optional[str] = None, *, default: Optional[int] = None, minimum: Optional[int] = None, maximum: Optional[int] = None, nullable: bool = False) -> Schema:
    schema: Schema = {"type": "integer"}
    if description:
        schema["description"] = description
    if default is not None:
        schema["default"] = default
    if minimum is not None:
        schema["minimum"] = minimum
    if maximum is not None:
        schema["maximum"] = maximum
    if nullable:
        schema["nullable"] = True
    return schema


def _number(description: Optional[str] = None, *, nullable: bool = False) -> Schema:
    schema: Schema = {"type": "number"}
    if description:
        schema["description"] = description
    if nullable:
        schema["nullable"] = True
    return schema


def _boolean(description: Optional[str] = None, *, default: Optional[bool] = None, nullable: bool = False) -> Schema:
    schema: Schema = {"type": "boolean"}
    if description:
        schema["description"] = description
    if default is not None:
        schema["default"] = default
    if nullable:
        schema["nullable"] = True
    return schema


def _enum(values: Iterable[str], description: Optional[str] = None, *, default: Optional[str] = None) -> Schema:
    schema: Schema = {"type": "string", "enum": list(values)}
    if description:
        schema["description"] = description
    if default is not None:
        schema["default"] = default
    return schema


def _json_response(schema_name: str, description: str = "JSON response") -> Schema:
    return {
        "description": description,
        "content": {
            "application/json": {
                "schema": _ref(schema_name),
            }
        },
    }


def _media_response(description: str, media_types: Iterable[str], schema: Optional[Schema] = None) -> Schema:
    payload_schema = schema or {"type": "string", "format": "binary"}
    return {
        "description": description,
        "content": {media_type: {"schema": payload_schema} for media_type in media_types},
    }


def _redirect_response(description: str = "Redirect") -> Schema:
    return {
        "description": description,
        "headers": {
            "Location": {
                "description": "Redirect target",
                "schema": {"type": "string", "format": "uri"},
            }
        },
    }


def _param(name: str, location: str, schema: Schema, *, required: bool = False, description: Optional[str] = None) -> Schema:
    payload: Schema = {"name": name, "in": location, "required": required, "schema": schema}
    if description:
        payload["description"] = description
    return payload


def _path_param(name: str, description: Optional[str] = None) -> Schema:
    return _param(name, "path", _string(), required=True, description=description)


def _query_param(name: str, schema: Schema, description: Optional[str] = None) -> Schema:
    return _param(name, "query", schema, required=False, description=description)


def _json_request(schema_name: str, description: Optional[str] = None, *, required: bool = True) -> Schema:
    payload: Schema = {
        "required": required,
        "content": {"application/json": {"schema": _ref(schema_name)}},
    }
    if description:
        payload["description"] = description
    return payload


def _multipart_request(schema_name: str, description: Optional[str] = None) -> Schema:
    payload: Schema = {
        "required": True,
        "content": {"multipart/form-data": {"schema": _ref(schema_name)}},
    }
    if description:
        payload["description"] = description
    return payload


def _errors(*codes: str) -> Dict[str, Schema]:
    descriptions = {
        "400": "Bad request",
        "401": "Authentication required",
        "403": "Forbidden",
        "404": "Not found",
        "409": "Conflict",
        "429": "Rate limited",
        "500": "Internal server error",
        "502": "Upstream error",
        "503": "Service unavailable",
    }
    return {code: _json_response("ErrorResponse", descriptions.get(code, "Error")) for code in codes}


def _operation(
    summary: str,
    responses: Dict[str, Schema],
    *,
    description: Optional[str] = None,
    parameters: Optional[Iterable[Schema]] = None,
    request_body: Optional[Schema] = None,
    tags: Optional[Iterable[str]] = None,
    security: Optional[Iterable[Schema]] = None,
    servers: Optional[Iterable[Schema]] = None,
    error_codes: Iterable[str] = ("400", "401", "403", "404", "429", "500"),
) -> Schema:
    merged_responses = dict(responses)
    for code, response in _errors(*error_codes).items():
        merged_responses.setdefault(code, response)
    payload: Schema = {"summary": summary, "responses": merged_responses}
    if description:
        payload["description"] = description
    if parameters:
        payload["parameters"] = list(parameters)
    if request_body:
        payload["requestBody"] = request_body
    if tags:
        payload["tags"] = list(tags)
    if security is not None:
        payload["security"] = list(security)
    if servers is not None:
        payload["servers"] = list(servers)
    return payload


def _schemas() -> Dict[str, Schema]:
    freeform = _object(description="Additional route-specific fields may be present.")
    string_map = _object(additional_properties={"type": "string"})
    count_map = _object(additional_properties={"type": "integer"})
    nullable_string = _string(nullable=True)

    asset_entry = _object(
        {
            "entry_type": _enum(["file", "folder"], "Filesystem entry kind"),
            "name": _string("Display or file name"),
            "path": _string("Path relative to the asset root"),
            "relative_path": _string("Path relative to the asset root"),
            "rom_path": _string("ROM path from gamelist metadata"),
            "file_path": _string("Generic relative file path"),
            "unique_id": _string("Stable URL-safe identifier used by download routes"),
            "system": _string("Batocera system key"),
            "byte_count": _integer("File size in bytes"),
            "file_size": _integer("File size in bytes"),
            "modified_time": _integer("Unix file modification time"),
            "modified_at": _string("ISO modification timestamp", fmt="date-time"),
            "md5": _string("MD5 hash when available"),
            "bios_md5": _string("BIOS MD5 hash when available"),
            "fingerprint": _string("Content thumbprint used for synchronization"),
            "rom_fingerprint": _string("ROM content thumbprint"),
            "saves_fingerprint": _string("Save-file content thumbprint"),
            "is_downloadable": _boolean("Whether direct download is allowed"),
            "exists_locally": _boolean("Whether a peer item already exists on this Drone"),
            "gamelist": freeform,
            "artwork_urls": string_map,
            "artwork_types": _array(_string()),
        },
        description="ROM, BIOS, image, video, save, artwork, or config inventory item.",
    )

    download_job = _object(
        {
            "job_id": _string("Download job identifier"),
            "status": _string("Queue status"),
            "file_type": _string("Human-readable asset type"),
            "asset_type": _string("Machine-readable asset type"),
            "system": _string("Batocera system key"),
            "name": _string("Display name"),
            "relative_path": _string("Source or target relative path"),
            "target_path": _string("Local target path"),
            "source_drone_id": _string("Peer Drone identifier"),
            "queued_at": _string(fmt="date-time"),
            "started_at": _string(fmt="date-time"),
            "completed_at": _string(fmt="date-time"),
            "bytes_total": _integer(),
            "bytes_downloaded": _integer(),
            "error": _string(),
        },
        description="Local Network peer-to-peer download job.",
    )

    upload_job = _object(
        {
            "upload_id": _string("Upload identifier"),
            "peer_device_id": _string("Requesting peer Drone identifier"),
            "status": _string("Upload status"),
            "asset_type": _string("Machine-readable asset type"),
            "system": _string("Batocera system key"),
            "relative_path": _string("Source relative path"),
            "file_name": _string("Display file name"),
            "transport": _string("Serving tier: direct or relay"),
            "total_bytes": _integer(),
            "bytes_transferred": _integer(),
            "percentage": _number(),
            "transfer_speed_bps": _number(),
            "started_at": _string(fmt="date-time"),
            "completed_at": _string(fmt="date-time"),
            "error_message": _string(),
        },
        description="An asset this Drone is serving (or recently served) to a peer.",
    )

    certificate_metadata = _object(
        {
            "status": _string("Certificate load/generation status"),
            "subject": _string("Certificate subject"),
            "issuer": _string("Certificate issuer"),
            "serial_number": _string("Certificate serial number"),
            "not_before": _string(fmt="date-time"),
            "not_after": _string(fmt="date-time"),
            "fingerprint": _string("SHA-256 certificate fingerprint"),
            "public_certificate": _string("PEM encoded public certificate"),
            "ca_certificate": _string("PEM encoded CA certificate"),
            "cert_file": _string("Local public certificate path"),
            "key_file_configured": _boolean("Whether a private key exists locally"),
            "days": _integer("Configured certificate lifetime"),
        },
        description="Public certificate metadata. Private key material is never returned.",
    )

    local_peer = _object(
        {
            "drone_id": _string("Peer Drone identifier"),
            "name": _string("Peer display name"),
            "hostname": _string("Peer hostname"),
            "reachable_url": _string("Peer API base URL", fmt="uri"),
            "advertised_reachable_url": _string("Peer-advertised API base URL", fmt="uri"),
            "scheme": _enum(["http", "https"]),
            "api_port": _integer("Peer's browser/admin port"),
            "peer_mtls_port": _integer("Peer's dedicated peer-to-peer mTLS port, used for actual /peer/* traffic"),
            "tailnet_ip": _string("Peer mesh-VPN (tailnet) address, empty when not on a tailnet"),
            "source": _enum(["Tailnet", "Local Network"]),
            "tailnet_device": _boolean("Connected Tailnet device that did not answer as a Drone"),
            "tailnet_forgotten": _boolean("Automatic Tailnet trust was explicitly forgotten"),
            "certificate_fingerprint": _string("Peer certificate SHA-256 fingerprint"),
            "source_ip": _string("Observed source IP"),
            "paired": _boolean(),
            "fake_data": _boolean(),
            "health": freeform,
        },
        description="Local Network peer metadata safe to expose in the admin UI.",
    )

    return {
        "ErrorResponse": _object({"error": _string("Human-readable error message")}, ("error",), description="Error response returned by API routes."),
        "OpenApiDocument": _object(description="OpenAPI 3 document."),
        "HealthResponse": _object(
            {
                "status": _enum(["ok"]),
                "drone_id": _string(),
                "checked_at": _string(fmt="date-time"),
            },
            ("status", "drone_id", "checked_at"),
            description="Public health status.",
        ),
        "SystemSummary": _object(
            {
                "name": _string("Batocera system key"),
                "display_name": _string("Human-readable system name"),
                "rom_count": _integer("Number of ROMs"),
                "is_visible": _boolean("Whether the system is visible in EmulationStation"),
            },
            description="One Batocera system row.",
        ),
        "AssetEntry": asset_entry,
        "SystemsResponse": _object({"systems": _array(_ref("SystemSummary"))}, ("systems",), description="Installed systems visible to the Drone UI."),
        "RomListResponse": _object({"system": _string(), "roms": _array(_ref("AssetEntry"))}, ("system", "roms")),
        "ImageListResponse": _object({"system": _string(), "images": _array(_ref("AssetEntry"))}, ("system", "images")),
        "VideoListResponse": _object({"system": _string(), "videos": _array(_ref("AssetEntry"))}, ("system", "videos")),
        "BiosListResponse": _object(
            {
                "bios": _array(_ref("AssetEntry")),
                "count": _integer(),
                "offset": _integer(),
                "limit": _integer(),
                "returned": _integer(),
                "has_more": _boolean(),
                "systems": _array(_string()),
                "systems_filtered": _array(_string()),
            },
            ("bios", "count", "offset", "limit", "returned", "has_more", "systems", "systems_filtered"),
        ),
        "SearchResponse": _object({"query": _string(), "system": _string(nullable=True), "results": _array(_ref("AssetEntry"))}, ("query", "results")),
        "RomFingerprintResponse": _object(
            {"system": _string(), "unique_id": _string(), "fingerprint": _string(), "cached": _boolean()},
            ("system", "unique_id", "fingerprint", "cached"),
        ),
        "ThemeMetaResponse": _object(
            {
                "enabled": _boolean(),
                "theme_name": _string(),
                "theme_dir": _string(),
                "selected_theme_name": _string(nullable=True),
                "theme_sources": freeform,
                "themes_root": _string(),
                "es_settings_file": _string(nullable=True),
                "api": freeform,
                "ui": freeform,
                "css_url": _string(nullable=True),
                "background_url": _string(nullable=True),
                "logo_url": _string(nullable=True),
                "resolved_files": freeform,
                "reason": _string(),
            },
            ("enabled",),
        ),
        "SystemThemeMetaResponse": _object(
            {
                "enabled": _boolean(),
                "system": _string(),
                "reason": _string(),
                "theme_name": _string(),
                "system_theme_dir": _string(),
                "theme_xml_url": _string(nullable=True),
                "css_url": _string(nullable=True),
                "background_url": _string(nullable=True),
                "logo_url": _string(nullable=True),
                "resolved_files": freeform,
            },
            ("enabled", "system"),
        ),
        "ThemeBackgroundsResponse": _object(
            {"enabled": _boolean(), "theme_name": _string(nullable=True), "count": _integer(), "backgrounds": _array(_string()), "cache_seconds": _integer()},
            ("enabled", "count", "backgrounds", "cache_seconds"),
        ),
        "ThemeLogosResponse": _object(
            {"enabled": _boolean(), "theme_name": _string(nullable=True), "count": _integer(), "logos": _array(_string()), "cache_seconds": _integer()},
            ("enabled", "count", "logos", "cache_seconds"),
        ),
        "ThemeImage": _object({"path": _string(), "folder": _string(), "name": _string(), "url": _string()}),
        "ThemeImagesResponse": _object(
            {
                "enabled": _boolean(),
                "theme_name": _string(nullable=True),
                "systems": _array(_string()),
                "count": _integer(),
                "offset": _integer(),
                "limit": _integer(),
                "returned": _integer(),
                "has_more": _boolean(),
                "images": _array(_ref("ThemeImage")),
            },
            ("enabled", "count", "images"),
        ),
        "AdminLogResponse": _object(
            {"source": _string(), "path": _string(), "lines": _integer(), "content": _string(), "attempted_paths": _array(_string()), "searched_roots": _array(_string())},
            description="Log tail or a not-found diagnostic.",
        ),
        "GameplayLogsResponse": _object(
            {
                "type": _enum(["game_logs"]),
                "collected_at": _string(fmt="date-time"),
                "sessions": _array(freeform),
                "logs": _array(freeform),
                "pending_spool_events": _integer(),
            },
            ("type", "collected_at", "sessions", "logs", "pending_spool_events"),
        ),
        "SystemInfoEntry": _object({"key": _string(), "value": _string()}, ("key", "value")),
        "SpeedSample": _object({"upload_mbps": _number(nullable=True), "download_mbps": _number(nullable=True), "latency_ms": _number(nullable=True), "source": _string(), "sampled_at": _string(fmt="date-time")}),
        "SystemInfoResponse": _object(
            {
                "raw": _string(),
                "lines": _array(_string()),
                "entries": _array(_ref("SystemInfoEntry")),
                "fields": string_map,
                "drone_app_version": _string(),
                "audio_volume": _integer(nullable=True, minimum=0, maximum=100),
                "runtime_metrics": freeform,
                "speed_sample": _ref("SpeedSample"),
                "warning": _string(),
            },
            ("raw", "lines", "entries", "fields", "drone_app_version", "runtime_metrics", "speed_sample"),
        ),
        "SystemVolumeUpdateRequest": _object(
            {"level": _integer(minimum=0, maximum=100)},
            ("level",),
            description="Volume level in increments of 5.",
        ),
        "SystemVolumeResponse": _object(
            {"audio_volume": _integer(minimum=0, maximum=100)},
            ("audio_volume",),
        ),
        "ScreenModeResponse": _object({"screen_mode": _string(nullable=True)}),
        "ScreenModeUpdateRequest": _object(
            {"mode": _string()},
            ("mode",),
            description="One of: full, kiosk, kid. Applying this restarts EmulationStation.",
        ),
        "ScreenModeUpdateResponse": _object(
            {"screen_mode": _string(), "emulationstation_restarted": _boolean()},
            ("screen_mode", "emulationstation_restarted"),
        ),
        "MusicVolumeUpdateRequest": _object(
            {"level": _integer(minimum=0, maximum=100)},
            ("level",),
            description="Music volume level. Applies live, no EmulationStation restart.",
        ),
        "EsSystemEntry": _object({"name": _string(), "full_name": _string(), "displayed": _boolean()}),
        "EsGroupChild": _object({"name": _string(), "full_name": _string(), "grouped": _boolean()}),
        "EsSystemGroup": _object({"group": _string(), "children": _array(_ref("EsGroupChild"))}),
        "EsAutoCollection": _object({"name": _string(), "label": _string(), "enabled": _boolean()}),
        "EsCustomCollection": _object({"name": _string(), "enabled": _boolean()}),
        "EsCollectionsState": _object(
            {
                "music_volume": _integer(minimum=0, maximum=100),
                "screensaver_minutes": _integer(minimum=0, maximum=120, description="Idle minutes before the screensaver starts; 0 = disabled."),
                "systems": _array(_ref("EsSystemEntry")),
                "groups": _array(_ref("EsSystemGroup")),
                "auto_collections": _array(_ref("EsAutoCollection")),
                "custom_collections": _array(_ref("EsCustomCollection")),
            },
            description="Current EmulationStation systems-displayed / grouped-systems / collections / music volume / screensaver configuration.",
        ),
        "EsCollectionsUpdateRequest": _object(
            {
                "music_volume": _integer(minimum=0, maximum=100),
                "screensaver_minutes": _integer(minimum=0, maximum=120),
                "hidden_systems": _array(_string()),
                "ungrouped_systems": _array(_string()),
                "auto_collections": _array(_string()),
                "custom_collections": _array(_string()),
            },
            description="Partial update: each field is optional and, when present, replaces that setting's FULL desired value/list (not a diff). Applying these startup-only EmulationStation settings restarts EmulationStation.",
        ),
        "DownloadJob": download_job,
        "AdminDownloadsResponse": _object(
            {
                "target_drone_id": _string(),
                "downloads": _array(_ref("DownloadJob")),
                "active": _array(_ref("DownloadJob")),
                "queued": _array(_ref("DownloadJob")),
                "recent": _array(_ref("DownloadJob")),
                "paused": _boolean(),
            },
            description="Download queue snapshot.",
        ),
        "DownloadActionResponse": _object(
            {"status": _string(), "job": _ref("DownloadJob"), "job_id": _string(), "message": _string(), "downloads": _array(_ref("DownloadJob"))},
            description="Download queue mutation result.",
        ),
        "UploadJob": upload_job,
        "AdminUploadsResponse": _object(
            {
                "target_drone_id": _string(),
                "active": _array(_ref("UploadJob")),
                "recent": _array(_ref("UploadJob")),
            },
            description="Upload activity snapshot: assets currently being served to peers, plus recently finished sends.",
        ),
        "AssetCacheResponse": _object(
            {
                "path": _string(),
                "schema_version": _integer(),
                "rebuilt": _boolean(),
                "active": _boolean(),
                "poller_enabled": _boolean(),
                "poll_seconds": _integer(),
                "watch_enabled": _boolean(),
                "watch_active": _boolean(),
                "rom_hashing_enabled": _boolean(),
                "initial_delay_seconds": _integer(),
                "complete": _boolean(),
                "uploaded": _boolean(),
                "needs_upload": _boolean(),
                "dirty": _boolean(),
                "full_refresh_pending": _boolean(),
                "scan_in_progress": _boolean(),
                "last_full_scan_at": _string(fmt="date-time", nullable=True),
                "last_successful_upload_at": _string(fmt="date-time", nullable=True),
                "scan_checkpoint_at": _string(fmt="date-time", nullable=True),
                "counts": count_map,
                "pending_changes": count_map,
            },
            description="ROM, BIOS, and artwork metadata cache status.",
        ),
        "AssetCachePurgeResponse": _object(
            {"status": _string(), "kept_fingerprint": _boolean(), "cleared": count_map, "requested_at": _string(fmt="date-time"), "message": _string()},
            ("status", "kept_fingerprint", "cleared", "message"),
        ),
        "AssetCacheClearPendingResponse": _object(
            {"status": _string(), "cleared": count_map, "pending_changes": count_map, "message": _string()},
            ("status", "cleared", "pending_changes", "message"),
        ),
        "CertificateMetadata": certificate_metadata,
        "ApiAdminStatusResponse": _object(
            {
                "swagger_url": _string(fmt="uri"),
                "openapi_url": _string(fmt="uri"),
                "certificate_download_url": _string(fmt="uri"),
                "mtls_enabled": _boolean(),
                "certificate": _ref("CertificateMetadata"),
                "guidance": _object({"curl": _string(), "warning": _string(), "lifecycle": _string()}),
            },
            ("swagger_url", "openapi_url", "certificate_download_url", "mtls_enabled", "certificate", "guidance"),
        ),
        "IdleVolumeConfig": _object({"enabled": _boolean(), "idle_minutes": _integer(), "target_volume": _integer()}),
        "IdleGameExitConfig": _object({"enabled": _boolean(), "idle_minutes": _integer()}),
        "WifiRecoveryConfig": _object({"enabled": _boolean()}),
        "WifiRecoveryStatus": _object({
            "last_check_epoch": _number(nullable=True),
            "last_recovery_epoch": _number(nullable=True),
            "wifi_enabled": _boolean(nullable=True),
            "wifi_connected": _boolean(),
            "wireless_interfaces": _array(_string()),
            "last_error": _string(nullable=True),
        }),
        "InputMonitorStatus": _object({"available": _boolean(), "idle_seconds": _integer(nullable=True), "last_activity_epoch": _number(nullable=True)}),
        "AutomationStatusResponse": _object({
            "idle_volume": _ref("IdleVolumeConfig"),
            "idle_game_exit": _ref("IdleGameExitConfig"),
            "wifi_recovery": _ref("WifiRecoveryConfig"),
            "wifi_status": _ref("WifiRecoveryStatus"),
            "input_monitor": _ref("InputMonitorStatus"),
            "current_volume": _integer(nullable=True),
            "game_running": _boolean(),
        }, ("idle_volume", "idle_game_exit", "wifi_recovery", "wifi_status", "input_monitor", "current_volume", "game_running")),
        "IdleVolumeUpdateRequest": _object({"enabled": _boolean(), "idle_minutes": _integer(), "target_volume": _integer()}),
        "IdleVolumeResponse": _object({"idle_volume": _ref("IdleVolumeConfig")}, ("idle_volume",)),
        "IdleGameExitUpdateRequest": _object({"enabled": _boolean(), "idle_minutes": _integer()}),
        "IdleGameExitResponse": _object({"idle_game_exit": _ref("IdleGameExitConfig")}, ("idle_game_exit",)),
        "WifiRecoveryUpdateRequest": _object({"enabled": _boolean()}, ("enabled",)),
        "WifiRecoveryResponse": _object({"wifi_recovery": _ref("WifiRecoveryConfig")}, ("wifi_recovery",)),
        "ArtworkMissingResponse": _object(
            {
                "roms": _array(freeform),
                "count": _integer(),
                "returned": _integer(),
                "limit": _integer(),
                "offset": _integer(),
                "has_more": _boolean(),
                "systems": _array(_string()),
                "systems_filtered": _array(_string()),
                "fields": _array(_string()),
                "field_counts": count_map,
                "selected_fields": _array(_string()),
                "selected_systems": _array(_string()),
                "rom_status": _enum(["any", "exists", "missing"]),
                "query": _string(),
                "mode": _enum(["filesystem", "gamelist"]),
                "show_all": _boolean(),
                "cached": _boolean(),
                "elapsed_ms": _integer(),
            },
            ("roms", "count", "returned", "limit", "offset", "has_more", "fields"),
        ),
        "ArtworkSearchResponse": _object(
            {
                "query": _string(),
                "system": _string(),
                "launchbox_platform": _string(),
                "mobygames_platform": _string(),
                "rom_id": _string(),
                "rom_path": _string(),
                "matches": _array(freeform),
                "configured": _boolean(),
                "message": _string(),
                "fields": _array(_string()),
            },
            ("query", "system", "rom_id", "rom_path", "matches"),
        ),
        "ArtworkApplyRequest": _object(
            {
                "system": _string(),
                "rom_id": _string(),
                "unique_id": _string(),
                "rom_path": _string(),
                "game_key": _string(),
                "game_id": _string(),
                "override_existing": _boolean(default=False),
                "import_metadata": _boolean(default=True),
            },
            description="Apply selected artwork from a scraper provider.",
        ),
        "ArtworkApplyResponse": _object(
            {"updated": _array(freeform), "missing": _array(_string()), "existing": freeform, "override_existing": _boolean(), "metadata_imported": _integer(), "source": _string()},
            description="Artwork import result.",
        ),
        "ArtworkUploadRequest": _object(
            {
                "file": {"type": "string", "format": "binary"},
                "field": _string("Artwork field to update"),
                "system": _string(),
                "rom_id": _string(),
                "rom_path": _string(),
            },
            ("file", "field", "system"),
            additional_properties=False,
        ),
        "ArtworkUploadResponse": _object(
            {
                "rom_name": _string(),
                "field": _string(),
                "path": _string(),
                "relative_path": _string(),
                "url": _string(fmt="uri"),
                "existing": string_map,
                "missing": _array(_string()),
                "gamelist": freeform,
                "has_gamelist_entry": _boolean(),
            },
            ("rom_name", "field", "path", "relative_path", "url", "existing", "missing", "has_gamelist_entry"),
        ),
        "GamelistRemoveRequest": _object({"system": _string(), "rom_path": _string()}, ("system", "rom_path")),
        "GamelistUpdateRequest": _object({"system": _string(), "rom_path": _string(), "fields": freeform}, ("system", "rom_path", "fields")),
        "GamelistRemoveMissingRequest": _object({"confirm": _enum(["DELETE_MISSING_GAMELIST_ENTRIES"]), "include_filesystem": _boolean(), "fields": _array(_string()), "systems": _array(_string()), "q": _string()}, ("confirm",)),
        "GamelistMutationResponse": _object(
            {"status": _string(), "removed": _integer(), "updated": _integer(), "matched_count": _integer(), "entry": freeform, "entries": _array(freeform)},
            description="Gamelist mutation result.",
        ),
        "CertificateRotateResponse": _object({"status": _enum(["rotated", "failed"]), "error": _string(), "certificate": _ref("CertificateMetadata")}, ("status", "certificate")),
        "DroneUpdateResponse": _object({"status": _string(), "version": _string(), "archive_url": _string(fmt="uri"), "elapsed_seconds": _number(), "restart": freeform}, description="Self-update result plus restart metadata."),
        "DroneAutoUpdateRequest": _object({"enabled": _boolean()}, ("enabled",)),
        "DroneAutoUpdateResponse": _object({"enabled": _boolean()}, ("enabled",)),
        "PixnUpdateResponse": _object({"type": _string(), "status": _string(), "pid": _integer(nullable=True), "script": _string()}, ("type", "status", "script"), description="PixN upgrade script launch result."),
        "CredentialsUpdateRequest": _object({"username": _string(), "password": _string()}, ("username", "password")),
        "CredentialsUpdateResponse": _object({"credentials": freeform, "message": _string()}, ("credentials", "message")),
        "NetworkModeResponse": _object(
            {
                "mode": _enum(["local_network"]),
                "local_network_active": _boolean(),
                "local_network_enabled": _boolean(),
                "modes": _array(_string()),
            },
            ("mode", "local_network_active", "local_network_enabled", "modes"),
        ),
        "NetworkModeUpdateRequest": _object({"mode": _enum(["local_network"]), "local_network_enabled": _boolean()}),
        "LocalPeer": local_peer,
        "PairingInfo": _object({"code": _string(), "expires_at": _string(fmt="date-time"), "ttl_seconds": _integer()}),
        "LocalNetworkStatusResponse": _object(
            {
                "mode": _string(),
                "active": _boolean(),
                "pairing": _ref("PairingInfo"),
                "peers": _array(_ref("LocalPeer")),
                "paired_count": _integer(),
                "discovered_count": _integer(),
                "downloads": _ref("AdminDownloadsResponse"),
                "activity": _array(freeform),
                "announcement_sent": _boolean(),
            },
            ("mode", "active", "pairing", "peers", "paired_count", "discovered_count", "downloads", "activity"),
        ),
        "PairingCodeResponse": _object({"pairing": _ref("PairingInfo")}, ("pairing",)),
        "LocalPeerPairRequest": _object({"pairing_code": _string()}, ("pairing_code",)),
        "LocalPeerPairByAddressRequest": _object(
            {
                "address": _string(description="Peer address: host[:port] or http(s)://host[:port]; e.g. a tailnet IP"),
                "pairing_code": _string(),
            },
            ("address", "pairing_code"),
        ),
        "LocalPeerPairResponse": _object({"status": _enum(["paired"]), "peer": _ref("LocalPeer")}, ("status", "peer")),
        "SwarmDroneEntry": _object(
            {
                "drone_id": _string(),
                "name": _string(),
                "hostname": _string(),
                "is_self": _boolean(),
                "online": _boolean(),
                "paired": _boolean(),
                "reachable_url": _string(fmt="uri"),
                "advertised_reachable_url": _string(fmt="uri"),
                "tailnet_ip": _string(description="Mesh-VPN (tailnet) address, empty when not on a tailnet"),
                "ui_url": _string(description="Best URL for the viewer's browser to open this drone's UI; empty for the drone serving the page"),
                "error": _string(nullable=True),
                "latency_ms": _integer(nullable=True),
                "summary": freeform,
            },
            ("drone_id", "name", "is_self", "online", "paired"),
        ),
        "SwarmOverviewResponse": _object(
            {
                "active": _boolean(description="Whether Local Network mode (the pairing/trust layer the swarm view is built on) is enabled"),
                "generated_at": _string(fmt="date-time"),
                "drones": _array(_ref("SwarmDroneEntry")),
            },
            ("active", "generated_at", "drones"),
        ),
        "TailnetStatusResponse": _object(
            {
                "installed": _boolean(description="tailscale binaries present under /userdata/system/tailscale"),
                "running": _boolean(description="tailscaled answers on its control socket"),
                "enrolled": _boolean(description="this drone holds a node key and is (re)connecting to the tailnet"),
                "tailnet_ip": _string(),
                "hostname": _string(),
                "backend_state": _string(description="raw tailscale BackendState, e.g. Running / NeedsLogin"),
                "version": _string(description="installed Tailscale version reported by tailscaled"),
                "dns_name": _string(description="this Drone's Tailscale DNS name, when available"),
                "tailnet_name": _string(description="current Tailnet name, when available"),
                "magic_dns_suffix": _string(description="Tailnet MagicDNS suffix, when available"),
                "relay": _string(description="preferred DERP relay region code, when available"),
                "health": _array(_string(description="Tailscale health warning")),
                "peers": _array(_ref("TailnetPeer")),
            },
            ("installed", "running", "enrolled", "tailnet_ip", "hostname"),
        ),
        "TailnetPeer": _object(
            {
                "tailnet_id": _string(),
                "name": _string(),
                "hostname": _string(),
                "dns_name": _string(),
                "tailnet_ip": _string(),
                "addresses": _array(_string()),
                "last_seen": _string(description="Tailscale last-seen timestamp; empty when unavailable"),
                "os": _string(),
                "online": _boolean(),
            },
            ("tailnet_id", "name", "tailnet_ip", "addresses", "online"),
        ),
        "TailnetDiscoveryResponse": _object(
            {"tailnet": _ref("TailnetStatusResponse"), "network": _ref("LocalNetworkStatusResponse")},
            ("tailnet", "network"),
        ),
        "TailnetEnrollRequest": _object(
            {"auth_key": _string(description="Tailscale auth key (tskey-auth-...) from https://login.tailscale.com/admin/settings/keys")},
            ("auth_key",),
        ),
        "RemoteProxyResponse": _object(
            description="Relayed verbatim from the proxied peer route -- its shape matches whatever that route's own documented response is; this generic passthrough has no fixed schema of its own."
        ),
        "RemoteStatusResponse": _object(
            {
                "connected": _boolean(description="true when a cached remote-admin session already exists for this peer"),
                "peer_id": _string(),
                "name": _string(),
            },
            ("connected", "peer_id", "name"),
        ),
        "RemoteConnectRequest": _object(
            {
                "peer_id": _string(description="A paired peer's drone_id"),
                "username": _string(description="That peer's own Drone login username"),
                "password": _string(description="That peer's own Drone login password"),
            },
            ("peer_id", "username", "password"),
        ),
        "RemoteConnectResponse": _object(
            {
                "status": _enum(["connected"]),
                "peer_id": _string(),
                "name": _string(),
                "drone_app_version": _string(description="The peer's reported Drone app version, when available"),
            },
            ("status", "peer_id", "name"),
        ),
        "RemoteDisconnectRequest": _object({"peer_id": _string()}, ("peer_id",)),
        "RemoteDisconnectResponse": _object({"status": _enum(["disconnected"]), "peer_id": _string()}, ("status", "peer_id")),
        "LocalPeerForgetResponse": _object({"status": _enum(["forgotten", "not_found"]), "peer_id": _string()}, ("status", "peer_id")),
        "LocalSyncRequest": _object(
            {
                "peer_id": _string(),
                "asset_type": _enum(["roms", "bios", "artwork", "saves"]),
                "system": _string(),
                "item": _ref("AssetEntry"),
                "include_artwork": _boolean(default=True),
                "include_roms": _boolean(default=True),
                "overwrite_files": _boolean(default=False),
            },
            ("peer_id", "asset_type"),
        ),
        "LocalSyncResponse": _object(
            {"status": _enum(["queued"]), "job": _ref("DownloadJob"), "jobs": _array(_ref("DownloadJob")), "rom_skipped": _boolean(), "rom_absent": _boolean()},
            ("status", "jobs", "rom_skipped", "rom_absent"),
        ),
        "LocalBulkSyncRequest": _object(
            {
                "peer_id": _string(),
                "asset_type": _enum(["roms", "bios", "artwork", "saves"]),
                "system": _string(),
                "systems": _array(_string()),
                "q": _string(),
                "include_artwork": _boolean(default=True),
                "include_roms": _boolean(default=True),
                "overwrite_files": _boolean(default=False),
            },
            ("peer_id", "asset_type"),
        ),
        "LocalBulkSyncResponse": _object(
            {"status": _enum(["queued"]), "asset_type": _string(), "system": _string(nullable=True), "systems": _array(_string()), "queued_assets": _integer(), "queued_artwork": _integer(), "skipped_existing": _integer(), "total_available": _integer()},
            ("status", "asset_type", "systems", "queued_assets", "queued_artwork", "skipped_existing", "total_available"),
        ),
        "PeerPairRequest": _object(
            {
                "pairing_code": _string(),
                "tailnet_auto_pair": _boolean(description="Request code-free pairing authorized by both Drones' current Tailnet membership"),
                "drone_id": _string(),
                "name": _string(),
                "hostname": _string(),
                "scheme": _enum(["http", "https"]),
                "api_port": _integer("Initiator's browser/admin port"),
                "peer_mtls_port": _integer("Initiator's dedicated peer-to-peer mTLS port"),
                "reachable_url": _string(fmt="uri"),
                "tailnet_ip": _string(description="Initiator's mesh-VPN (tailnet) address, empty when not on a tailnet"),
                "certificate_pem": _string(),
                "certificate_fingerprint": _string(),
            },
            ("pairing_code", "drone_id", "certificate_pem"),
        ),
        "PeerPairResponse": _object(
            {
                "status": _enum(["paired"]),
                "peer": _ref("LocalPeer"),
                "drone_id": _string(),
                "name": _string(),
                "scheme": _enum(["http", "https"]),
                "api_port": _integer("Responder's browser/admin port"),
                "peer_mtls_port": _integer("Responder's dedicated peer-to-peer mTLS port"),
                "reachable_url": _string(fmt="uri"),
                "tailnet_ip": _string(description="Responder's mesh-VPN (tailnet) address, empty when not on a tailnet"),
                "certificate_pem": _string(),
                "certificate_fingerprint": _string(),
            },
            ("status", "peer", "drone_id", "name", "scheme", "api_port", "certificate_pem", "certificate_fingerprint"),
        ),
        "PeerInfoResponse": _object(
            {
                "service": _string(),
                "kind": _string(),
                "drone_id": _string(),
                "name": _string(),
                "hostname": _string(),
                "scheme": _enum(["http", "https"]),
                "api_port": _integer("Browser/admin port"),
                "peer_mtls_port": _integer("Dedicated peer-to-peer mTLS port"),
                "reachable_url": _string(fmt="uri"),
                "tailnet_ip": _string(description="Mesh-VPN (tailnet) address, empty when not on a tailnet"),
                "certificate_fingerprint": _string(),
                "sent_at": _string(fmt="date-time"),
            },
            ("service", "drone_id", "name", "scheme", "api_port", "reachable_url", "certificate_fingerprint"),
        ),
        "PeerHealthResponse": _object(
            {"status": _enum(["ok"]), "drone_id": _string(), "checked_at": _string(fmt="date-time"), "mtls": _boolean(), "network_mode": _string()},
            ("status", "drone_id", "checked_at", "mtls", "network_mode"),
        ),
        "PeerInventorySummaryResponse": _object(
            {"drone_id": _string(), "name": _string(), "systems": _array(_string()), "system_counts": count_map, "counts": count_map, "updated_at": _string(fmt="date-time")},
            ("drone_id", "name", "systems", "system_counts", "counts", "updated_at"),
        ),
        "PeerInventoryResponse": _object(
            {
                "drone_id": _string(),
                "asset_type": _enum(["roms", "bios", "artwork", "saves", "emulator_configs", "gameplay"]),
                "system": _string(nullable=True),
                "systems": _array(_string()),
                "total": _integer(),
                "limit": _integer(),
                "offset": _integer(),
                "items": _array(_ref("AssetEntry")),
            },
            ("drone_id", "asset_type", "systems", "total", "limit", "offset", "items"),
        ),
        "PeerInventoryEnvelope": {
            "oneOf": [_ref("PeerInventorySummaryResponse"), _ref("PeerInventoryResponse")],
            "description": "Peer inventory summary or paged asset inventory, depending on asset_type.",
        },
        "PeerRomManifestFile": _object({"relative_path": _string(), "file_size": _integer(), "modified_time": _integer()}, ("relative_path", "file_size", "modified_time")),
        "PeerRomManifestResponse": _object(
            {
                "system": _string(),
                "relative_path": _string(),
                "entry_type": _enum(["folder"]),
                "file_count": _integer(),
                "file_size": _integer(),
                "modified_time": _integer(),
                "directories": _array(_string()),
                "files": _array(_ref("PeerRomManifestFile")),
            },
            ("system", "relative_path", "entry_type", "file_count", "file_size", "modified_time", "directories", "files"),
        ),
        "ConfigFileResponse": _object(
            {
                "source": _string(),
                "path": _string(),
                "type": _enum(["file", "directory", "json", "xml"]),
                "format": _enum(["json", "xml"]),
                "max_bytes": _integer(),
                "truncated": _boolean(),
                "content": _array(_string()),
                "parsed": freeform,
                "attempted_paths": _array(_string()),
            },
            description="Config file content, directory listing, parsed es_systems JSON, or not-found diagnostic.",
        ),
        "ConfigSourcesResponse": _object({"sources": _array(_string()), "versions": _object(additional_properties={"type": "string", "nullable": True}), "scan_root": _string()}, ("sources", "versions", "scan_root")),
        "EmulatorConfigFile": _object({"name": _string(), "root_name": _string(), "relative_path": _string(), "size": _integer(), "modified_at": _string(fmt="date-time"), "fingerprint": _string(), "error": _string()}),
        "EmulatorsResponse": _object({"type": _enum(["emulator_configs"]), "configs": _array(_ref("EmulatorConfigFile")), "count": _integer(), "max_configs": _integer(), "incremental": _boolean()}, description="Detected emulator config files exposed to the admin UI and paired peers."),
        "EmulatorFileResponse": _object({"root_name": _string(), "relative_path": _string(), "path": _string(), "size": _integer(), "truncated": _boolean(), "content": _string(), "fingerprint": _string()}, description="One emulator config file content."),
    }


def build_openapi_spec(version: str, api_prefix: str = "/v1/api") -> Dict[str, Any]:
    common_paging = [
        _query_param("limit", _integer(default=100, minimum=1, maximum=5000), "Maximum rows to return"),
        _query_param("offset", _integer(default=0, minimum=0), "Zero-based row offset"),
        _query_param("q", _string(), "Case-insensitive search query"),
    ]
    system_filter_params = [
        _query_param("system", _string(), "Single system filter"),
        _query_param("systems", _string(), "Comma-separated list of system filters, for example snes,ps2,_root"),
    ]
    peer_inventory_params = [
        _query_param("type", _enum(["summary", "roms", "bios", "artwork", "saves", "emulator_configs", "gameplay"], default="summary"), "Peer asset type"),
        *common_paging,
        *system_filter_params,
    ]
    peer_security = [{"mutualTLS": []}]

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Drone App",
            "version": version,
            "description": (
                "Browse and download ROM, image, video, BIOS, save, artwork, and admin assets. "
                "JSON routes are documented with named response schemas. Peer API file-transfer routes "
                "remain binary streams and can require mTLS or paired Local Network certificates -- only "
                "the dedicated peer-mTLS listener (default port 8543, DRONE_PEER_MTLS_PORT) requests a "
                "client certificate at all; the main browser/admin port never does. For manual health "
                "testing use a client certificate/key with curl against that port, for example: "
                "curl --cert client.crt --key client.key -k https://drone-host:8543/health. The admin API "
                "page exposes certificate metadata and the public certificate only; private key material "
                "must stay on the Drone."
            ),
        },
        "servers": [{"url": api_prefix}],
        "components": {
            "securitySchemes": {
                "basicAuth": {"type": "http", "scheme": "basic"},
                "mutualTLS": {
                    "type": "mutualTLS",
                    "description": "Used by peer routes when Drone mTLS or Local Network certificate pairing is enabled.",
                },
            },
            "schemas": _schemas(),
        },
        "security": [{"basicAuth": []}],
        "paths": {
            "/": {
                "get": _operation(
                    "Root UI",
                    {"200": _media_response("HTML UI", ["text/html"], {"type": "string"})},
                    tags=["ui"],
                    error_codes=("401", "403", "429", "500"),
                )
            },
            "/health": {
                "get": _operation(
                    "Public process health",
                    {"200": _json_response("HealthResponse", "Health status")},
                    tags=["health"],
                    security=[],
                    servers=[{"url": "/"}],
                    error_codes=("429", "500"),
                )
            },
            "/systems": {"get": _operation("List systems", {"200": _json_response("SystemsResponse", "Systems list")}, tags=["library"])},
            "/systems/{system}": {
                "get": _operation(
                    "List ROMs for a system",
                    {"200": _json_response("RomListResponse", "ROM list")},
                    parameters=[_path_param("system", "Batocera system key")],
                    tags=["library"],
                )
            },
            "/systems/{system}/roms/{unique_id}": {
                "get": _operation(
                    "Download ROM by unique ID",
                    {"200": _media_response("ROM file stream", ["application/octet-stream"])},
                    parameters=[_path_param("system"), _path_param("unique_id", "ROM unique_id from the ROM list")],
                    tags=["downloads"],
                )
            },
            "/systems/{system}/{unique_id}": {
                "get": _operation(
                    "Download ROM by unique ID (legacy route)",
                    {"200": _media_response("ROM file stream", ["application/octet-stream"])},
                    parameters=[_path_param("system"), _path_param("unique_id", "ROM unique_id from the ROM list")],
                    tags=["downloads"],
                )
            },
            "/systems/{system}/roms/{unique_id}/fingerprint": {
                "get": _operation(
                    "Get ROM content fingerprint",
                    {"200": _json_response("RomFingerprintResponse", "ROM fingerprint")},
                    parameters=[_path_param("system"), _path_param("unique_id")],
                    tags=["library"],
                )
            },
            "/systems/{system}/images": {
                "get": _operation(
                    "List images for a system",
                    {"200": _json_response("ImageListResponse", "Image list")},
                    parameters=[_path_param("system")],
                    tags=["library"],
                )
            },
            "/systems/{system}/images/{image_ref}": {
                "get": _operation(
                    "Get image or download image asset by reference",
                    {
                        "200": _media_response("Image bytes or attachment", ["image/png", "image/jpeg", "image/webp", "image/gif", "application/octet-stream"]),
                        "302": _redirect_response("Redirect to fake-data image provider when fake data is enabled"),
                    },
                    parameters=[_path_param("system"), _path_param("image_ref", "Image file name or image asset unique_id")],
                    tags=["downloads"],
                )
            },
            "/public/systems/{system}/images/{image_file}": {
                "get": _operation(
                    "Public image endpoint",
                    {
                        "200": _media_response("Image bytes", ["image/png", "image/jpeg", "image/webp", "image/gif"]),
                        "302": _redirect_response("Redirect to fake-data image provider when fake data is enabled"),
                    },
                    parameters=[_path_param("system"), _path_param("image_file")],
                    tags=["public"],
                    security=[],
                    error_codes=("400", "404", "429", "500"),
                )
            },
            "/public/systems/{system}/video/{rom_path}": {
                "get": _operation(
                    "Public per-ROM gamelist video endpoint",
                    {
                        "200": _media_response("Video bytes", ["video/mp4", "video/webm", "video/x-matroska", "video/quicktime", "video/x-msvideo"]),
                    },
                    parameters=[_path_param("system"), _path_param("rom_path", "ROM path used to resolve this game's gamelist <video> entry. URL-encode slash-separated paths.")],
                    tags=["public"],
                    security=[],
                    error_codes=("400", "404", "429", "500"),
                )
            },
            "/systems/{system}/videos": {
                "get": _operation("List videos for a system", {"200": _json_response("VideoListResponse", "Video list")}, parameters=[_path_param("system")], tags=["library"])
            },
            "/systems/{system}/videos/{unique_id}": {
                "get": _operation(
                    "Download video by unique ID",
                    {"200": _media_response("Video file stream", ["application/octet-stream", "video/mp4"])},
                    parameters=[_path_param("system"), _path_param("unique_id")],
                    tags=["downloads"],
                )
            },
            "/bios": {
                "get": _operation(
                    "List BIOS entries",
                    {"200": _json_response("BiosListResponse", "Paged BIOS list")},
                    parameters=[*common_paging, _query_param("systems", _string(), "Comma-separated BIOS folder filters, for example ps2,_root")],
                    tags=["library"],
                )
            },
            "/bios/{unique_id}": {
                "get": _operation(
                    "Download BIOS file by unique ID",
                    {"200": _media_response("BIOS file stream", ["application/octet-stream"])},
                    parameters=[_path_param("unique_id")],
                    tags=["downloads"],
                )
            },
            "/openapi.json": {
                "get": _operation("OpenAPI spec", {"200": _json_response("OpenApiDocument", "OpenAPI JSON")}, tags=["meta"], error_codes=("401", "403", "429", "500"))
            },
            "/swagger": {
                "get": _operation("Swagger UI", {"200": _media_response("Swagger HTML", ["text/html"], {"type": "string"})}, tags=["meta"], error_codes=("401", "403", "429", "500"))
            },
            "/downloads": {
                "get": _operation("HTML sitemap of downloadable ROM links grouped by system", {"200": _media_response("Download sitemap HTML", ["text/html"], {"type": "string"})}, tags=["downloads"])
            },
            "/search": {
                "get": _operation(
                    "Search ROMs across all systems",
                    {"200": _json_response("SearchResponse", "Search results")},
                    parameters=[_query_param("q", _string(), "Required search query"), _query_param("system", _string(), "Optional system filter")],
                    tags=["library"],
                )
            },
            "/theme/meta": {"get": _operation("Detected Batocera theme metadata", {"200": _json_response("ThemeMetaResponse", "Theme metadata")}, tags=["theme"])},
            "/theme/assets/{path}": {
                "get": _operation(
                    "Serve asset from detected Batocera theme directory",
                    {
                        "200": _media_response("Theme asset bytes", ["text/css", "image/svg+xml", "image/png", "image/jpeg", "image/webp", "image/gif", "application/octet-stream"]),
                        "302": _redirect_response("Redirect to fake-data image provider when fake data is enabled"),
                    },
                    parameters=[_path_param("path", "Theme-relative path. URL-encode slashes for clients that cannot preserve path segments.")],
                    tags=["theme"],
                )
            },
            "/theme/system/{system}": {
                "get": _operation("Resolved theme metadata for a system", {"200": _json_response("SystemThemeMetaResponse", "System theme metadata")}, parameters=[_path_param("system")], tags=["theme"])
            },
            "/theme/backgrounds": {"get": _operation("List candidate background images from active Batocera theme", {"200": _json_response("ThemeBackgroundsResponse", "Theme background candidates")}, tags=["theme"])},
            "/theme/logos": {"get": _operation("List candidate logo images from active Batocera theme", {"200": _json_response("ThemeLogosResponse", "Theme logo candidates")}, tags=["theme"])},
            "/theme/images": {
                "get": _operation(
                    "List all image assets from active Batocera theme",
                    {"200": _json_response("ThemeImagesResponse", "Paged theme image catalog")},
                    parameters=[*common_paging, *system_filter_params],
                    tags=["theme"],
                )
            },
            "/admin/logs/{source}": {
                "get": _operation(
                    "Get logs from Batocera system or emulators",
                    {"200": _json_response("AdminLogResponse", "Log content")},
                    parameters=[_path_param("source", "Log source key"), _query_param("lines", _integer(default=200, minimum=1, maximum=5000), "Number of tail lines")],
                    tags=["admin"],
                )
            },
            "/admin/gameplay-logs": {"get": _operation("Get local gameplay history", {"200": _json_response("GameplayLogsResponse", "Gameplay history")}, tags=["admin"])},
            "/admin/system-info": {
                "get": _operation(
                    "Get Batocera system information",
                    {"200": _json_response("SystemInfoResponse", "Structured system information")},
                    parameters=[_query_param("speed", _boolean(default=False), "Include an active network speed sample")],
                    tags=["admin"],
                )
            },
            "/admin/system-info/volume": {
                "post": _operation(
                    "Set Batocera system volume",
                    {"200": _json_response("SystemVolumeResponse")},
                    request_body=_json_request("SystemVolumeUpdateRequest"),
                    tags=["admin"],
                    error_codes=("400", "401", "403", "429", "500", "503"),
                )
            },
            "/admin/system-info/screen-mode": {
                "get": _operation(
                    "Get the current EmulationStation screen (UI) mode",
                    {"200": _json_response("ScreenModeResponse")},
                    tags=["admin"],
                ),
                "post": _operation(
                    "Set the EmulationStation screen mode (restarts EmulationStation)",
                    {"200": _json_response("ScreenModeUpdateResponse")},
                    request_body=_json_request("ScreenModeUpdateRequest"),
                    tags=["admin"],
                    error_codes=("400", "401", "403", "429", "500", "503"),
                ),
            },
            "/admin/system-info/music-volume": {
                "post": _operation(
                    "Set EmulationStation music volume (applies live, no restart)",
                    {"200": _json_response("EsCollectionsState")},
                    request_body=_json_request("MusicVolumeUpdateRequest"),
                    tags=["admin"],
                    error_codes=("400", "401", "403", "429", "500", "503"),
                )
            },
            "/admin/es-collections": {
                "get": _operation(
                    "Get EmulationStation systems-displayed / grouped-systems / collections state",
                    {"200": _json_response("EsCollectionsState")},
                    tags=["admin"],
                ),
                "post": _operation(
                    "Update EmulationStation systems-displayed / grouped-systems / collections / music volume / screensaver (restarts EmulationStation)",
                    {"200": _json_response("EsCollectionsState")},
                    request_body=_json_request("EsCollectionsUpdateRequest"),
                    tags=["admin"],
                    error_codes=("400", "401", "403", "429", "500", "503"),
                ),
            },
            "/admin/downloads": {"get": _operation("Get download queue status", {"200": _json_response("AdminDownloadsResponse", "Download queue snapshot")}, tags=["admin", "downloads"])},
            "/admin/downloads/{job_id}/cancel": {
                "post": _operation("Cancel a download job", {"200": _json_response("DownloadActionResponse"), "404": _json_response("DownloadActionResponse", "Job not found")}, parameters=[_path_param("job_id")], tags=["admin", "downloads"], error_codes=("400", "401", "403", "429", "500", "503"))
            },
            "/admin/downloads/{job_id}/retry": {
                "post": _operation("Retry a failed download job", {"200": _json_response("DownloadActionResponse"), "404": _json_response("DownloadActionResponse", "Job not found"), "409": _json_response("DownloadActionResponse", "Job is not retryable")}, parameters=[_path_param("job_id")], tags=["admin", "downloads"], error_codes=("400", "401", "403", "429", "500", "503"))
            },
            "/admin/downloads/{job_id}/pause": {
                "post": _operation("Pause a single download job", {"200": _json_response("DownloadActionResponse"), "404": _json_response("DownloadActionResponse", "Job not found"), "409": _json_response("DownloadActionResponse", "Job is not pausable")}, parameters=[_path_param("job_id")], tags=["admin", "downloads"], error_codes=("400", "401", "403", "429", "500", "503"))
            },
            "/admin/downloads/{job_id}/resume": {
                "post": _operation("Resume a single paused download job", {"200": _json_response("DownloadActionResponse"), "404": _json_response("DownloadActionResponse", "Job not found"), "409": _json_response("DownloadActionResponse", "Job is not resumable")}, parameters=[_path_param("job_id")], tags=["admin", "downloads"], error_codes=("400", "401", "403", "429", "500", "503"))
            },
            "/admin/downloads/pause": {"post": _operation("Pause download processing", {"200": _json_response("DownloadActionResponse")}, tags=["admin", "downloads"], error_codes=("401", "403", "429", "500", "503"))},
            "/admin/downloads/resume": {"post": _operation("Resume download processing", {"200": _json_response("DownloadActionResponse")}, tags=["admin", "downloads"], error_codes=("401", "403", "429", "500", "503"))},
            "/admin/downloads/clear": {"post": _operation("Clear completed and failed downloads", {"200": _json_response("DownloadActionResponse")}, tags=["admin", "downloads"], error_codes=("401", "403", "429", "500", "503"))},
            "/admin/uploads": {"get": _operation("Get upload activity snapshot", {"200": _json_response("AdminUploadsResponse", "Assets currently being served to peers")}, tags=["admin", "downloads"])},
            "/admin/asset-cache": {"get": _operation("Get ROM, BIOS, and artwork asset cache progress", {"200": _json_response("AssetCacheResponse")}, tags=["admin"])},
            "/admin/asset-cache/purge": {"post": _operation("Purge cached asset metadata while keeping fingerprints", {"200": _json_response("AssetCachePurgeResponse")}, tags=["admin"])},
            "/admin/asset-cache/clear-pending": {"post": _operation("Clear pending asset metadata upload changes", {"200": _json_response("AssetCacheClearPendingResponse")}, tags=["admin"])},
            "/admin/api/status": {"get": _operation("API access, Swagger, and mTLS certificate guidance", {"200": _json_response("ApiAdminStatusResponse")}, tags=["admin", "meta"])},
            "/admin/api/certificate": {"get": _operation("Download Drone public certificate", {"200": _media_response("Public certificate PEM", ["application/x-pem-file", "application/x-x509-ca-cert", "text/plain"])}, description="Downloads the public certificate only. Private key material is not exposed.", tags=["admin", "meta"])},
            "/admin/api/certificate/rotate": {"post": _operation("Rotate the Drone's self-signed mTLS certificate", {"200": _json_response("CertificateRotateResponse"), "502": _json_response("CertificateRotateResponse", "Certificate rotation failed")}, tags=["admin", "meta"], error_codes=("400", "401", "403", "404", "429", "500"))},
            "/admin/automation": {"get": _operation("Get device automation settings and input-idle status", {"200": _json_response("AutomationStatusResponse")}, tags=["admin"])},
            "/admin/automation/idle-volume": {"post": _operation("Update idle-volume automation", {"200": _json_response("IdleVolumeResponse")}, request_body=_json_request("IdleVolumeUpdateRequest"), tags=["admin"])},
            "/admin/automation/idle-game-exit": {"post": _operation("Update idle-game-exit automation", {"200": _json_response("IdleGameExitResponse")}, request_body=_json_request("IdleGameExitUpdateRequest"), tags=["admin"])},
            "/admin/automation/wifi-recovery": {"post": _operation("Update Wi-Fi recovery automation", {"200": _json_response("WifiRecoveryResponse")}, request_body=_json_request("WifiRecoveryUpdateRequest"), tags=["admin"])},
            "/admin/system/update-drone": {"post": _operation("Download and stage the latest Drone app release", {"200": _json_response("DroneUpdateResponse")}, tags=["admin"], error_codes=("400", "401", "403", "429", "500", "502"))},
            "/admin/system/auto-update": {
                "get": _operation("Get automatic Drone update setting", {"200": _json_response("DroneAutoUpdateResponse")}, tags=["admin"]),
                "post": _operation("Enable or disable the startup Drone update check", {"200": _json_response("DroneAutoUpdateResponse")}, request_body=_json_request("DroneAutoUpdateRequest"), tags=["admin"]),
            },
            "/admin/system/run-pixn-update": {"post": _operation("Run the installed PixN upgrade script", {"200": _json_response("PixnUpdateResponse")}, tags=["admin"], error_codes=("400", "401", "403", "404", "429", "500"))},
            "/admin/artwork/missing": {
                "get": _operation(
                    "List ROMs for the artwork and metadata hub",
                    {"200": _json_response("ArtworkMissingResponse")},
                    parameters=[
                        _query_param("include_filesystem", _boolean(default=False)),
                        _query_param("refresh", _boolean(default=False)),
                        *common_paging,
                        _query_param("fields", _string(), "Comma-separated artwork fields"),
                        _query_param("systems", _string(), "Comma-separated system filters"),
                        _query_param("rom_status", _enum(["any", "exists", "missing"], default="any")),
                    ],
                    tags=["admin", "artwork"],
                )
            },
            "/admin/artwork/launchbox/search": {"get": _operation("Search LaunchBox Games Database", {"200": _json_response("ArtworkSearchResponse")}, parameters=_artwork_search_params(), tags=["admin", "artwork"])},
            "/admin/artwork/launchbox/apply": {"post": _operation("Apply selected LaunchBox artwork", {"200": _json_response("ArtworkApplyResponse")}, request_body=_json_request("ArtworkApplyRequest"), tags=["admin", "artwork"])},
            "/admin/artwork/thegamesdb/search": {"get": _operation("Search TheGamesDB for artwork candidates", {"200": _json_response("ArtworkSearchResponse")}, parameters=_artwork_search_params(), tags=["admin", "artwork"])},
            "/admin/artwork/thegamesdb/apply": {"post": _operation("Apply selected TheGamesDB artwork", {"200": _json_response("ArtworkApplyResponse")}, request_body=_json_request("ArtworkApplyRequest"), tags=["admin", "artwork"])},
            "/admin/artwork/mobygames/search": {"get": _operation("Search MobyGames metadata", {"200": _json_response("ArtworkSearchResponse")}, parameters=_artwork_search_params(), tags=["admin", "artwork"])},
            "/admin/artwork/mobygames/apply": {"post": _operation("Apply selected MobyGames artwork", {"400": _json_response("ErrorResponse", "MobyGames scraping is disabled")}, request_body=_json_request("ArtworkApplyRequest"), tags=["admin", "artwork"], error_codes=("401", "403", "429", "500"))},
            "/admin/artwork/upload": {"post": _operation("Upload an artwork file and update gamelist metadata", {"200": _json_response("ArtworkUploadResponse")}, request_body=_multipart_request("ArtworkUploadRequest"), tags=["admin", "artwork"])},
            "/admin/artwork/gamelist/remove": {"post": _operation("Remove one gamelist entry", {"200": _json_response("GamelistMutationResponse")}, request_body=_json_request("GamelistRemoveRequest"), tags=["admin", "artwork"])},
            "/admin/artwork/gamelist/update": {"post": _operation("Update one gamelist entry", {"200": _json_response("GamelistMutationResponse")}, request_body=_json_request("GamelistUpdateRequest"), tags=["admin", "artwork"])},
            "/admin/artwork/gamelist/remove-missing": {"post": _operation("Remove gamelist entries whose ROM files are missing", {"200": _json_response("GamelistMutationResponse")}, request_body=_json_request("GamelistRemoveMissingRequest"), tags=["admin", "artwork"])},
            "/admin/network-mode": {
                "get": _operation("Get active integration network mode", {"200": _json_response("NetworkModeResponse")}, tags=["admin", "local-network"]),
                "post": _operation("Update integration network mode", {"200": _json_response("NetworkModeResponse")}, request_body=_json_request("NetworkModeUpdateRequest"), tags=["admin", "local-network"]),
            },
            "/admin/local-network/status": {"get": _operation("Get Local Network discovery and pairing status", {"200": _json_response("LocalNetworkStatusResponse")}, tags=["admin", "local-network"])},
            "/admin/local-network/discover": {"post": _operation("Broadcast Local Network discovery announcement", {"200": _json_response("LocalNetworkStatusResponse")}, tags=["admin", "local-network"], error_codes=("401", "403", "409", "429", "500"))},
            "/admin/local-network/pairing-code/rotate": {"post": _operation("Rotate Local Network pairing code", {"200": _json_response("PairingCodeResponse")}, tags=["admin", "local-network"], error_codes=("401", "403", "409", "429", "500"))},
            "/admin/local-network/pair-by-address": {"post": _operation("Pair with a peer at an operator-entered address (e.g. a tailnet IP; no multicast discovery needed)", {"200": _json_response("LocalPeerPairResponse")}, request_body=_json_request("LocalPeerPairByAddressRequest"), tags=["admin", "local-network"], error_codes=("400", "401", "403", "409", "429", "500", "502"))},
            "/admin/swarm/overview": {"get": _operation("Fleet overview: this Drone plus every paired peer, probed in parallel with a short per-peer budget", {"200": _json_response("SwarmOverviewResponse")}, tags=["admin", "local-network"])},
            "/admin/tailnet/status": {"get": _operation("Tailscale mesh status for the Swarm page onboarding card", {"200": _json_response("TailnetStatusResponse")}, tags=["admin", "local-network"])},
            "/admin/tailnet/enroll": {"post": _operation("Enroll this Drone in the tailnet with an auth key pasted in the UI", {"200": _json_response("TailnetStatusResponse")}, request_body=_json_request("TailnetEnrollRequest"), tags=["admin", "local-network"], error_codes=("400", "401", "403", "429", "500", "502"))},
            "/admin/tailnet/rotate-auth-key": {"post": _operation("Re-enroll this connected Drone with a replacement Tailscale auth key", {"200": _json_response("TailnetStatusResponse")}, request_body=_json_request("TailnetEnrollRequest"), tags=["admin", "local-network"], error_codes=("400", "401", "403", "429", "500", "502"))},
            "/admin/remote/status": {
                "get": _operation(
                    "Check whether a remote-admin session is already cached for a peer",
                    {"200": _json_response("RemoteStatusResponse")},
                    description="Lets a newly opened impersonation tab skip the credential prompt when another tab already connected to the same peer within the session TTL.",
                    parameters=[_query_param("peer_id", _string(), "A paired peer's drone_id")],
                    tags=["admin", "remote"],
                )
            },
            "/admin/remote/connect": {
                "post": _operation(
                    "Verify a paired peer's own Drone credentials and cache them for remote administration",
                    {"200": _json_response("RemoteConnectResponse")},
                    description="Credentials are held only in this Drone's process memory (never persisted, never returned to the browser) and are used solely to authenticate to that peer's own /admin/* surface -- the same login required to manage it directly.",
                    request_body=_json_request("RemoteConnectRequest"),
                    tags=["admin", "remote"],
                    error_codes=("400", "401", "404", "409", "429", "500", "502"),
                )
            },
            "/admin/remote/disconnect": {
                "post": _operation(
                    "Drop the cached remote-administration session for a peer",
                    {"200": _json_response("RemoteDisconnectResponse")},
                    request_body=_json_request("RemoteDisconnectRequest"),
                    tags=["admin", "remote"],
                    error_codes=("400", "401", "429", "500"),
                )
            },
            "/remote/{peer_id}/admin/{admin_path}": {
                "get": _operation(
                    "Proxy an admin GET to a paired, connected peer's own /admin/* surface",
                    {
                        "200": {
                            "description": "Whatever the proxied /admin/* route itself returns -- relayed verbatim",
                            "content": {
                                "application/json": {"schema": _ref("RemoteProxyResponse")},
                                "text/plain": {"schema": {"type": "string"}},
                            },
                        }
                    },
                    description="Generic passthrough: forwards to https://<peer>/v1/api/admin/{admin_path} using the credentials cached by /admin/remote/connect. The peer authenticates and authorizes the request exactly as it would a direct browser call.",
                    parameters=[_path_param("peer_id", "A paired peer's drone_id"), _path_param("admin_path", "Any existing /admin/* sub-path on the peer, e.g. system-info")],
                    tags=["admin", "remote"],
                    error_codes=("401", "403", "404", "429", "500", "502"),
                ),
                "post": _operation(
                    "Proxy an admin POST to a paired, connected peer's own /admin/* surface",
                    {
                        "200": {
                            "description": "Whatever the proxied /admin/* route itself returns -- relayed verbatim",
                            "content": {
                                "application/json": {"schema": _ref("RemoteProxyResponse")},
                                "text/plain": {"schema": {"type": "string"}},
                            },
                        }
                    },
                    description="Same as the GET form, forwarding the request body and Content-Type unchanged.",
                    parameters=[_path_param("peer_id", "A paired peer's drone_id"), _path_param("admin_path", "Any existing /admin/* sub-path on the peer")],
                    tags=["admin", "remote"],
                    error_codes=("400", "401", "403", "404", "429", "500", "502"),
                ),
            },
            "/admin/tailnet/discover": {"post": _operation("Fetch online Tailnet devices and automatically establish mTLS trust with Drones", {"200": _json_response("TailnetDiscoveryResponse")}, tags=["admin", "local-network"], error_codes=("401", "403", "429", "500", "502"))},
            "/admin/local-network/peers/{peer_id}/pair": {"post": _operation("Pair with a discovered Local Network peer", {"200": _json_response("LocalPeerPairResponse")}, parameters=[_path_param("peer_id")], request_body=_json_request("LocalPeerPairRequest"), tags=["admin", "local-network"], error_codes=("400", "401", "403", "404", "409", "429", "500"))},
            "/admin/local-network/peers/{peer_id}/forget": {"post": _operation("Forget a paired Local Network peer", {"200": _json_response("LocalPeerForgetResponse")}, parameters=[_path_param("peer_id")], tags=["admin", "local-network"])},
            "/admin/local-network/peers/{peer_id}/restore-tailnet": {"post": _operation("Restore automatic pairing for a forgotten online Tailnet Drone", {"200": _json_response("LocalPeerPairResponse")}, parameters=[_path_param("peer_id")], tags=["admin", "local-network"], error_codes=("401", "403", "404", "409", "429", "500", "502"))},
            "/admin/local-network/peers/{peer_id}/assets": {"get": _operation("Browse a paired peer's asset inventory", {"200": _json_response("PeerInventoryEnvelope")}, parameters=[_path_param("peer_id"), *peer_inventory_params], tags=["admin", "local-network"], error_codes=("400", "401", "403", "404", "409", "429", "500", "502"))},
            "/admin/local-network/sync": {"post": _operation("Queue one asset copy from a paired peer", {"202": _json_response("LocalSyncResponse")}, request_body=_json_request("LocalSyncRequest"), tags=["admin", "local-network"], error_codes=("400", "401", "403", "404", "409", "429", "500", "503"))},
            "/admin/local-network/sync-bulk": {"post": _operation("Queue bulk asset copies from a paired peer", {"202": _json_response("LocalBulkSyncResponse")}, request_body=_json_request("LocalBulkSyncRequest"), tags=["admin", "local-network"], error_codes=("400", "401", "403", "404", "409", "429", "500", "503"))},
            "/admin/credentials/update": {"post": _operation("Update local Drone Basic Auth credentials", {"200": _json_response("CredentialsUpdateResponse")}, request_body=_json_request("CredentialsUpdateRequest"), tags=["admin"])},
            "/admin/configs/{source}": {
                "get": _operation(
                    "Get important configuration file content for debugging",
                    {"200": _json_response("ConfigFileResponse")},
                    parameters=[_path_param("source"), _query_param("max_bytes", _integer(default=131072, minimum=1024, maximum=1048576)), _query_param("format", _enum(["json", "xml"], default="json"), "Only used for source=es_systems")],
                    tags=["admin", "configs"],
                )
            },
            "/admin/configs/sources": {"get": _operation("List config source keys available on this host", {"200": _json_response("ConfigSourcesResponse")}, tags=["admin", "configs"])},
            "/admin/emulators": {"get": _operation("List emulator config files exposed to the admin UI", {"200": _json_response("EmulatorsResponse")}, tags=["admin", "configs"])},
            "/admin/emulators/file": {
                "get": _operation(
                    "Read one emulator config file",
                    {"200": _json_response("EmulatorFileResponse")},
                    parameters=[_query_param("root", _string(), "Root name from /admin/emulators"), _query_param("relative_path", _string(), "Config path relative to the root"), _query_param("max_bytes", _integer(default=131072, minimum=1024, maximum=1048576))],
                    tags=["admin", "configs"],
                )
            },
            "/peer/pair": {
                "post": _operation(
                    "Pair two Drones in Local Network mode",
                    {"200": _json_response("PeerPairResponse")},
                    request_body=_json_request("PeerPairRequest"),
                    tags=["peer"],
                    security=[],
                    error_codes=("400", "403", "409", "429", "500"),
                )
            },
            "/peer/info": {
                "get": _operation(
                    "Open pairing-bootstrap identity (what the multicast announce broadcasts)",
                    {"200": _json_response("PeerInfoResponse")},
                    description="Unauthenticated by design, like POST /peer/pair: lets a Drone be discovered by dialing its address directly across links multicast cannot cross (e.g. a tailnet).",
                    tags=["peer"],
                    security=[],
                    error_codes=("409", "429", "500"),
                )
            },
            "/peer/health": {
                "get": _operation(
                    "Peer health check",
                    {"200": _json_response("PeerHealthResponse")},
                    tags=["peer"],
                    security=peer_security,
                    error_codes=("403", "429", "500"),
                )
            },
            "/peer/inventory/{asset_type}": {
                "get": _operation(
                    "Get peer asset inventory",
                    {"200": _json_response("PeerInventoryEnvelope")},
                    description="For asset_type=summary the response has the PeerInventorySummaryResponse shape; other asset types use PeerInventoryResponse.",
                    parameters=[
                        _path_param("asset_type", "summary, roms, bios, artwork, saves, emulator_configs, or gameplay"),
                        *common_paging,
                        *system_filter_params,
                    ],
                    tags=["peer"],
                    security=peer_security,
                    error_codes=("400", "403", "429", "500"),
                )
            },
            "/peer/roms/{system}/{relative_path}": {
                "get": _operation(
                    "Download a ROM file from a peer",
                    {"200": _media_response("Peer ROM file stream", ["application/octet-stream"])},
                    parameters=[_path_param("system"), _path_param("relative_path", "ROM path relative to the system directory. URL-encode slash-separated paths.")],
                    tags=["peer", "downloads"],
                    security=peer_security,
                    error_codes=("400", "403", "404", "429", "500"),
                )
            },
            "/peer/rom-manifest/{system}/{relative_path}": {
                "get": _operation(
                    "Get a folder-ROM manifest from a peer",
                    {"200": _json_response("PeerRomManifestResponse")},
                    parameters=[_path_param("system"), _path_param("relative_path", "Folder ROM path relative to the system directory. URL-encode slash-separated paths.")],
                    tags=["peer"],
                    security=peer_security,
                    error_codes=("400", "403", "404", "429", "500"),
                )
            },
            "/peer/bios/{relative_path}": {
                "get": _operation(
                    "Download a BIOS file from a peer",
                    {"200": _media_response("Peer BIOS file stream", ["application/octet-stream"])},
                    parameters=[_path_param("relative_path", "BIOS path relative to the BIOS root. URL-encode slash-separated paths.")],
                    tags=["peer", "downloads"],
                    security=peer_security,
                    error_codes=("400", "403", "404", "429", "500"),
                )
            },
            "/peer/saves/{system}/{relative_path}": {
                "get": _operation(
                    "Download a save file from a peer",
                    {"200": _media_response("Peer save file stream", ["application/octet-stream"])},
                    parameters=[_path_param("system"), _path_param("relative_path", "Save path relative to the system save folder. URL-encode slash-separated paths.")],
                    tags=["peer", "downloads"],
                    security=peer_security,
                    error_codes=("400", "403", "404", "429", "500"),
                )
            },
            "/peer/artwork/{system}/{artwork_type}/{rom_path}": {
                "get": _operation(
                    "Download artwork from a peer",
                    {"200": _media_response("Peer artwork file stream", ["application/octet-stream"])},
                    parameters=[_path_param("system"), _path_param("artwork_type"), _path_param("rom_path", "ROM path used to resolve artwork. URL-encode slash-separated paths.")],
                    tags=["peer", "downloads"],
                    security=peer_security,
                    error_codes=("400", "403", "404", "429", "500"),
                )
            },
        },
    }


def _artwork_search_params() -> Iterable[Schema]:
    return [
        _query_param("system", _string(), "Batocera system key"),
        _query_param("rom_id", _string(), "ROM unique_id"),
        _query_param("rom_path", _string(), "ROM path from gamelist metadata"),
        _query_param("q", _string(), "Manual search query"),
    ]
