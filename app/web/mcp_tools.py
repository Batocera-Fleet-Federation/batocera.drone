"""MCP tool registry for the Drone MCP server.

Every tool is a thin wrapper over an existing Drone REST endpoint: the MCP
request handler calls these on the loopback interface (where the auth layer
already trusts on-device traffic), so the tool surface stays automatically in
sync with the real API and there is no second copy of the business logic.

Favour the SQLite-backed read endpoints (``/roms``, ``/systems``, ``/bios``,
``/admin/artwork/missing``) over anything that walks the filesystem, per the
feature request.

Stdlib only.
"""

from __future__ import annotations

from typing import Callable, Dict, List

# ---------------------------------------------------------------------------
# Tool definitions
#
# Each entry:
#   name         - MCP tool name (snake_case, stable)
#   description  - shown to the model
#   schema       - JSON Schema for the arguments object
#   call(ctx, a) - returns a JSON-serialisable result; ``ctx`` exposes
#                  ctx.get(path, **query) and ctx.post(path, body)
# ---------------------------------------------------------------------------


def _str(value) -> str:
    return "" if value is None else str(value)


def _tool(name, description, schema, call):
    return {"name": name, "description": description, "inputSchema": schema, "call": call}


_NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}


def _paged(extra=None):
    props = {
        "limit": {"type": "integer", "description": "Max rows to return."},
        "offset": {"type": "integer", "description": "Rows to skip (pagination)."},
        "q": {"type": "string", "description": "Free-text filter."},
    }
    props.update(extra or {})
    return {"type": "object", "properties": props, "additionalProperties": False}


def _build() -> List[dict]:
    tools: List[dict] = []

    # -- Assets / games ----------------------------------------------------
    tools.append(_tool(
        "list_asset_systems",
        "List every game/emulator system known to this Drone, from the asset cache DB.",
        _NO_ARGS,
        lambda ctx, a: ctx.get("/systems"),
    ))
    tools.append(_tool(
        "list_assets",
        "Browse cached ROM/game assets (SQLite asset DB, no disk scan). "
        "Filter by system, genre or free text.",
        _paged({
            "system": {"type": "string"},
            "genre": {"type": "string"},
            "browser_playable": {"type": "boolean"},
        }),
        lambda ctx, a: ctx.get(
            "/roms",
            system=a.get("system"), genre=a.get("genre"), q=a.get("q"),
            limit=a.get("limit"), offset=a.get("offset"),
            browser_playable=a.get("browser_playable"),
        ),
    ))
    tools.append(_tool(
        "get_gamelist",
        "Read the gamelist (cached ROM metadata) for one system.",
        _paged({"system": {"type": "string", "description": "System name, e.g. 'snes'."}}),
        lambda ctx, a: ctx.get(
            "/systems/" + _str(a.get("system")),
            q=a.get("q"), limit=a.get("limit"), offset=a.get("offset"),
        ),
    ))
    tools.append(_tool(
        "list_bios",
        "List cached BIOS files and their per-system association status.",
        _paged({
            "system": {"type": "string"},
            "unassigned": {"type": "boolean", "description": "Only BIOS files not linked to a system."},
        }),
        lambda ctx, a: ctx.get(
            "/bios", q=a.get("q"), system=a.get("system"),
            unassigned=a.get("unassigned"), limit=a.get("limit"), offset=a.get("offset"),
        ),
    ))
    tools.append(_tool(
        "list_missing_artwork",
        "List cached assets that are missing artwork (from the asset DB).",
        _paged({
            "systems": {"type": "string", "description": "Comma-separated system filter."},
            "fields": {"type": "string", "description": "Comma-separated artwork fields to check."},
            "rom_status": {"type": "string", "enum": ["any", "present", "missing"]},
        }),
        lambda ctx, a: ctx.get(
            "/admin/artwork/missing",
            systems=a.get("systems"), fields=a.get("fields"), q=a.get("q"),
            rom_status=a.get("rom_status"), limit=a.get("limit"), offset=a.get("offset"),
        ),
    ))
    tools.append(_tool(
        "scrape_asset_artwork",
        "Run an artwork scraper for a single ROM and apply the best match. "
        "Identify the ROM by system + rom_path (preferred) or rom_id.",
        {
            "type": "object",
            "properties": {
                "system": {"type": "string"},
                "rom_path": {"type": "string"},
                "rom_id": {"type": "string"},
                "provider": {"type": "string", "enum": ["launchbox", "thegamesdb", "mobygames"]},
                "q": {"type": "string", "description": "Override search title."},
                "override_existing": {"type": "boolean"},
                "import_metadata": {"type": "boolean"},
            },
            "required": ["system"],
            "additionalProperties": False,
        },
        lambda ctx, a: _scrape(ctx, a),
    ))

    # -- Controls (read + write) ----------------------------------------
    tools.append(_tool(
        "get_controls",
        "Read current device controls: screen mode, audio volume, music volume, screensaver delay.",
        _NO_ARGS,
        lambda ctx, a: {
            "screen_mode": ctx.get("/admin/system-info/screen-mode"),
            "es_collections": ctx.get("/admin/es-collections"),
            "system_info": ctx.get("/admin/system-info"),
        },
    ))
    tools.append(_tool(
        "set_screen_mode",
        "Set the EmulationStation screen mode (full / kiosk / kid).",
        {
            "type": "object",
            "properties": {"mode": {"type": "string", "enum": ["full", "kiosk", "kid"]}},
            "required": ["mode"],
            "additionalProperties": False,
        },
        lambda ctx, a: ctx.post("/admin/system-info/screen-mode", {"mode": a.get("mode")}),
    ))
    tools.append(_tool(
        "set_volume",
        "Set the system audio volume (0-100, multiples of 5).",
        {
            "type": "object",
            "properties": {"level": {"type": "integer", "minimum": 0, "maximum": 100}},
            "required": ["level"],
            "additionalProperties": False,
        },
        lambda ctx, a: ctx.post("/admin/system-info/volume", {"level": a.get("level")}),
    ))
    tools.append(_tool(
        "set_music_volume",
        "Set the EmulationStation background music volume (0-100).",
        {
            "type": "object",
            "properties": {"level": {"type": "integer", "minimum": 0, "maximum": 100}},
            "required": ["level"],
            "additionalProperties": False,
        },
        lambda ctx, a: ctx.post("/admin/system-info/music-volume", {"level": a.get("level")}),
    ))
    tools.append(_tool(
        "set_screensaver_minutes",
        "Set the EmulationStation screensaver delay in minutes (0 disables it).",
        {
            "type": "object",
            "properties": {"minutes": {"type": "integer", "minimum": 0}},
            "required": ["minutes"],
            "additionalProperties": False,
        },
        lambda ctx, a: ctx.post("/admin/es-collections", {"screensaver_minutes": a.get("minutes")}),
    ))

    # -- Automation (read + write) ------------------------------------------
    tools.append(_tool(
        "get_automation",
        "Read automation status and configuration (idle volume, idle game exit, wifi recovery).",
        _NO_ARGS,
        lambda ctx, a: ctx.get("/admin/automation"),
    ))
    tools.append(_tool(
        "set_automation_idle_volume",
        "Update the idle-volume automation. Any subset of fields may be sent.",
        {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "idle_minutes": {"type": "integer", "minimum": 1},
                "target_volume": {"type": "integer", "minimum": 0, "maximum": 100},
            },
            "additionalProperties": False,
        },
        lambda ctx, a: ctx.post("/admin/automation/idle-volume", _compact(a)),
    ))
    tools.append(_tool(
        "set_automation_idle_game_exit",
        "Update the idle-game-exit automation. Any subset of fields may be sent.",
        {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "idle_minutes": {"type": "integer", "minimum": 1},
            },
            "additionalProperties": False,
        },
        lambda ctx, a: ctx.post("/admin/automation/idle-game-exit", _compact(a)),
    ))
    tools.append(_tool(
        "set_automation_wifi_recovery",
        "Enable or disable the wifi-recovery automation.",
        {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
            "additionalProperties": False,
        },
        lambda ctx, a: ctx.post("/admin/automation/wifi-recovery", {"enabled": a.get("enabled")}),
    ))

    # -- Swarm / networking (read) ----------------------------------------
    tools.append(_tool(
        "get_swarm",
        "Read the swarm overview: paired peers, pairing state, reachability.",
        _NO_ARGS,
        lambda ctx, a: ctx.get("/admin/swarm/overview"),
    ))
    tools.append(_tool(
        "get_tailnet",
        "Read Tailscale tailnet status and connected tailnet devices.",
        _NO_ARGS,
        lambda ctx, a: ctx.get("/admin/tailnet/status"),
    ))
    tools.append(_tool(
        "get_local_network",
        "Read local-network status and discovered peers.",
        _NO_ARGS,
        lambda ctx, a: ctx.get("/admin/local-network/status"),
    ))
    tools.append(_tool(
        "get_network_shares",
        "Read the network (NFS) share configuration and referenced peers.",
        _NO_ARGS,
        lambda ctx, a: ctx.get("/admin/network-shares"),
    ))
    tools.append(_tool(
        "get_vpn",
        "Read VPN (OpenVPN) status: connected/disconnected, config, verified IP.",
        _NO_ARGS,
        lambda ctx, a: ctx.get("/admin/vpn"),
    ))

    # -- Transfers / torrents (read) --------------------------------------
    tools.append(_tool(
        "get_transfers",
        "Read current and historical peer-to-peer transfers (downloads and uploads).",
        _NO_ARGS,
        lambda ctx, a: {
            "downloads": ctx.get("/admin/downloads"),
            "uploads": ctx.get("/admin/uploads"),
        },
    ))
    tools.append(_tool(
        "get_torrents",
        "Read torrents: completed, downloading, queued and errored, plus aria2 status.",
        _NO_ARGS,
        lambda ctx, a: ctx.get("/admin/torrents"),
    ))

    # -- System / diagnostics (read) -------------------------------------
    tools.append(_tool(
        "get_system_info",
        "Read device system info (host, OS, storage, memory, uptime; optional CPU speed).",
        {
            "type": "object",
            "properties": {"speed": {"type": "boolean", "description": "Include the CPU speed probe."}},
            "additionalProperties": False,
        },
        lambda ctx, a: ctx.get("/admin/system-info", speed=1 if a.get("speed") else 0),
    ))
    tools.append(_tool(
        "get_system_log",
        "Read the tail of a device log.",
        {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": [
                        "drone_stdout", "drone_stderr", "drone_activity",
                        "tailscaled", "es_launch_stdout", "es_launch_stderr",
                    ],
                },
                "lines": {"type": "integer", "minimum": 1, "maximum": 5000},
            },
            "required": ["source"],
            "additionalProperties": False,
        },
        lambda ctx, a: ctx.get("/admin/logs/" + _str(a.get("source")), lines=a.get("lines") or 200),
    ))
    tools.append(_tool(
        "get_gameplay_log",
        "Read recent gameplay-session history.",
        _NO_ARGS,
        lambda ctx, a: ctx.get("/admin/gameplay-logs"),
    ))
    tools.append(_tool(
        "get_notifications",
        "Read the Drone notification feed.",
        _NO_ARGS,
        lambda ctx, a: ctx.get("/admin/notifications"),
    ))

    # -- Emulator configs (read) ----------------------------------------
    tools.append(_tool(
        "list_emulator_configs",
        "List the emulator configuration files this Drone can read.",
        _NO_ARGS,
        lambda ctx, a: ctx.get("/admin/emulators"),
    ))
    tools.append(_tool(
        "read_emulator_config",
        "Read one emulator configuration file (identify it from list_emulator_configs).",
        {
            "type": "object",
            "properties": {
                "root": {"type": "string"},
                "relative_path": {"type": "string"},
                "max_bytes": {"type": "integer"},
            },
            "required": ["root", "relative_path"],
            "additionalProperties": False,
        },
        lambda ctx, a: ctx.get(
            "/admin/emulators/file",
            root=a.get("root"), relative_path=a.get("relative_path"),
            max_bytes=a.get("max_bytes"),
        ),
    ))

    # -- Email (read) --------------------------------------------------
    tools.append(_tool(
        "get_email_settings",
        "Read email/SMTP status: enabled flag, SMTP config, and notification preferences.",
        _NO_ARGS,
        lambda ctx, a: ctx.get("/admin/smtp"),
    ))

    return tools


def _compact(args: dict) -> dict:
    return {k: v for k, v in (args or {}).items() if v is not None}


def _scrape(ctx, a: dict) -> dict:
    provider = _str(a.get("provider") or "launchbox").strip() or "launchbox"
    if provider not in ("launchbox", "thegamesdb", "mobygames"):
        raise ValueError("provider must be launchbox, thegamesdb or mobygames")
    search = ctx.get(
        f"/admin/artwork/{provider}/search",
        system=a.get("system"), rom_id=a.get("rom_id"),
        rom_path=a.get("rom_path"), q=a.get("q"),
    )
    matches = (search or {}).get("matches") or []
    if not matches:
        return {"scraped": False, "reason": "no matches", "search": search}
    top = matches[0]
    key = top.get("game_key") or top.get("game_id") or top.get("id") or top.get("game_id_str")
    body = {
        "system": a.get("system"),
        "rom_id": a.get("rom_id"),
        "rom_path": a.get("rom_path"),
        "override_existing": bool(a.get("override_existing", False)),
        "import_metadata": bool(a.get("import_metadata", True)),
    }
    if provider == "launchbox":
        body["game_key"] = key
    else:
        body["game_id"] = _str(key)
    result = ctx.post(f"/admin/artwork/{provider}/apply", _compact(body))
    return {"scraped": True, "provider": provider, "match": top, "result": result}


TOOLS: List[dict] = _build()
_BY_NAME: Dict[str, dict] = {t["name"]: t for t in TOOLS}


def list_tools() -> List[dict]:
    return [
        {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
        for t in TOOLS
    ]


def get_tool(name: str) -> dict:
    return _BY_NAME.get(name)
