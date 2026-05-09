import ssl
from urllib.parse import parse_qs

try:
    from .route_config import API_PREFIX
except ImportError:
    from route_config import API_PREFIX  # type: ignore


class ApiRoutesMixin:
    def do_GET(self) -> None:
        try:
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
            if len(public_parts) == 5 and public_parts[0] == "public" and public_parts[1] == "systems" and public_parts[3] == "images":
                self._handle_public_image(public_parts[2], public_parts[4])
                return
            if len(parts) == 5 and parts[0] == "public" and parts[1] == "systems" and parts[3] == "images":
                self._handle_public_image(parts[2], parts[4])
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
                lines_raw = query_params.get("lines", ["200"])[0]
                try:
                    lines = int(lines_raw)
                except Exception:
                    lines = 200
                self._handle_admin_logs(parts[2], lines)
                return

            if len(parts) == 2 and parts[0] == "admin" and parts[1] == "system-info":
                self._handle_admin_system_info()
                return

            if len(parts) == 3 and parts[0] == "admin" and parts[1] == "artwork" and parts[2] == "missing":
                include_filesystem = str(query_params.get("include_filesystem", ["0"])[0]).strip().lower() in ("1", "true", "yes", "on")
                refresh = str(query_params.get("refresh", ["0"])[0]).strip().lower() in ("1", "true", "yes", "on")
                self._handle_admin_artwork_missing(include_filesystem=include_filesystem, refresh=refresh)
                return

            if len(parts) == 4 and parts[0] == "admin" and parts[1] == "artwork" and parts[2] == "launchbox" and parts[3] == "search":
                self._handle_admin_launchbox_search(
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
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
            pass
        except Exception as error:
            self.log_error('500 internal error "%s": %s', self.path.split("?", 1)[0], str(error))
            self._send_json(500, {"error": "internal server error"})

    def do_POST(self) -> None:
        try:
            raw_path, _, _ = self.path.partition("?")
            if raw_path == API_PREFIX:
                api_path = "/"
            elif raw_path.startswith(f"{API_PREFIX}/"):
                api_path = raw_path[len(API_PREFIX) :]
            else:
                api_path = raw_path
            parts = [part for part in api_path.split("/") if part]

            if not self.auth.check(self.headers.get("Authorization")):
                self._send_unauthorized()
                return

            if parts and parts[0] == "admin" and not self.settings.admin_enabled:
                self._send_json(403, {"error": "admin disabled"})
                return

            if len(parts) == 4 and parts[0] == "admin" and parts[1] == "integrations" and parts[2] == "overmind" and parts[3] == "config":
                payload = self._read_json_body()
                self._handle_admin_overmind_config(payload)
                return

            if len(parts) == 4 and parts[0] == "admin" and parts[1] == "integrations" and parts[2] == "overmind" and parts[3] == "start":
                payload = self._read_json_body()
                self._handle_admin_overmind_start(payload)
                return

            if len(parts) == 4 and parts[0] == "admin" and parts[1] == "artwork" and parts[2] == "launchbox" and parts[3] == "apply":
                payload = self._read_json_body()
                self._handle_admin_launchbox_apply(payload)
                return

            self._send_json(404, {"error": "not found"})
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
        except Exception as error:
            self.log_error('500 internal error "%s": %s', self.path.split("?", 1)[0], str(error))
            self._send_json(500, {"error": "internal server error"})
