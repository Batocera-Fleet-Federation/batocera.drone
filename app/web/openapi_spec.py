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
            "dns_name": _string("Peer's Tailnet MagicDNS FQDN (e.g. drone.tailnet-name.ts.net), empty until a Tailnet discovery sync has seen this peer"),
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
        "AuthSessionResponse": _object(
            {"authenticated": _boolean(), "username": _string(nullable=True)},
            ("authenticated",),
            description="Whether the caller's session cookie (if any) is currently valid.",
        ),
        "AuthLoginRequest": _object({"username": _string(), "password": _string()}, ("username", "password")),
        "AuthLoginResponse": _object({"status": _enum(["ok"]), "username": _string()}, ("status", "username"), description="Login succeeded; the session cookie is set via Set-Cookie."),
        "AuthLogoutResponse": _object({"status": _enum(["logged_out"])}, ("status",)),
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
        "MovieEntry": _object(
            {
                "entry_key": _string("Stable id, used for /movies/{entry_key}/stream, /download, and /artwork/{field}"),
                "movie_name": _string(),
                "name": _string(),
                "display_title": _string("Scraped TMDb title if this movie has been scraped, otherwise movie_name"),
                "file_path": _string(),
                "relative_path": _string(),
                "absolute_path": _string(),
                "file_size": _integer(),
                "byte_count": _integer(),
                "modified_time": _integer("Unix timestamp (seconds)"),
                "mtime": _integer("Unix timestamp (seconds)"),
                "fingerprint": _string(),
                "movies_fingerprint": _string(),
                "is_downloadable": _boolean(),
            },
            description="One movie file under movies_root. Movies have no system/artwork association -- this is a flat inventory.",
        ),
        "MoviesListResponse": _object(
            {
                "movies": _array(_ref("MovieEntry")),
                "count": _integer(),
                "offset": _integer(),
                "limit": _integer(),
                "returned": _integer(),
                "has_more": _boolean(),
            },
            ("movies", "count", "offset", "limit", "returned", "has_more"),
        ),
        "MovieCastMember": _object({"name": _string(), "character": _string()}, description="One TMDb credits.cast entry, billing-ordered."),
        "MovieMetadata": _object(
            {
                "entry_key": _string(),
                "provider": _enum(["tmdb"]),
                "provider_id": _string(),
                "title": _string(),
                "poster_relative_path": _string(nullable=True),
                "backdrop_relative_path": _string(nullable=True),
                "scraped_at": _string(fmt="date-time"),
                "overview": _string(),
                "tagline": _string(),
                "genres": _array(_string()),
                "cast": _array(_ref("MovieCastMember")),
                "release_date": _string(nullable=True),
                "rating": _number(nullable=True),
                "runtime_minutes": _integer(nullable=True),
            },
            description="Scraped TMDb metadata for one movie -- absent (see MovieDetailResponse.metadata) until it has been scraped at least once.",
        ),
        "MovieDetailResponse": _object(
            {"metadata": _object(additional_properties=True, description="MovieMetadata fields, or absent/null if never scraped")},
            description="MovieEntry's fields plus the movie's scraped metadata (or null).",
            additional_properties=True,
        ),
        "MovieScraperSettingsResponse": _object(
            {"has_api_key": _boolean("Whether a TMDb API key is configured -- the key itself is never returned")},
            ("has_api_key",),
        ),
        "MovieScraperSettingsUpdateRequest": _object({"api_key": _string("TMDb API key (v3 auth)")}, ("api_key",)),
        "MovieScrapeSearchResult": _object(
            {
                "tmdb_id": _integer(),
                "title": _string(),
                "release_date": _string(nullable=True),
                "overview": _string(),
                "thumbnail_url": _string(nullable=True),
            },
        ),
        "MovieScrapeSearchResponse": _object(
            {"query": _string(), "results": _array(_ref("MovieScrapeSearchResult"))}, ("query", "results")
        ),
        "MovieScrapeApplyRequest": _object(
            {
                "tmdb_id": _integer("A candidate already chosen from this app's own search results"),
                "tmdb_url": _string(
                    "Direct-lookup alternative to tmdb_id: a bare TMDb id or a full themoviedb.org movie URL "
                    "(e.g. https://www.themoviedb.org/movie/21380-virus), for a title that search doesn't "
                    "reliably surface. Exactly one of tmdb_id/tmdb_url is required; tmdb_id wins if both are sent.",
                    nullable=True,
                ),
            },
        ),
        "MovieScrapeDeleteResponse": _object(
            {"deleted": _boolean("Whether a metadata row existed and was removed; false is a normal no-op, not an error")},
            ("deleted",),
        ),
        "MovieBulkScrapeJob": _object(
            {
                "id": _integer(),
                "status": _enum(["running", "complete", "error"]),
                "rescan_all": _boolean(),
                "total": _integer(),
                "processed": _integer(),
                "matched_count": _integer(),
                "skipped_count": _integer(),
                "failed_count": _integer(),
                "current_movie": _string(),
                "error_message": _string(nullable=True),
                "started_at": _string(fmt="date-time"),
                "completed_at": _string(fmt="date-time", nullable=True),
            },
            description="Progress of the most recent bulk artwork/metadata scrape job.",
        ),
        "MovieBulkScrapeStatusResponse": _object(
            {"job": _ref("MovieBulkScrapeJob")}, description="``job`` is null if no bulk scrape has ever been run."
        ),
        "MovieBulkScrapeStartRequest": _object(
            {"rescan_all": _boolean("Re-scrape every movie, not just ones missing a poster (default false)")}
        ),
        "MovieBulkScrapeStartResponse": _object(
            {
                "status": _enum(["ok", "already_running", "error"]),
                "job": _ref("MovieBulkScrapeJob"),
                "error": _string(nullable=True),
            },
            ("status",),
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
        "TorrentSettings": _object(
            {
                "directory": _string("Watched folder for .torrent files"),
                "download_directory": _string("Where aria2 writes downloaded files; empty means 'same as directory'. Can be a different disk/mount than the Drone's own install location (e.g. an external drive under /media, or /userdata/roms)."),
                "seed_time": _integer("Seed time in minutes; 0 stops seeding as soon as the download completes", minimum=0),
                "seed_ratio": _number("Stop seeding at this share ratio; 0 disables the ratio limit"),
                "bt_stop_timeout": _integer("Stop a torrent stalled at 0 B/s for this many seconds; 0 disables", minimum=0),
                "file_allocation": _enum(("none", "prealloc", "trunc", "falloc"), "aria2 file allocation mode"),
                "max_concurrent_downloads": _integer("Torrents downloading at once; Force Start bypasses this", minimum=1, maximum=16),
            },
            ("directory",),
            description="Watched-folder torrent settings.",
        ),
        "TorrentAria2Status": _object(
            {
                "installed": _boolean(),
                "path": _string(nullable=True),
                "source": _enum(("system", "managed"), "PATH install vs. Drone-managed binary"),
                "version": _string(nullable=True),
                "running": _boolean("Whether the aria2c daemon is currently running"),
                "daemon_error": _string("Last daemon start failure, if any"),
            },
        ),
        "TorrentEntry": _object(
            {
                "id": _string(),
                "name": _string(),
                "status": _enum(("queued", "downloading", "complete", "error")),
                "message": _string(),
                "seeding": _boolean("Complete but still uploading to peers"),
                "progress_percent": _number(),
                "total_bytes": _integer(),
                "completed_bytes": _integer(),
                "download_speed_bps": _integer(),
                "upload_speed_bps": _integer(),
                "num_seeders": _integer(),
                "connections": _integer(),
                "eta_seconds": _integer(nullable=True),
                "torrent_file": _string(),
                "download_dir": _string(),
                "added_at": _string(fmt="date-time", nullable=True),
                "completed_at": _string(fmt="date-time", nullable=True),
            },
        ),
        "AdminTorrentsResponse": _object(
            {
                "target_drone_id": _string(),
                "settings": _ref("TorrentSettings"),
                "directory_exists": _boolean(),
                "download_directory_exists": _boolean(),
                "effective_download_directory": _string("Resolved download location: the override if set, else 'directory'"),
                "aria2": _ref("TorrentAria2Status"),
                "counts": _object({status: _integer() for status in ("queued", "downloading", "complete", "error")}),
                "torrents": _array(_ref("TorrentEntry")),
                "paused": _boolean("Whether the global torrent queue is paused (aria2.pauseAll)"),
                "recent_move_locations": _array(_string("Recently-used Move Files destination")),
            },
            description="Torrent queue snapshot: settings, aria2c status, and per-torrent progress.",
        ),
        "TorrentSettingsUpdateRequest": _object(
            {
                "directory": _string(),
                "download_directory": _string("Empty clears the override (falls back to 'directory')"),
                "seed_time": _integer(minimum=0),
                "seed_ratio": _number(),
                "bt_stop_timeout": _integer(minimum=0),
                "file_allocation": _enum(("none", "prealloc", "trunc", "falloc")),
                "max_concurrent_downloads": _integer(minimum=1, maximum=16),
            },
            description="Partial torrent settings update; omitted fields keep their current values.",
        ),
        "TorrentSettingsUpdateResponse": _object({"settings": _ref("TorrentSettings")}, ("settings",)),
        "TorrentActionResponse": _object(
            {
                "status": _string(),
                "message": _string(),
                "torrent_file_removed": _boolean(),
                "downloaded_files_removed": _boolean("Whether the downloaded payload was fully removed"),
            },
            ("status",),
            description="Torrent mutation result.",
        ),
        "Aria2InstallResponse": _object(
            {
                "status": _string(),
                "path": _string(),
                "version": _string(),
                "source_url": _string(),
                "duration_ms": _integer(),
            },
            description="Result of installing the static aria2c binary.",
        ),
        "ConfigBackup": _object(
            {
                "id": _integer(),
                "status": _enum(("creating", "complete", "error")),
                "file_name": _string(),
                "size_bytes": _integer(),
                "included_file_count": _integer(),
                "skipped_file_count": _integer("Files excluded, e.g. by the configs/ per-file size cap"),
                "skipped_bytes": _integer(),
                "error_message": _string(),
                "created_at": _string(),
                "completed_at": _string(),
                "name": _string("User-supplied label, empty if none was given"),
                "description": _string(),
                "source_drone_id": _string("Set only when pulled from a paired peer, null for a locally-built backup"),
                "source_drone_name": _string(),
                "source_created_at": _string("The backup's original creation time on its source drone, if pulled from a peer"),
                "is_local": _boolean("False when this backup was pulled from a paired peer rather than built here"),
            },
            ("id", "status", "file_name"),
            description="One config-backup tarball's metadata (bytes live on disk, not in this row). Also a P2P asset type (config_backups) -- see /peer/config-backups/{file_name}.",
        ),
        "ConfigBackupsListResponse": _object({"backups": _array(_ref("ConfigBackup"))}, ("backups",)),
        "ConfigBackupCreateRequest": _object(
            {"name": _string(), "description": _string()},
            description="Both optional -- a user-supplied label/description to identify this backup later, including to a peer browsing it via the Request Assets flow.",
        ),
        "ConfigBackupCreateResponse": _object(
            {"status": _string(), "backup": _ref("ConfigBackup")},
            ("status",),
            description="'already_creating' (409) if a backup is already being built.",
        ),
        "ConfigBackupEmailResponse": _object(
            {
                "status": _enum(("sent", "not_configured", "too_large", "error", "not_found")),
                "size_bytes": _integer("Present when status is 'too_large'"),
                "limit_bytes": _integer("Present when status is 'too_large'"),
                "error": _string("Present when status is 'error'"),
            },
            ("status",),
            description="'not_configured'/'too_large'/'error' are 200s (not thrown errors) so the caller can branch on the exact outcome -- e.g. show a popup pointing at Email settings for 'not_configured'.",
        ),
        "ConfigBackupActionResponse": _object({"status": _string()}, ("status",)),
        "ConfigBackupTreeFile": _object({"relative_path": _string(), "size": _integer()}, ("relative_path", "size")),
        "ConfigBackupTreeResponse": _object(
            {
                "status": _enum(("ok", "not_found", "error")),
                "file_name": _string(),
                "name": _string(),
                "size_bytes": _integer(),
                "files": _array(_ref("ConfigBackupTreeFile")),
                "error": _string("Present when status is 'error'"),
            },
            ("status",),
            description="Read-only listing of every file inside the tarball (path + size, never contents), for the admin UI's tree browser.",
        ),
        "ConfigBackupApplyResponse": _object(
            {
                "status": _enum(("ok", "not_found", "error")),
                "restored_file_count": _integer(),
                "skipped_file_count": _integer(),
                "restarted_emulationstation": _boolean(),
                "error": _string("Present when status is 'error'"),
            },
            ("status",),
            description="Extracts the tarball back onto this machine's real config/gamelist/saves paths. An overlay, not a wipe-and-replace: only files the backup actually contains are overwritten, nothing already on disk is deleted or cleared first. Overwriting a targeted file is still irreversible -- the admin UI requires an explicit confirmation before calling this. EmulationStation is stopped during the copy and restarted afterward.",
        ),
        "TorrentUploadRequest": _object(
            {"torrents": _array({"type": "string", "format": "binary"})},
            description="One or more .torrent files as multipart file parts (any field names).",
        ),
        "TorrentUploadError": _object({"file": _string(), "error": _string()}, ("file", "error")),
        "TorrentUploadResponse": _object(
            {
                "status": _string(),
                "saved": _array(_string("Stored filename, suffixed on collision")),
                "errors": _array(_ref("TorrentUploadError")),
                "directory": _string("Watched folder the files were saved into"),
            },
            ("status", "saved", "errors"),
            description="Result of uploading .torrent files into the watched folder.",
        ),
        "TorrentBrowseEntry": _object({"name": _string(), "path": _string()}, ("name", "path")),
        "TorrentBrowseResponse": _object(
            {
                "path": _string("Listed folder; empty when showing the storage roots"),
                "parent": _string(nullable=True),
                "roots": _array(_string()),
                "dirs": _array(_ref("TorrentBrowseEntry")),
            },
            description="Directory-picker listing, restricted to the Batocera storage roots.",
        ),
        "TorrentFile": _object(
            {
                "path": _string("Absolute path on the drone's filesystem"),
                "relative_path": _string("Path relative to the torrent's download directory"),
                "name": _string(),
                "size": _integer(nullable=True),
                "exists": _boolean("Whether the file is still present on disk"),
            },
            ("path", "relative_path", "name", "exists"),
        ),
        "TorrentFilesResponse": _object(
            {
                "status": _string(),
                "message": _string(),
                "files": _array(_ref("TorrentFile")),
                "download_dir": _string(),
            },
            ("status",),
            description="Files known to belong to a completed torrent's download.",
        ),
        "TorrentMoveRequest": _object(
            {
                "files": _array(_string("Absolute path, as returned by TorrentFilesResponse")),
                "destination": _string("Target folder; must be inside the browsable storage roots"),
                "cleanup": _boolean("Delete the torrent's remaining downloaded files afterward, only if every selected file moved successfully"),
            },
            ("files", "destination"),
        ),
        "TorrentMoveError": _object({"file": _string(), "error": _string()}, ("file", "error")),
        "TorrentMoveResponse": _object(
            {
                "status": _string(),
                "message": _string(),
                "moved": _array(_string("New path of a successfully moved file")),
                "errors": _array(_ref("TorrentMoveError")),
                "cleanup_performed": _boolean(),
                "removed_from_list": _boolean("Whether the torrent was fully removed (cleanup succeeded, so nothing was left to track)"),
            },
            ("status",),
            description="Result of moving selected files out of a completed torrent's download folder.",
        ),
        "TorrentClearRequest": _object(
            {
                "delete_from_ui": _boolean("Remove matching torrents from the list"),
                "delete_torrent_file": _boolean("Delete each matching torrent's .torrent file"),
                "delete_downloaded_files": _boolean("Delete each matching torrent's downloaded payload"),
                "scope": _enum(("completed", "all"), "Which torrents to match; 'all' includes downloading/queued/error", default="completed"),
            },
            description="Bulk cleanup request; at least one delete_* flag must be set.",
        ),
        "TorrentClearResponse": _object(
            {
                "status": _string(),
                "cleared": _integer("Number of torrents matched and processed"),
                "scope": _string(),
            },
            ("status",),
            description="Result of a bulk torrent cleanup.",
        ),
        "VpnStatusResponse": _object(
            {
                "status": _enum(("disconnected", "connecting", "connected", "error")),
                "message": _string("Detail for the 'error' status, e.g. an auth failure parsed from the log"),
                "installed": _boolean("Whether an openvpn binary was found on PATH"),
                "binary_path": _string(nullable=True),
                "pid": _integer(nullable=True),
                "has_config": _boolean(),
                "config_filename": _string("Original uploaded filename, for display"),
                "remotes": _array(_string()),
                "has_credentials": _boolean(),
                "username": _string("VPN username, for display -- the password is never returned"),
                "sharing_enabled": _boolean("Whether paired peers may pull this config; always false for an imported config"),
                "source_peer_id": _string("Peer this config was imported from, empty if self-uploaded (self-uploaded configs may be shared; imported ones may not)"),
                "source_peer_name": _string("Display name of source_peer_id's drone, empty if self-uploaded"),
                "revoked_reason": _string("Set when the source peer stopped sharing and credentials were auto-removed; cleared by the next successful upload/import"),
                "revoked_at": _string(fmt="date-time", nullable=True),
                "self_heal_enabled": _boolean("Whether the background watchdog may auto-reconnect on failure; defaults to true"),
                "self_heal_last_at": _string(fmt="date-time", nullable=True),
                "self_heal_last_reason": _string("Why the most recent self-heal reconnect fired, e.g. a decrypt/replay error flood"),
                "self_heal_recent_count": _integer("Self-heal reconnects within the current rate-limit window; the watchdog pauses once this hits the cap"),
                "connected_at": _string(fmt="date-time", nullable=True),
                "connected_duration_seconds": _integer(nullable=True),
                "tunnel_ip": _string(nullable=True),
                "tunnel_interface": _string(),
                "validation_errors": _array(_string()),
                "log_available": _boolean(),
                "log_tail": _array(_string()),
            },
            ("status", "installed", "has_config", "has_credentials", "validation_errors"),
            description="OpenVPN configuration + live connection status snapshot.",
        ),
        "VpnUploadResponse": _object(
            {
                "status": _string(),
                "config_filename": _string(),
                "remotes": _array(_string()),
            },
            ("status", "config_filename"),
            description="Result of uploading and rewriting a provider .ovpn file.",
        ),
        "VpnCredentialsRequest": _object({"username": _string(), "password": _string()}, ("username", "password")),
        "VpnCredentialsResponse": _object({"status": _string(), "username": _string()}, ("status", "username")),
        "VpnActionResponse": _object(
            {
                "status": _enum(("connecting", "connected", "disconnected", "not_running", "already_running", "error")),
                "errors": _array(_string()),
            },
            ("status",),
            description="Result of a connect/disconnect action.",
        ),
        "VpnSharingRequest": _object({"enabled": _boolean()}, ("enabled",)),
        "VpnSharingResponse": _object({"sharing_enabled": _boolean()}, ("sharing_enabled",)),
        "VpnSelfHealRequest": _object({"enabled": _boolean()}, ("enabled",)),
        "VpnSelfHealResponse": _object({"self_heal_enabled": _boolean()}, ("self_heal_enabled",)),
        "VpnPullFromPeerRequest": _object(
            {"peer_id": _string("drone_id of a paired peer that has VPN sharing turned on")},
            ("peer_id",),
        ),
        "VpnPullFromPeerResponse": _object(
            {
                "status": _string(),
                "config_filename": _string(),
                "remotes": _array(_string()),
                "credentials_imported": _boolean("False if the peer shared a config but no credentials"),
            },
            ("status", "config_filename", "credentials_imported"),
            description="Result of importing a peer's shared VPN config (+ credentials, if included).",
        ),
        "VpnPeerConfigResponse": _object(
            {
                "config_filename": _string(),
                "config_text": _string("The rewritten .ovpn contents"),
                "remotes": _array(_string()),
                "has_credentials": _boolean(),
                "username": _string(nullable=True),
                "password": _string("Only ever served over this mTLS peer channel, never through any /admin/* response", nullable=True),
                "connected": _boolean("Whether this drone's own VPN tunnel is actually up right now -- used by the swarm-bootstrap flow to only adopt from a peer with a proven-working connection, not merely one that's configured to share"),
            },
            ("config_filename", "config_text", "has_credentials", "connected"),
            description="This drone's VPN config as served to a paired peer -- only returned when sharing is on (see /admin/vpn/sharing).",
        ),
        "VpnVerifyIpResponse": _object(
            {
                "ip": _string(nullable=True),
                "checked_at": _string(fmt="date-time", nullable=True),
                "error": _string(nullable=True),
            },
            description="On-demand public-IP check (e.g. via ipinfo.io) to confirm traffic is routing through the tunnel.",
        ),
        "SmtpStatusResponse": _object(
            {
                "has_config": _boolean(),
                "host": _string(),
                "port": _integer(),
                "use_starttls": _boolean(),
                "use_ssl": _boolean(),
                "username": _string(),
                "has_password": _boolean("Whether a password is stored -- the password itself is never returned"),
                "from_address": _string(),
                "recipient_email": _string("Where test and digest emails are sent"),
                "sharing_enabled": _boolean("Whether paired peers may pull this config; always false for an imported config"),
                "source_peer_id": _string("Peer this config was imported from, empty if self-configured"),
                "source_peer_name": _string(),
                "revoked_reason": _string(),
                "revoked_at": _string(fmt="date-time", nullable=True),
                "smtp_enabled": _boolean("Local master switch for sending mail from this drone -- independent of sharing"),
                "digest_interval_seconds": _integer(
                    "How often the digest poller checks for new mail to send, in seconds -- 60 (1 minute) to 86400 (24 hours), default 300 (5 minutes). Local to this drone, like smtp_enabled/notify.",
                    default=300, minimum=60, maximum=86400,
                ),
                "notify": _object(additional_properties={"type": "boolean"}, description="event_type -> whether it's included in the email digest"),
                "last_test_result": _object(additional_properties=True, description="{status, sent_at|error} of the most recent Test Email, or absent if never tested"),
                "last_test_at": _string(fmt="date-time", nullable=True),
                "last_digest_sent_at": _string(fmt="date-time", nullable=True),
                "last_digest_error": _string(),
            },
            ("has_config", "smtp_enabled", "sharing_enabled"),
            description="SMTP configuration + sharing status snapshot.",
        ),
        "SmtpSettingsUpdateRequest": _object(
            {
                "host": _string(),
                "port": _integer(),
                "use_starttls": _boolean(),
                "use_ssl": _boolean(),
                "username": _string(),
                "password": _string("Optional on update -- blank keeps the existing stored password"),
                "from_address": _string(),
                "recipient_email": _string(),
            },
            description="Partial update accepted -- omitted fields keep their current value.",
        ),
        "SmtpEnabledRequest": _object({"enabled": _boolean()}, ("enabled",)),
        "SmtpEnabledResponse": _object({"smtp_enabled": _boolean()}, ("smtp_enabled",)),
        "SmtpNotificationTogglesRequest": _object(
            additional_properties={"type": "boolean"},
            description="event_type -> enabled; only keys present are changed.",
        ),
        "SmtpNotificationTogglesResponse": _object(
            {"notify": _object(additional_properties={"type": "boolean"})}, ("notify",)
        ),
        "SmtpDigestIntervalRequest": _object(
            {"digest_interval_seconds": _integer("60 (1 minute) to 86400 (24 hours)", minimum=60, maximum=86400)},
            ("digest_interval_seconds",),
        ),
        "SmtpDigestIntervalResponse": _object(
            {"digest_interval_seconds": _integer(minimum=60, maximum=86400)}, ("digest_interval_seconds",)
        ),
        "SmtpSharingRequest": _object({"enabled": _boolean()}, ("enabled",)),
        "SmtpSharingResponse": _object({"sharing_enabled": _boolean()}, ("sharing_enabled",)),
        "SmtpPullFromPeerRequest": _object(
            {"peer_id": _string("drone_id of a paired peer that has SMTP sharing turned on")},
            ("peer_id",),
        ),
        "SmtpPullFromPeerResponse": _object(
            {"has_config": _boolean(), "host": _string(), "port": _integer()},
            ("has_config",),
            description="Result of importing a peer's shared SMTP configuration -- same shape as SmtpStatusResponse.",
            additional_properties=True,
        ),
        "SmtpTestResponse": _object(
            {
                "status": _enum(("ok", "error")),
                "sent_at": _string(fmt="date-time", nullable=True),
                "error": _string(nullable=True),
            },
            ("status",),
            description="Result of the Test Email button.",
        ),
        "SmtpPeerConfigResponse": _object(
            {
                "host": _string(),
                "port": _integer(),
                "use_starttls": _boolean(),
                "use_ssl": _boolean(),
                "username": _string(),
                "password": _string("Only ever served over this mTLS peer channel, never through any /admin/* response", nullable=True),
                "from_address": _string(),
                "recipient_email": _string(),
            },
            ("host", "port"),
            description="This drone's SMTP settings as served to a paired peer -- only returned when sharing is on (see /admin/smtp/sharing).",
        ),
        "NotificationEntry": _object(
            {
                "id": _integer(),
                "audit_log_id": _integer(),
                "event_type": _string(),
                "title": _string(),
                "message": _string(),
                "created_at": _string(fmt="date-time"),
                "read_at": _string(fmt="date-time", nullable=True),
                "read": _boolean(),
            },
            ("id", "event_type", "title", "created_at", "read"),
        ),
        "NotificationsListResponse": _object(
            {
                "items": _array(_ref("NotificationEntry")),
                "limit": _integer(),
                "has_more": _boolean(),
                "next_before_id": _integer(nullable=True),
                "unread_count": _integer(),
            },
            ("items", "limit", "has_more", "unread_count"),
            description="Keyset-paginated notifications, newest first.",
        ),
        "NotificationUnreadCountResponse": _object({"unread_count": _integer()}, ("unread_count",)),
        "NotificationActionResponse": _object({"status": _string()}, ("status",)),
        "NotificationReadAllResponse": _object({"status": _string(), "marked_read": _integer()}, ("status", "marked_read")),
        "NotificationClearRequest": _object({"only_read": _boolean("Clear only already-read notifications instead of all")}),
        "NotificationClearResponse": _object({"status": _string(), "cleared": _integer()}, ("status", "cleared")),
        "TorrentMagnetRequest": _object({"magnet_uri": _string()}, ("magnet_uri",)),
        "TorrentMagnetResponse": _object({"status": _string(), "id": _string(), "name": _string()}, ("status", "id", "name")),
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
        "DroneUpdateHistoryEntry": _object(
            {
                "id": _integer(),
                "previous_version": _string(),
                "version": _string(),
                "release_url": _string(fmt="uri"),
                "release_notes": _string("Commit summaries between previous_version and version, one per line"),
                "applied_at": _string(),
            },
            ("id", "version", "applied_at"),
        ),
        "DroneUpdateHistoryResponse": _object({"updates": _array(_ref("DroneUpdateHistoryEntry"))}, ("updates",)),
        "PixnUpdateResponse": _object({"type": _string(), "status": _string(), "pid": _integer(nullable=True), "script": _string()}, ("type", "status", "script"), description="PixN upgrade script launch result."),
        "RestartEmulationStationResponse": _object({"status": _string()}, ("status",), description="Result of restarting EmulationStation (batocera-es-swissknife --restart or the init.d script)."),
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
                "dns_name": _string(description="Peer's Tailnet MagicDNS FQDN (e.g. drone.tailnet-name.ts.net), empty until a Tailnet discovery sync has seen this peer"),
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
        "NetworkShareSystemRecord": _object(
            {
                "system": _string(description="System folder name (e.g. 'snes')"),
                "had_local_collision": _boolean(description="Whether a local folder with real content already existed for this system"),
                "renamed_to": _string(description="If a collision was resolved, the exact name the local folder was renamed to (e.g. 'snes.old') so it can be precisely restored later"),
                "symlink_created": _boolean(),
                "skipped_reason": _string(description="Non-empty when this system was left untouched, e.g. because <system>.old already existed"),
            },
            ("system", "had_local_collision", "symlink_created"),
        ),
        "NetworkShareRecord": _object(
            {
                "peer_id": _string(),
                "peer_name": _string(),
                "tailnet_ip": _string(),
                "mount_point": _string(),
                "enabled": _boolean(),
                "status": _enum(["mounted", "peer_unreachable", "error", "pending"]),
                "status_detail": _string(),
                "systems": _array(_ref("NetworkShareSystemRecord")),
                "created_at": _string(fmt="date-time"),
                "updated_at": _string(fmt="date-time"),
                "last_checked_at": _string(fmt="date-time", nullable=True),
            },
            ("peer_id", "peer_name", "status"),
        ),
        "NetworkShareListResponse": _object({"shares": _array(_ref("NetworkShareRecord"))}, ("shares",)),
        "NetworkShareDisableResponse": _object({"status": _enum(["disabled", "not_found"]), "peer_id": _string()}, ("status", "peer_id")),
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
        "LocalPeerForgetResponse": _object({"status": _enum(["forgotten", "not_found"]), "peer_id": _string()}, ("status", "peer_id")),
        "LocalSyncRequest": _object(
            {
                "peer_id": _string(),
                "asset_type": _enum(["roms", "bios", "artwork", "saves", "movies"]),
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
                "asset_type": _enum(["roms", "bios", "artwork", "saves", "movies"]),
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
                "asset_type": _enum(["roms", "bios", "artwork", "saves", "movies", "emulator_configs", "gameplay"]),
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
        _query_param("type", _enum(["summary", "roms", "bios", "artwork", "saves", "movies", "emulator_configs", "gameplay"], default="summary"), "Peer asset type"),
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
                "sessionCookie": {
                    "type": "apiKey",
                    "in": "cookie",
                    "name": "drone_session",
                    "description": "Session cookie issued by POST /auth/login. Log in once; the browser (or a cookie-jar-capable script) attaches this automatically afterward.",
                },
                "mutualTLS": {
                    "type": "mutualTLS",
                    "description": "Used by peer routes when Drone mTLS or Local Network certificate pairing is enabled.",
                },
            },
            "schemas": _schemas(),
        },
        "security": [{"sessionCookie": []}],
        "paths": {
            "/": {
                "get": _operation(
                    "Root UI",
                    {"200": _media_response("HTML UI", ["text/html"], {"type": "string"})},
                    tags=["ui"],
                    security=[],
                    error_codes=("429", "500"),
                )
            },
            "/auth/session": {
                "get": _operation(
                    "Check whether the caller has a live session",
                    {"200": _json_response("AuthSessionResponse", "Session status")},
                    tags=["auth"],
                    security=[],
                    error_codes=("429", "500"),
                )
            },
            "/auth/login": {
                "post": _operation(
                    "Log in and start a session",
                    {"200": _json_response("AuthLoginResponse", "Session started"), "401": _json_response("ErrorResponse", "Invalid username or password")},
                    request_body=_json_request("AuthLoginRequest"),
                    tags=["auth"],
                    security=[],
                    error_codes=("400", "429", "500"),
                )
            },
            "/auth/logout": {
                "post": _operation(
                    "Log out (ends the caller's own session, if any)",
                    {"200": _json_response("AuthLogoutResponse", "Logged out")},
                    tags=["auth"],
                    security=[],
                    error_codes=("429", "500"),
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
            "/movies": {
                "get": _operation(
                    "List local movies (flat inventory, no system grouping). Omit limit to get the whole set in one response (used by the Movies tab to build its folder tree client-side); pass limit for a paged response instead.",
                    {"200": _json_response("MoviesListResponse", "Movies list -- paged if limit was given, otherwise the complete inventory")},
                    parameters=common_paging,
                    tags=["library"],
                )
            },
            "/movies/{entry_key}/stream": {
                "get": _operation(
                    "Stream a movie inline for in-browser playback (Range/206-aware for seeking)",
                    {"200": _media_response("Movie bytes", ["video/mp4", "video/webm", "video/x-matroska", "video/quicktime", "video/x-msvideo", "application/octet-stream"])},
                    parameters=[_path_param("entry_key")],
                    tags=["downloads"],
                    error_codes=("401", "403", "404", "429", "500"),
                )
            },
            "/movies/{entry_key}/download": {
                "get": _operation(
                    "Download a movie file by its entry_key",
                    {"200": _media_response("Movie file stream", ["application/octet-stream"])},
                    parameters=[_path_param("entry_key")],
                    tags=["downloads"],
                    error_codes=("401", "403", "404", "429", "500"),
                )
            },
            "/movies/{entry_key}": {
                "get": _operation(
                    "Full detail for one movie: file info plus scraped TMDb metadata (metadata is null if never scraped)",
                    {"200": _json_response("MovieDetailResponse", "Movie detail")},
                    parameters=[_path_param("entry_key")],
                    tags=["library"],
                    error_codes=("401", "403", "404", "429", "500"),
                )
            },
            "/movies/{entry_key}/artwork/{field}": {
                "get": _operation(
                    "Serve a scraped poster/backdrop image",
                    {"200": _media_response("Artwork image bytes", ["image/jpeg"])},
                    parameters=[_path_param("entry_key"), _path_param("field", "poster or backdrop")],
                    tags=["library"],
                    error_codes=("401", "403", "404", "429", "500"),
                )
            },
            "/admin/movies/scraper-settings": {
                "get": _operation(
                    "Get whether a TMDb API key is configured (the key itself is never returned)",
                    {"200": _json_response("MovieScraperSettingsResponse")},
                    tags=["admin", "movies"],
                ),
                "post": _operation(
                    "Save the TMDb API key used to scrape movie metadata/artwork",
                    {"200": _json_response("MovieScraperSettingsResponse"), "400": _json_response("ErrorResponse", "Missing api_key")},
                    request_body=_json_request("MovieScraperSettingsUpdateRequest"),
                    tags=["admin", "movies"],
                    error_codes=("400", "401", "403", "429", "500", "503"),
                ),
            },
            "/admin/movies/{entry_key}/scrape/search": {
                "get": _operation(
                    "Search TMDb for this movie -- q defaults to a cleaned-up version of the filename if omitted",
                    {"200": _json_response("MovieScrapeSearchResponse"), "404": _json_response("ErrorResponse", "Unknown movie"), "502": _json_response("ErrorResponse", "TMDb unreachable or no API key configured")},
                    parameters=[_path_param("entry_key"), _query_param("q", _string(), "Search query; defaults to a cleaned-up filename")],
                    tags=["admin", "movies"],
                    error_codes=("401", "403", "404", "429", "500", "502", "503"),
                )
            },
            "/admin/movies/{entry_key}/scrape/apply": {
                "post": _operation(
                    "Apply a chosen TMDb search result (tmdb_id), or a directly pasted TMDb id/movie URL (tmdb_url) -- "
                    "either way, downloads poster/backdrop art next to the movie file and saves metadata",
                    {"200": _json_response("MovieMetadata"), "400": _json_response("ErrorResponse", "Missing/invalid tmdb_id or tmdb_url"), "404": _json_response("ErrorResponse", "Unknown movie"), "502": _json_response("ErrorResponse", "TMDb unreachable or no API key configured")},
                    request_body=_json_request("MovieScrapeApplyRequest"),
                    parameters=[_path_param("entry_key")],
                    tags=["admin", "movies"],
                    error_codes=("400", "401", "403", "404", "429", "500", "502", "503"),
                )
            },
            "/admin/movies/{entry_key}/scrape/delete": {
                "post": _operation(
                    "Clear a movie/show entry's scraped TMDb metadata and artwork -- for when a scrape matched the "
                    "wrong thing and it needs a clean slate before retrying",
                    {"200": _json_response("MovieScrapeDeleteResponse")},
                    parameters=[_path_param("entry_key")],
                    tags=["admin", "movies"],
                    error_codes=("401", "403", "429", "500", "503"),
                )
            },
            "/admin/movies/scrape/bulk": {
                "get": _operation(
                    "Get the progress of the most recent bulk artwork/metadata scrape job",
                    {"200": _json_response("MovieBulkScrapeStatusResponse")},
                    tags=["admin", "movies"],
                ),
                "post": _operation(
                    "Start a background job that scrapes every movie missing a poster (or all movies, if rescan_all is set) -- "
                    "auto-applies the top TMDb search match for each, no per-movie confirmation",
                    {
                        "200": _json_response("MovieBulkScrapeStartResponse"),
                        "409": _json_response("MovieBulkScrapeStartResponse", "A bulk scrape is already running"),
                        "502": _json_response("MovieBulkScrapeStartResponse", "TMDb unreachable or no API key configured"),
                    },
                    request_body=_json_request("MovieBulkScrapeStartRequest"),
                    tags=["admin", "movies"],
                    error_codes=("401", "403", "409", "429", "500", "502", "503"),
                ),
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
            "/admin/torrents": {"get": _operation("Get torrent queue snapshot", {"200": _json_response("AdminTorrentsResponse", "Torrent settings, aria2c status, and per-torrent progress")}, tags=["admin", "torrents"], error_codes=("401", "403", "429", "500", "503"))},
            "/admin/torrents/browse": {
                "get": _operation("Browse folders for the torrent directory picker", {"200": _json_response("TorrentBrowseResponse")}, parameters=[_query_param("path", _string(), "Folder to list; empty lists the storage roots")], tags=["admin", "torrents"], error_codes=("400", "401", "403", "429", "500", "503"))
            },
            "/admin/torrents/settings": {
                "post": _operation("Update torrent settings", {"200": _json_response("TorrentSettingsUpdateResponse")}, request_body=_json_request("TorrentSettingsUpdateRequest"), tags=["admin", "torrents"], error_codes=("400", "401", "403", "429", "500", "503"))
            },
            "/admin/torrents/upload": {
                "post": _operation("Upload one or more .torrent files into the watched folder", {"200": _json_response("TorrentUploadResponse"), "400": _json_response("TorrentUploadResponse", "No file could be saved")}, request_body=_multipart_request("TorrentUploadRequest"), tags=["admin", "torrents"], error_codes=("401", "403", "429", "500", "503"))
            },
            "/admin/torrents/aria2/install": {
                "post": _operation("Download and install the static aria2c binary", {"200": _json_response("Aria2InstallResponse")}, tags=["admin", "torrents"], error_codes=("400", "401", "403", "429", "500", "503"))
            },
            "/admin/config-backups": {
                "get": _operation("List config-backup tarballs, newest first", {"200": _json_response("ConfigBackupsListResponse")}, tags=["admin", "config-backups"], error_codes=("401", "403", "429", "500")),
                "post": _operation("Start building a new config-backup tarball in the background", {"200": _json_response("ConfigBackupCreateResponse"), "409": _json_response("ConfigBackupCreateResponse", "A backup is already being built")}, request_body=_json_request("ConfigBackupCreateRequest"), tags=["admin", "config-backups"], error_codes=("401", "403", "429", "500")),
            },
            "/admin/config-backups/{backup_id}/download": {
                "get": _operation("Download a completed config-backup tarball", {"200": {"description": "The tar.gz file", "content": {"application/gzip": {"schema": {"type": "string", "format": "binary"}}}}, "404": _json_response("ConfigBackupActionResponse", "Backup not found or not yet complete")}, parameters=[_path_param("backup_id")], tags=["admin", "config-backups"], error_codes=("401", "403", "429", "500"))
            },
            "/admin/config-backups/{backup_id}/tree": {
                "get": _operation("List the files/directories inside a completed config-backup tarball (no contents)", {"200": _json_response("ConfigBackupTreeResponse"), "404": _json_response("ConfigBackupTreeResponse", "Backup not found or not yet complete")}, parameters=[_path_param("backup_id")], tags=["admin", "config-backups"], error_codes=("401", "403", "429", "500"))
            },
            "/admin/config-backups/{backup_id}/apply": {
                "post": _operation("Apply (restore) a completed config-backup tarball onto this machine -- irreversible", {"200": _json_response("ConfigBackupApplyResponse"), "404": _json_response("ConfigBackupApplyResponse", "Backup not found or not yet complete")}, parameters=[_path_param("backup_id")], tags=["admin", "config-backups"], error_codes=("401", "403", "429", "500"))
            },
            "/admin/config-backups/{backup_id}/delete": {
                "post": _operation("Delete a config-backup tarball and its metadata", {"200": _json_response("ConfigBackupActionResponse"), "404": _json_response("ConfigBackupActionResponse", "Backup not found")}, parameters=[_path_param("backup_id")], tags=["admin", "config-backups"], error_codes=("401", "403", "429", "500"))
            },
            "/admin/config-backups/{backup_id}/email": {
                "post": _operation("Email a completed config-backup tarball as an attachment (SMTP must be configured)", {"200": _json_response("ConfigBackupEmailResponse"), "404": _json_response("ConfigBackupEmailResponse", "Backup not found or not yet complete")}, parameters=[_path_param("backup_id")], tags=["admin", "config-backups"], error_codes=("401", "403", "429", "500"))
            },
            "/peer/config-backups/{file_name}": {
                "get": _operation("mTLS: download a completed config-backup tarball from a paired peer", {"200": {"description": "The tar.gz file", "content": {"application/gzip": {"schema": {"type": "string", "format": "binary"}}}}, "400": _json_response("ConfigBackupActionResponse", "Invalid file name"), "404": _json_response("ConfigBackupActionResponse", "Backup not found or not yet complete")}, parameters=[_path_param("file_name")], tags=["peer", "config-backups"], error_codes=("401", "403", "429", "500"))
            },
            "/admin/torrents/{torrent_id}/force-start": {
                "post": _operation("Force-start a torrent, bypassing the concurrency limit", {"200": _json_response("TorrentActionResponse"), "404": _json_response("TorrentActionResponse", "Torrent not found"), "409": _json_response("TorrentActionResponse", "Torrent already completed")}, parameters=[_path_param("torrent_id")], tags=["admin", "torrents"], error_codes=("400", "401", "403", "429", "500", "503"))
            },
            "/admin/torrents/{torrent_id}/cancel": {
                "post": _operation("Stop a torrent and send it to the back of the queue (or stop seeding a completed one); resumes on its own, keeps partial files", {"200": _json_response("TorrentActionResponse"), "404": _json_response("TorrentActionResponse", "Torrent not found"), "409": _json_response("TorrentActionResponse", "Torrent is not cancelable")}, parameters=[_path_param("torrent_id")], tags=["admin", "torrents"], error_codes=("400", "401", "403", "429", "500", "503"))
            },
            "/admin/torrents/{torrent_id}/delete": {
                "post": _operation("Delete a torrent: remove it from the list, delete its .torrent file, and delete its downloaded files", {"200": _json_response("TorrentActionResponse"), "404": _json_response("TorrentActionResponse", "Torrent not found")}, parameters=[_path_param("torrent_id")], tags=["admin", "torrents"], error_codes=("400", "401", "403", "429", "500", "503"))
            },
            "/admin/torrents/{torrent_id}/files": {
                "get": _operation("List the files a completed torrent downloaded", {"200": _json_response("TorrentFilesResponse"), "404": _json_response("TorrentFilesResponse", "Torrent not found"), "409": _json_response("TorrentFilesResponse", "Torrent has not completed yet")}, parameters=[_path_param("torrent_id")], tags=["admin", "torrents"], error_codes=("400", "401", "403", "429", "500", "503"))
            },
            "/admin/torrents/{torrent_id}/move": {
                "post": _operation("Move selected files out of a completed torrent's download folder, optionally cleaning up afterward", {"200": _json_response("TorrentMoveResponse"), "400": _json_response("TorrentMoveResponse", "No files selected or destination invalid"), "404": _json_response("TorrentMoveResponse", "Torrent not found"), "409": _json_response("TorrentMoveResponse", "Torrent has not completed yet")}, parameters=[_path_param("torrent_id")], request_body=_json_request("TorrentMoveRequest"), tags=["admin", "torrents"], error_codes=("400", "401", "403", "429", "500", "503"))
            },
            "/admin/torrents/pause": {"post": _operation("Pause the torrent queue (aria2.pauseAll; new torrents stop starting)", {"200": _json_response("AdminTorrentsResponse")}, tags=["admin", "torrents"], error_codes=("401", "403", "429", "500", "503"))},
            "/admin/torrents/resume": {"post": _operation("Resume the torrent queue", {"200": _json_response("AdminTorrentsResponse")}, tags=["admin", "torrents"], error_codes=("401", "403", "429", "500", "503"))},
            "/admin/torrents/clear": {
                "post": _operation("Bulk-clean up torrents matching a scope", {"200": _json_response("TorrentClearResponse"), "400": _json_response("TorrentClearResponse", "No delete_* action selected")}, request_body=_json_request("TorrentClearRequest"), tags=["admin", "torrents"], error_codes=("401", "403", "429", "500", "503"))
            },
            "/admin/torrents/magnet": {
                "post": _operation("Add a magnet link to the torrent queue, paused like a scanned .torrent file", {"200": _json_response("TorrentMagnetResponse"), "400": _json_response("ErrorResponse", "Not a valid magnet link")}, request_body=_json_request("TorrentMagnetRequest"), tags=["admin", "torrents"], error_codes=("400", "401", "403", "429", "500", "503"))
            },
            "/admin/vpn": {"get": _operation("Get OpenVPN configuration and live connection status", {"200": _json_response("VpnStatusResponse")}, tags=["admin", "vpn"], error_codes=("401", "403", "429", "500", "503"))},
            "/admin/vpn/upload": {
                "post": _operation("Upload a provider .ovpn file (rewritten to use the managed credentials file)", {"200": _json_response("VpnUploadResponse"), "400": _json_response("ErrorResponse", "Not a valid OpenVPN config")}, request_body=_multipart_request("TorrentUploadRequest"), tags=["admin", "vpn"], error_codes=("401", "403", "429", "500", "503"))
            },
            "/admin/vpn/credentials": {
                "post": _operation("Save VPN username/password (written to a 600-permission auth file)", {"200": _json_response("VpnCredentialsResponse")}, request_body=_json_request("VpnCredentialsRequest"), tags=["admin", "vpn"], error_codes=("400", "401", "403", "429", "500", "503"))
            },
            "/admin/vpn/connect": {
                "post": _operation("Connect the VPN", {"200": _json_response("VpnActionResponse"), "400": _json_response("VpnActionResponse", "Not ready to connect (see errors)")}, tags=["admin", "vpn"], error_codes=("401", "403", "429", "500", "503"))
            },
            "/admin/vpn/disconnect": {
                "post": _operation("Disconnect the VPN", {"200": _json_response("VpnActionResponse")}, tags=["admin", "vpn"], error_codes=("401", "403", "429", "500", "503"))
            },
            "/admin/vpn/verify-ip": {
                "post": _operation("On-demand public-IP check to confirm the tunnel is actually routing traffic", {"200": _json_response("VpnVerifyIpResponse"), "502": _json_response("VpnVerifyIpResponse", "Could not determine the public IP")}, tags=["admin", "vpn"], error_codes=("401", "403", "429", "500", "503"))
            },
            "/admin/vpn/sharing": {
                "post": _operation("Toggle allowing paired peers to pull this VPN config; rejected if this config was itself imported from a peer", {"200": _json_response("VpnSharingResponse"), "400": _json_response("ErrorResponse", "This config was imported from a peer and cannot be re-shared")}, request_body=_json_request("VpnSharingRequest"), tags=["admin", "vpn"], error_codes=("400", "401", "403", "429", "500", "503"))
            },
            "/admin/vpn/pull-from-peer": {
                "post": _operation("Pull VPN config (+ credentials, if shared) from a paired peer and adopt it", {"200": _json_response("VpnPullFromPeerResponse"), "404": _json_response("ErrorResponse", "Unknown peer, or that peer has sharing off / no config"), "502": _json_response("ErrorResponse", "Could not reach that peer")}, request_body=_json_request("VpnPullFromPeerRequest"), tags=["admin", "vpn"], error_codes=("400", "401", "403", "404", "429", "500", "502", "503"))
            },
            "/admin/vpn/self-heal": {
                "post": _operation("Toggle automatically reconnecting when the VPN connection fails (decrypt/replay errors or an explicit connection error); on by default", {"200": _json_response("VpnSelfHealResponse")}, request_body=_json_request("VpnSelfHealRequest"), tags=["admin", "vpn"], error_codes=("400", "401", "403", "429", "500", "503"))
            },
            "/admin/vpn/log/download": {
                "get": _operation("Download the raw openvpn log", {"200": _media_response("Log file", ["text/plain"])}, tags=["admin", "vpn"], error_codes=("401", "403", "404", "429", "500"))
            },
            "/admin/smtp": {"get": _operation("Get SMTP configuration and sharing status", {"200": _json_response("SmtpStatusResponse")}, tags=["admin", "smtp"], error_codes=("401", "403", "429", "500", "503"))},
            "/admin/smtp/settings": {
                "post": _operation("Save SMTP settings (host/port/auth/from/recipient)", {"200": _json_response("SmtpStatusResponse"), "400": _json_response("ErrorResponse", "Missing/invalid host, from address, recipient, or port")}, request_body=_json_request("SmtpSettingsUpdateRequest"), tags=["admin", "smtp"], error_codes=("400", "401", "403", "429", "500", "503"))
            },
            "/admin/smtp/enabled": {
                "post": _operation("Toggle whether this drone sends mail (Test Email + the digest poller) -- independent of sharing", {"200": _json_response("SmtpEnabledResponse")}, request_body=_json_request("SmtpEnabledRequest"), tags=["admin", "smtp"], error_codes=("401", "403", "429", "500", "503"))
            },
            "/admin/smtp/notifications": {
                "post": _operation("Update which event types are included in the email digest", {"200": _json_response("SmtpNotificationTogglesResponse")}, request_body=_json_request("SmtpNotificationTogglesRequest"), tags=["admin", "smtp"], error_codes=("401", "403", "429", "500", "503"))
            },
            "/admin/smtp/digest-interval": {
                "post": _operation("Update how often the digest poller checks for new mail to send (1 minute-24 hours)", {"200": _json_response("SmtpDigestIntervalResponse"), "400": _json_response("ErrorResponse", "Value outside 60-86400 seconds")}, request_body=_json_request("SmtpDigestIntervalRequest"), tags=["admin", "smtp"], error_codes=("400", "401", "403", "429", "500", "503"))
            },
            "/admin/smtp/sharing": {
                "post": _operation("Toggle allowing paired peers to pull this SMTP config; rejected if this config was itself imported from a peer", {"200": _json_response("SmtpSharingResponse"), "400": _json_response("ErrorResponse", "This config was imported from a peer and cannot be re-shared")}, request_body=_json_request("SmtpSharingRequest"), tags=["admin", "smtp"], error_codes=("400", "401", "403", "429", "500", "503"))
            },
            "/admin/smtp/pull-from-peer": {
                "post": _operation("Pull SMTP settings from a paired peer and adopt them", {"200": _json_response("SmtpPullFromPeerResponse"), "404": _json_response("ErrorResponse", "Unknown peer, or that peer has sharing off / no config"), "502": _json_response("ErrorResponse", "Could not reach that peer")}, request_body=_json_request("SmtpPullFromPeerRequest"), tags=["admin", "smtp"], error_codes=("400", "401", "403", "404", "429", "500", "502", "503"))
            },
            "/admin/smtp/test": {
                "post": _operation("Send a test email using the saved settings", {"200": _json_response("SmtpTestResponse"), "502": _json_response("SmtpTestResponse", "Send failed")}, tags=["admin", "smtp"], error_codes=("401", "403", "429", "500", "503"))
            },
            "/admin/notifications": {
                "get": _operation("List notifications, newest first (keyset-paginated)", {"200": _json_response("NotificationsListResponse")}, parameters=[_query_param("before_id", _integer(), "Return items with id less than this"), _query_param("limit", _integer(), "Page size"), _query_param("unread_only", _string(), "1/true to return only unread items")], tags=["admin", "notifications"])
            },
            "/admin/notifications/unread-count": {
                "get": _operation("Get the unread notification count (for the bell-icon badge)", {"200": _json_response("NotificationUnreadCountResponse")}, tags=["admin", "notifications"])
            },
            "/admin/notifications/{notification_id}/read": {
                "post": _operation("Mark a notification read", {"200": _json_response("NotificationActionResponse"), "404": _json_response("NotificationActionResponse", "Notification not found")}, parameters=[_path_param("notification_id")], tags=["admin", "notifications"], error_codes=("401", "403", "429", "500"))
            },
            "/admin/notifications/read-all": {
                "post": _operation("Mark every notification read", {"200": _json_response("NotificationReadAllResponse")}, tags=["admin", "notifications"])
            },
            "/admin/notifications/{notification_id}/dismiss": {
                "post": _operation("Delete a single notification", {"200": _json_response("NotificationActionResponse"), "404": _json_response("NotificationActionResponse", "Notification not found")}, parameters=[_path_param("notification_id")], tags=["admin", "notifications"], error_codes=("401", "403", "429", "500"))
            },
            "/admin/notifications/clear": {
                "post": _operation("Delete all notifications, or only already-read ones", {"200": _json_response("NotificationClearResponse")}, request_body=_json_request("NotificationClearRequest", required=False), tags=["admin", "notifications"])
            },
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
            "/admin/system/update-history": {
                "get": _operation("List past Drone app self-updates (version, GitHub release link, commit notes)", {"200": _json_response("DroneUpdateHistoryResponse")}, tags=["admin"]),
            },
            "/admin/system/auto-update": {
                "get": _operation("Get automatic Drone update setting", {"200": _json_response("DroneAutoUpdateResponse")}, tags=["admin"]),
                "post": _operation("Enable or disable the startup Drone update check", {"200": _json_response("DroneAutoUpdateResponse")}, request_body=_json_request("DroneAutoUpdateRequest"), tags=["admin"]),
            },
            "/admin/system/run-pixn-update": {"post": _operation("Run the installed PixN upgrade script", {"200": _json_response("PixnUpdateResponse")}, tags=["admin"], error_codes=("400", "401", "403", "404", "429", "500"))},
            "/admin/system/restart-emulationstation": {"post": _operation("Restart EmulationStation", {"200": _json_response("RestartEmulationStationResponse"), "502": _json_response("RestartEmulationStationResponse", "Restart failed")}, tags=["admin"], error_codes=("400", "401", "403", "429", "500"))},
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
            "/admin/network-shares": {"get": _operation("List this Drone's configured peer ROM references (SMB/CIFS network shares) and their live mount status", {"200": _json_response("NetworkShareListResponse")}, tags=["admin", "local-network"])},
            "/admin/network-shares/{peer_id}/enable": {
                "post": _operation(
                    "Reference a paired peer's whole ROM library over SMB (mount + symlink every system, renaming any locally-colliding system folder to <system>.old first)",
                    {"200": _json_response("NetworkShareRecord")},
                    parameters=[_path_param("peer_id", "A paired peer's drone_id")],
                    tags=["admin", "local-network"],
                    error_codes=("400", "401", "403", "429", "500", "502"),
                )
            },
            "/admin/network-shares/{peer_id}/disable": {
                "post": _operation(
                    "Stop referencing a peer's ROM library: unmount and precisely reverse only the renames/symlinks this Drone made for it",
                    {"200": _json_response("NetworkShareDisableResponse")},
                    parameters=[_path_param("peer_id", "A paired peer's drone_id")],
                    tags=["admin", "local-network"],
                    error_codes=("401", "403", "404", "429", "500"),
                )
            },
            "/admin/tailnet/status": {"get": _operation("Tailscale mesh status for the Swarm page onboarding card", {"200": _json_response("TailnetStatusResponse")}, tags=["admin", "local-network"])},
            "/admin/tailnet/enroll": {"post": _operation("Enroll this Drone in the tailnet with an auth key pasted in the UI", {"200": _json_response("TailnetStatusResponse")}, request_body=_json_request("TailnetEnrollRequest"), tags=["admin", "local-network"], error_codes=("400", "401", "403", "429", "500", "502"))},
            "/admin/tailnet/rotate-auth-key": {"post": _operation("Re-enroll this connected Drone with a replacement Tailscale auth key", {"200": _json_response("TailnetStatusResponse")}, request_body=_json_request("TailnetEnrollRequest"), tags=["admin", "local-network"], error_codes=("400", "401", "403", "429", "500", "502"))},
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
            "/peer/vpn/config": {
                "get": _operation(
                    "Get this drone's shared VPN config (+ credentials, if included) -- only when sharing is on",
                    {"200": _json_response("VpnPeerConfigResponse"), "404": _json_response("ErrorResponse", "Sharing is off, or no config has been uploaded")},
                    tags=["peer", "vpn"],
                    security=peer_security,
                    error_codes=("403", "404", "429", "500"),
                )
            },
            "/peer/smtp/config": {
                "get": _operation(
                    "Get this drone's shared SMTP settings -- only when sharing is on",
                    {"200": _json_response("SmtpPeerConfigResponse"), "404": _json_response("ErrorResponse", "Sharing is off, or no config has been set up")},
                    tags=["peer", "smtp"],
                    security=peer_security,
                    error_codes=("403", "404", "429", "500"),
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
