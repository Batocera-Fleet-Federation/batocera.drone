import ssl
from urllib.parse import parse_qs

try:
    from .route_config import API_PREFIX
except ImportError:
    from route_config import API_PREFIX  # type: ignore


class ApiRoutesMixin:
    def do_GET(self) -> None:
        try:
            if self._reject_if_ip_blocked():
                return
            raw_path, _, raw_query = self.path.partition("?")
            query_params = parse_qs(raw_query, keep_blank_values=True)
            if raw_path == API_PREFIX:
                api_path = "/"
            elif raw_path.startswith(f"{API_PREFIX}/"):
                api_path = raw_path[len(API_PREFIX) :]
            else:
                api_path = raw_path
            parts = [part for part in api_path.split("/") if part]

            public_parts = [part for part in raw_path.split("/") if part]
            if self._rate_limit_unauthenticated_external_request():
                return
            if len(public_parts) >= 2 and public_parts[0] == "static":
                self._handle_static_file("/".join(public_parts[1:]))
                return
            if len(public_parts) >= 2 and public_parts[0] == "content":
                self._handle_content_file("/".join(public_parts[1:]))
                return
            if raw_path == "/favicon.ico":
                self._send_empty(204)
                return
            if raw_path == "/health":
                self._handle_public_health()
                return
            if len(public_parts) == 5 and public_parts[0] == "public" and public_parts[1] == "systems" and public_parts[3] == "images":
                self._handle_public_image(public_parts[2], public_parts[4])
                return
            if len(parts) == 5 and parts[0] == "public" and parts[1] == "systems" and parts[3] == "images":
                self._handle_public_image(parts[2], parts[4])
                return

            if len(parts) == 2 and parts[0] == "peer" and parts[1] == "health":
                self._handle_peer_health()
                return

            if len(parts) == 3 and parts[0] == "peer" and parts[1] == "inventory":
                self._handle_peer_inventory(parts[2], query_params)
                return

            if len(parts) >= 4 and parts[0] == "peer" and parts[1] == "roms":
                self._handle_peer_rom_download(parts[2], "/".join(parts[3:]))
                return

            if len(parts) >= 4 and parts[0] == "peer" and parts[1] == "rom-manifest":
                self._handle_peer_rom_manifest(parts[2], "/".join(parts[3:]))
                return

            if len(parts) >= 3 and parts[0] == "peer" and parts[1] == "bios":
                self._handle_peer_bios_download("/".join(parts[2:]))
                return

            if len(parts) >= 4 and parts[0] == "peer" and parts[1] == "saves":
                self._handle_peer_save_download(parts[2], "/".join(parts[3:]))
                return

            if len(parts) >= 5 and parts[0] == "peer" and parts[1] == "artwork":
                self._handle_peer_artwork_download(parts[2], parts[3], "/".join(parts[4:]))
                return

            if not self.auth.check(self.headers.get("Authorization")):
                self._send_unauthorized()
                return

            if api_path == "/":
                self._handle_root_html()
                return

            if api_path == "/swagger":
                self._handle_swagger_html()
                return

            if api_path == "/openapi.json":
                self._handle_openapi_json()
                return

            if api_path == "/downloads":
                self._handle_download_sitemap()
                return

            if api_path == "/search":
                self._handle_search((query_params.get("q", [""])[0]), query_params.get("system", [None])[0])
                return

            if api_path == "/theme/meta":
                self._handle_theme_meta()
                return

            if api_path == "/theme/backgrounds":
                self._handle_theme_backgrounds()
                return

            if api_path == "/theme/logos":
                self._handle_theme_logos()
                return

            if api_path == "/theme/images":
                limit_raw = query_params.get("limit", ["500"])[0]
                offset_raw = query_params.get("offset", ["0"])[0]
                query = query_params.get("q", [None])[0]
                system_filter = query_params.get("system", [None])[0]
                systems_raw = query_params.get("systems", [None])[0]
                system_filters = [part.strip() for part in (systems_raw or "").split(",") if part.strip()]
                try:
                    limit = int(limit_raw)
                except Exception:
                    limit = 500
                try:
                    offset = int(offset_raw)
                except Exception:
                    offset = 0
                self._handle_theme_images(limit, offset, query, system_filter, system_filters=system_filters)
                return

            if len(parts) == 3 and parts[0] == "theme" and parts[1] == "system":
                self._handle_system_theme_meta(parts[2])
                return

            if api_path == "/systems":
                self._handle_systems()
                return

            if api_path == "/bios":
                limit_raw = query_params.get("limit", ["100"])[0]
                offset_raw = query_params.get("offset", ["0"])[0]
                query = query_params.get("q", [None])[0]
                systems_raw = query_params.get("systems", [None])[0]
                system_filters = [part.strip() for part in (systems_raw or "").split(",") if part.strip()]
                try:
                    limit = int(limit_raw)
                except Exception:
                    limit = 100
                try:
                    offset = int(offset_raw)
                except Exception:
                    offset = 0
                self._handle_bios_list(limit=limit, offset=offset, query=query, system_filters=system_filters)
                return

            if len(parts) == 2 and parts[0] == "systems":
                self._handle_rom_list(parts[1])
                return

            if len(parts) == 2 and parts[0] == "bios":
                self._handle_bios_download(parts[1])
                return

            if len(parts) == 3 and parts[0] == "systems" and parts[2] == "images":
                self._handle_images_list(parts[1])
                return

            if len(parts) == 3 and parts[0] == "systems" and parts[2] == "videos":
                self._handle_videos_list(parts[1])
                return

            if len(parts) == 3 and parts[0] == "systems":
                self._handle_download(parts[1], "roms", parts[2])
                return

            if len(parts) == 4 and parts[0] == "systems" and parts[2] == "roms":
                self._handle_download(parts[1], "roms", parts[3])
                return

            if len(parts) == 5 and parts[0] == "systems" and parts[2] == "roms" and parts[4] == "fingerprint":
                self._handle_rom_fingerprint(parts[1], parts[3])
                return

            if len(parts) == 4 and parts[0] == "systems" and parts[2] == "images":
                self._handle_image_file_or_download(parts[1], parts[3])
                return

            if len(parts) == 4 and parts[0] == "systems" and parts[2] == "videos":
                self._handle_download(parts[1], "videos", parts[3])
                return

            if len(parts) >= 3 and parts[0] == "theme" and parts[1] == "assets":
                relative_path = "/".join(parts[2:])
                self._handle_theme_asset(relative_path)
                return

            if parts and parts[0] == "admin" and not self.settings.admin_enabled:
                self._send_json(403, {"error": "admin disabled"})
                return

            if len(parts) == 3 and parts[0] == "admin" and parts[1] == "logs":
                lines_raw = query_params.get("lines", ["100"])[0]
                try:
                    lines = int(lines_raw)
                except Exception:
                    lines = 100
                self._handle_admin_logs(parts[2], lines)
                return

            if len(parts) == 2 and parts[0] == "admin" and parts[1] == "gameplay-logs":
                self._handle_admin_gameplay_logs()
                return

            if len(parts) == 2 and parts[0] == "admin" and parts[1] == "system-info":
                include_speed = str(query_params.get("speed", ["0"])[0]).strip().lower() in ("1", "true", "yes", "on")
                self._handle_admin_system_info(include_speed=include_speed)
                return

            if len(parts) == 2 and parts[0] == "admin" and parts[1] == "downloads":
                self._handle_admin_downloads()
                return

            if len(parts) == 2 and parts[0] == "admin" and parts[1] == "asset-cache":
                self._handle_admin_asset_cache()
                return

            if len(parts) == 3 and parts[0] == "admin" and parts[1] == "api" and parts[2] == "status":
                self._handle_admin_api_status()
                return

            if len(parts) == 2 and parts[0] == "admin" and parts[1] == "automation":
                self._handle_admin_automation_status()
                return

            if len(parts) == 3 and parts[0] == "admin" and parts[1] == "api" and parts[2] == "certificate":
                self._handle_admin_api_certificate()
                return

            if len(parts) == 3 and parts[0] == "admin" and parts[1] == "artwork" and parts[2] == "missing":
                include_filesystem = str(query_params.get("include_filesystem", ["0"])[0]).strip().lower() in ("1", "true", "yes", "on")
                refresh = str(query_params.get("refresh", ["0"])[0]).strip().lower() in ("1", "true", "yes", "on")
                try:
                    limit = int(query_params.get("limit", ["200"])[0])
                except Exception:
                    limit = 200
                try:
                    offset = int(query_params.get("offset", ["0"])[0])
                except Exception:
                    offset = 0
                art_fields = [
                    part.strip()
                    for value in query_params.get("fields", [])
                    for part in value.split(",")
                    if part.strip()
                ]
                system_filters = [
                    part.strip()
                    for value in query_params.get("systems", [])
                    for part in value.split(",")
                    if part.strip()
                ]
                self._handle_admin_artwork_missing(
                    include_filesystem=include_filesystem,
                    refresh=refresh,
                    limit=limit,
                    offset=offset,
                    art_fields=art_fields,
                    system_filters=system_filters,
                    query=query_params.get("q", [""])[0],
                    rom_status=query_params.get("rom_status", ["any"])[0],
                )
                return

            if len(parts) == 4 and parts[0] == "admin" and parts[1] == "artwork" and parts[2] == "launchbox" and parts[3] == "search":
                self._handle_admin_launchbox_search(
                    query_params.get("system", [""])[0],
                    query_params.get("rom_id", [""])[0],
                    query_params.get("rom_path", [""])[0],
                    query_params.get("q", [""])[0],
                )
                return

            if len(parts) == 4 and parts[0] == "admin" and parts[1] == "artwork" and parts[2] == "thegamesdb" and parts[3] == "search":
                self._handle_admin_thegamesdb_artwork_search(
                    query_params.get("system", [""])[0],
                    query_params.get("rom_id", [""])[0],
                    query_params.get("rom_path", [""])[0],
                    query_params.get("q", [""])[0],
                )
                return

            if len(parts) == 4 and parts[0] == "admin" and parts[1] == "artwork" and parts[2] == "mobygames" and parts[3] == "search":
                self._handle_admin_mobygames_artwork_search(
                    query_params.get("system", [""])[0],
                    query_params.get("rom_id", [""])[0],
                    query_params.get("rom_path", [""])[0],
                    query_params.get("q", [""])[0],
                )
                return

            if len(parts) == 4 and parts[0] == "admin" and parts[1] == "integrations" and parts[2] == "overmind" and parts[3] == "status":
                self._handle_admin_overmind_status()
                return

            if len(parts) == 4 and parts[0] == "admin" and parts[1] == "integrations" and parts[2] == "overmind" and parts[3] == "actions":
                self._handle_admin_overmind_actions()
                return

            if len(parts) == 2 and parts[0] == "admin" and parts[1] == "network-mode":
                self._handle_admin_network_mode()
                return

            if len(parts) == 3 and parts[0] == "admin" and parts[1] == "local-network" and parts[2] == "status":
                self._handle_admin_local_network_status()
                return

            if len(parts) == 5 and parts[0] == "admin" and parts[1] == "local-network" and parts[2] == "peers" and parts[4] == "assets":
                self._handle_admin_local_peer_assets(parts[3], query_params)
                return

            if len(parts) == 2 and parts[0] == "admin" and parts[1] == "emulators":
                self._handle_admin_emulators()
                return

            if len(parts) == 3 and parts[0] == "admin" and parts[1] == "emulators" and parts[2] == "file":
                max_bytes_raw = query_params.get("max_bytes", ["131072"])[0]
                try:
                    max_bytes = int(max_bytes_raw)
                except Exception:
                    max_bytes = 131072
                self._handle_admin_emulator_file(
                    query_params.get("root", [""])[0],
                    query_params.get("relative_path", [""])[0],
                    max_bytes,
                )
                return

            if len(parts) == 3 and parts[0] == "admin" and parts[1] == "configs":
                if parts[2] == "sources":
                    self._handle_admin_config_sources()
                    return
                max_bytes_raw = query_params.get("max_bytes", ["131072"])[0]
                format_value = query_params.get("format", ["json"])[0]
                try:
                    max_bytes = int(max_bytes_raw)
                except Exception:
                    max_bytes = 131072
                self._handle_admin_config(parts[2], max_bytes, format_value)
                return

            self._send_json(404, {"error": "not found"})
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
        except FileNotFoundError:
            self._send_json(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            # The client went away mid-response; nothing we can or need to send.
            pass
        except Exception as error:
            # Everything else (including a failed upstream peer fetch, which raises
            # URLError/OSError/SSLError) must be logged AND reported, not silently
            # swallowed -- otherwise the browser just sees "Failed to fetch" with no
            # server-side trace. Guard the reply in case the client is already gone.
            self.log_error('500 internal error "%s": %s: %s', self.path.split("?", 1)[0], error.__class__.__name__, str(error))
            try:
                self._send_json(500, {"error": str(error) or "internal server error"})
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
                pass

    def do_POST(self) -> None:
        try:
            if self._reject_if_ip_blocked():
                return
            raw_path, _, _ = self.path.partition("?")
            if raw_path == API_PREFIX:
                api_path = "/"
            elif raw_path.startswith(f"{API_PREFIX}/"):
                api_path = raw_path[len(API_PREFIX) :]
            else:
                api_path = raw_path
            parts = [part for part in api_path.split("/") if part]

            if self._rate_limit_unauthenticated_external_request():
                return

            if len(parts) == 2 and parts[0] == "peer" and parts[1] == "pair":
                payload = self._read_json_body()
                self._handle_peer_pair(payload)
                return

            if not self.auth.check(self.headers.get("Authorization")):
                self._send_unauthorized()
                return

            if parts and parts[0] == "admin" and not self.settings.admin_enabled:
                self._send_json(403, {"error": "admin disabled"})
                return

            if len(parts) == 3 and parts[0] == "admin" and parts[1] == "credentials" and parts[2] == "update":
                payload = self._read_json_body()
                self._handle_admin_credentials_update(payload)
                return

            if len(parts) == 4 and parts[0] == "admin" and parts[1] == "integrations" and parts[2] == "overmind" and parts[3] == "config":
                payload = self._read_json_body()
                self._handle_admin_overmind_config(payload)
                return

            if len(parts) == 2 and parts[0] == "admin" and parts[1] == "network-mode":
                payload = self._read_json_body()
                self._handle_admin_network_mode_update(payload)
                return

            if len(parts) == 3 and parts[0] == "admin" and parts[1] == "local-network" and parts[2] == "discover":
                self._handle_admin_local_network_discover()
                return

            if len(parts) == 4 and parts[0] == "admin" and parts[1] == "local-network" and parts[2] == "pairing-code" and parts[3] == "rotate":
                self._handle_admin_local_pairing_code_rotate()
                return

            if len(parts) == 5 and parts[0] == "admin" and parts[1] == "local-network" and parts[2] == "peers" and parts[4] == "pair":
                payload = self._read_json_body()
                self._handle_admin_local_peer_pair(parts[3], payload)
                return

            if len(parts) == 5 and parts[0] == "admin" and parts[1] == "local-network" and parts[2] == "peers" and parts[4] == "forget":
                self._handle_admin_local_peer_forget(parts[3])
                return

            if len(parts) == 3 and parts[0] == "admin" and parts[1] == "local-network" and parts[2] == "sync":
                payload = self._read_json_body()
                self._handle_admin_local_sync(payload)
                return

            if len(parts) == 3 and parts[0] == "admin" and parts[1] == "local-network" and parts[2] == "sync-bulk":
                payload = self._read_json_body()
                self._handle_admin_local_sync_bulk(payload)
                return

            if len(parts) == 4 and parts[0] == "admin" and parts[1] == "integrations" and parts[2] == "overmind" and parts[3] == "start":
                payload = self._read_json_body()
                self._handle_admin_overmind_start(payload)
                return

            if len(parts) == 4 and parts[0] == "admin" and parts[1] == "integrations" and parts[2] == "overmind" and parts[3] == "claim-ownership":
                payload = self._read_json_body()
                self._handle_admin_overmind_claim_ownership(payload)
                return

            if len(parts) == 5 and parts[0] == "admin" and parts[1] == "integrations" and parts[2] == "overmind" and parts[3] == "swarm" and parts[4] == "connect":
                self._handle_admin_overmind_swarm_connect()
                return

            if len(parts) == 5 and parts[0] == "admin" and parts[1] == "integrations" and parts[2] == "overmind" and parts[3] == "swarm" and parts[4] == "disconnect":
                self._handle_admin_overmind_swarm_disconnect()
                return

            if len(parts) == 4 and parts[0] == "admin" and parts[1] == "api" and parts[2] == "certificate" and parts[3] == "rotate":
                self._handle_admin_api_certificate_rotate()
                return

            if len(parts) == 3 and parts[0] == "admin" and parts[1] == "automation" and parts[2] == "idle-volume":
                payload = self._read_json_body()
                self._handle_admin_automation_idle_volume(payload)
                return

            if len(parts) == 3 and parts[0] == "admin" and parts[1] == "system" and parts[2] == "update-drone":
                self._handle_admin_drone_update()
                return

            if len(parts) == 3 and parts[0] == "admin" and parts[1] == "asset-cache" and parts[2] == "purge":
                self._handle_admin_asset_cache_purge()
                return

            if len(parts) == 3 and parts[0] == "admin" and parts[1] == "asset-cache" and parts[2] == "clear-pending":
                self._handle_admin_asset_cache_clear_pending()
                return

            if len(parts) == 4 and parts[0] == "admin" and parts[1] == "downloads" and parts[3] == "cancel":
                self._handle_admin_download_cancel(parts[2])
                return

            if len(parts) == 4 and parts[0] == "admin" and parts[1] == "downloads" and parts[3] == "retry":
                self._handle_admin_download_retry(parts[2])
                return

            if len(parts) == 3 and parts[0] == "admin" and parts[1] == "downloads" and parts[2] == "pause":
                self._handle_admin_downloads_pause()
                return

            if len(parts) == 3 and parts[0] == "admin" and parts[1] == "downloads" and parts[2] == "resume":
                self._handle_admin_downloads_resume()
                return

            if len(parts) == 3 and parts[0] == "admin" and parts[1] == "downloads" and parts[2] == "clear":
                self._handle_admin_downloads_clear()
                return

            if len(parts) == 4 and parts[0] == "admin" and parts[1] == "artwork" and parts[2] == "launchbox" and parts[3] == "apply":
                payload = self._read_json_body()
                self._handle_admin_launchbox_apply(payload)
                return

            if len(parts) == 4 and parts[0] == "admin" and parts[1] == "artwork" and parts[2] == "thegamesdb" and parts[3] == "apply":
                payload = self._read_json_body()
                self._handle_admin_thegamesdb_artwork_apply(payload)
                return

            if len(parts) == 4 and parts[0] == "admin" and parts[1] == "artwork" and parts[2] == "mobygames" and parts[3] == "apply":
                payload = self._read_json_body()
                self._handle_admin_mobygames_artwork_apply(payload)
                return

            if len(parts) == 4 and parts[0] == "admin" and parts[1] == "artwork" and parts[2] == "gamelist" and parts[3] == "remove":
                payload = self._read_json_body()
                self._handle_admin_gamelist_remove(payload)
                return

            if len(parts) == 4 and parts[0] == "admin" and parts[1] == "artwork" and parts[2] == "gamelist" and parts[3] == "update":
                payload = self._read_json_body()
                self._handle_admin_gamelist_update(payload)
                return

            if len(parts) == 4 and parts[0] == "admin" and parts[1] == "artwork" and parts[2] == "gamelist" and parts[3] == "remove-missing":
                payload = self._read_json_body()
                self._handle_admin_gamelist_remove_missing(payload)
                return

            if len(parts) == 3 and parts[0] == "admin" and parts[1] == "artwork" and parts[2] == "upload":
                self._handle_admin_artwork_upload()
                return

            self._send_json(404, {"error": "not found"})
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self.log_error('500 internal error "%s": %s: %s', self.path.split("?", 1)[0], error.__class__.__name__, str(error))
            try:
                self._send_json(500, {"error": str(error) or "internal server error"})
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
                pass
