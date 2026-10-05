"""Admin -> Integrations HTTP handlers (``/admin/integrations/...``).

Reached only through ``ApiRoutesMixin`` after the session gate and the
``admin_enabled`` check, on the browser/admin listener (never ``/peer/*``),
and never proxied to another Drone: every operation here configures *this*
machine. Mutations are POST-only (GETs are side-effect free); a POST must
also be same-origin when the browser sends ``Origin`` and must declare a JSON
body (uploads: multipart), so a cross-site page cannot drive the
code-executing endpoints even where the session cookie's SameSite=Lax would
not apply (e.g. the pre-authenticated loopback client).
"""

from pathlib import Path
from urllib.parse import urlparse

try:
    from ..common.multipart import boundary_from_content_type, parse_multipart_file_parts
    from ..integrations.registry import build_integration_registry
    from ..integrations.streamdeck.config import hex_id, safe_id
    from ..integrations.streamdeck.dependencies import DependencyError
    from ..integrations.streamdeck.images import MAX_UPLOAD_BYTES
    from ..integrations.streamdeck.manager import get_streamdeck_integration
    from ..integrations.streamdeck.paths import OwnershipError
    from ..integrations.streamdeck.scripts import ScriptInUseError
except ImportError:  # pragma: no cover - flat execution
    from common.multipart import boundary_from_content_type, parse_multipart_file_parts  # type: ignore
    from integrations.registry import build_integration_registry  # type: ignore
    from integrations.streamdeck.config import hex_id, safe_id  # type: ignore
    from integrations.streamdeck.dependencies import DependencyError  # type: ignore
    from integrations.streamdeck.images import MAX_UPLOAD_BYTES  # type: ignore
    from integrations.streamdeck.manager import get_streamdeck_integration  # type: ignore
    from integrations.streamdeck.paths import OwnershipError  # type: ignore
    from integrations.streamdeck.scripts import ScriptInUseError  # type: ignore


_UPLOAD_OVERHEAD = 64 * 1024


def _query(query_params: dict, name: str, default: str = "") -> str:
    values = query_params.get(name) or [default]
    return str(values[0] if values[0] is not None else default)


def _int_query(query_params: dict, name: str, default: int) -> int:
    try:
        return int(_query(query_params, name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


class HandlersIntegrationsMixin:
    def _require_integration_session(self) -> bool:
        # These endpoints grant persistent administrative code execution.
        # Require a real login even for the otherwise trusted loopback client.
        if self.auth.authenticate_request(self.headers) is None:
            self._send_unauthorized()
            return False
        if not self.settings.admin_enabled:
            self._send_json(403, {"error": "admin disabled"})
            return False
        return True

    def _streamdeck(self):
        return get_streamdeck_integration(self.settings, self.repository)

    def _integration_actor(self) -> str:
        try:
            session = self.auth.authenticate_request(self.headers, self.client_address[0] if self.client_address else None)
        except Exception:  # noqa: BLE001 - attribution is best-effort
            session = None
        return str((session or {}).get("username") or "")

    def _integration_origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return (parsed.scheme in ("http", "https") and not parsed.username and not parsed.password
                and bool(parsed.netloc) and parsed.netloc.lower() == str(self.headers.get("Host") or "").lower())

    def _integration_error(self, error: Exception) -> None:
        if isinstance(error, ScriptInUseError):
            self._send_json(409, {"error": str(error), "references": error.references})
        elif isinstance(error, (OwnershipError, ValueError)):
            self._send_json(400, {"error": str(error)})
        elif isinstance(error, (KeyError, LookupError, FileNotFoundError)):
            message = error.args[0] if isinstance(error, KeyError) and error.args else str(error)
            self._send_json(404, {"error": str(message) or "not found"})
        elif isinstance(error, (RuntimeError, DependencyError)):
            self._send_json(409, {"error": str(error)})
        else:
            raise error

    def _send_integration_file(self, path: Path, content_type: str) -> None:
        self._stream_file(path, content_type, extra_headers={"Content-Disposition": "inline"})

    # ------------------------------------------------------------------- GET
    def _handle_admin_integrations_get(self, parts: list, query_params: dict) -> None:
        if not self._require_integration_session():
            return
        try:
            self._dispatch_integrations_get(parts, query_params)
        except Exception as error:  # noqa: BLE001 - mapped to structured HTTP errors
            self._integration_error(error)

    def _dispatch_integrations_get(self, parts: list, query_params: dict) -> None:
        if not parts:
            cards = build_integration_registry(self.settings, self.repository).cards()
            self._send_json(200, {"integrations": cards, "scope": "local-only"})
            return
        if parts[0] != "streamdeck":
            raise KeyError("unknown integration")
        manager = self._streamdeck()
        sub = parts[1:]
        if not sub or sub == ["status"]:
            self._send_json(200, manager.get_status())
        elif sub == ["devices"]:
            self._send_json(200, {"devices": manager.devices()})
        elif sub == ["profiles"]:
            self._send_json(200, manager.profiles_view())
        elif sub == ["actions"]:
            self._send_json(200, {"actions": manager.actions.list(),
                                  "categories": ["Game", "System", "Audio", "State"]})
        elif sub == ["games"]:
            result = manager.games.search(
                _query(query_params, "q"), system=_query(query_params, "system") or None,
                limit=_int_query(query_params, "limit", 30), offset=_int_query(query_params, "offset", 0),
            )
            self._send_json(200, result)
        elif sub == ["games", "systems"]:
            self._send_json(200, {"systems": manager.games.systems()})
        elif sub == ["games", "artwork"]:
            game = {"system": safe_id(_query(query_params, "system"), "system"), "rom_path": _query(query_params, "rom_path")}
            path = manager.games.artwork_path(game, _query(query_params, "field", "auto") or "auto")
            if path is None:
                raise FileNotFoundError("no local artwork for this game")
            self._send_integration_file(path, self._guess_content_type(path))
        elif sub == ["scripts"]:
            self._send_json(200, {"scripts": manager.scripts.list()})
        elif len(sub) == 2 and sub[0] == "scripts":
            script = manager.scripts.get(sub[1])
            script["references"] = manager.script_references(script["id"])
            self._send_json(200, script)
        elif len(sub) == 2 and sub[0] == "jobs":
            self._send_json(200, manager.jobs.get(sub[1]))
        elif len(sub) == 2 and sub[0] == "images":
            path, meta = manager.images.get(sub[1])
            self._send_integration_file(path, meta["content_type"])
        elif len(sub) == 3 and sub[0] == "preview":
            device_id = safe_id(sub[1], "device id")
            key = int(sub[2]) if sub[2].isdigit() else -1
            if not 0 <= key < 64:
                raise ValueError("invalid key")
            path = manager.paths.assert_owned(manager.paths.preview_dir / device_id / f"{key}.png")
            if not path.is_file():
                raise FileNotFoundError("no rendered preview for this key yet")
            self._send_integration_file(path, "image/png")
        elif sub == ["logs"]:
            self._send_json(200, manager.log_tail(_query(query_params, "source", "runtime"),
                                                  _int_query(query_params, "lines", 200)))
        else:
            raise KeyError("not found")

    # ------------------------------------------------------------------- POST
    def _handle_admin_integrations_post(self, parts: list) -> None:
        if not self._require_integration_session():
            return
        if not self._integration_origin_ok():
            self._send_json(403, {"error": "cross-origin integration requests are not allowed"})
            return
        is_upload = parts == ["streamdeck", "images", "upload"]
        content_type = str(self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not is_upload and content_type != "application/json":
            self._send_json(415, {"error": "integration requests must send a JSON body (Content-Type: application/json)"})
            return
        try:
            self._dispatch_integrations_post(parts, is_upload)
        except Exception as error:  # noqa: BLE001 - mapped to structured HTTP errors
            self._integration_error(error)

    def _dispatch_integrations_post(self, parts: list, is_upload: bool) -> None:
        if not parts or parts[0] != "streamdeck" or len(parts) < 2:
            raise KeyError("not found")
        manager = self._streamdeck()
        actor = self._integration_actor()
        sub = parts[1:]
        if is_upload:
            self._handle_streamdeck_image_upload(manager)
            return
        body = self._read_json_body()
        if sub == ["enable"]:
            self._send_json(202, manager.enable(requested_by=actor))
        elif sub == ["disable"]:
            self._send_json(200, manager.disable(requested_by=actor))
        elif sub == ["repair"]:
            self._send_json(202, manager.repair(reinstall=body.get("reinstall") is True, requested_by=actor))
        elif sub == ["remove"]:
            self._send_json(200, manager.remove(include_configuration=body.get("include_configuration") is True,
                                                requested_by=actor))
        elif sub == ["apply"]:
            self._send_json(200, manager.apply(requested_by=actor))
        elif sub == ["test-connection"]:
            self._send_json(200, manager.test_connection())
        elif sub == ["settings"]:
            self._send_json(200, manager.update_settings(body, requested_by=actor))
        elif sub == ["rules"]:
            self._send_json(200, manager.update_rules(body.get("rules"), requested_by=actor))
        elif len(sub) == 3 and sub[0] == "devices" and sub[2] == "settings":
            self._send_json(200, manager.set_device_settings(sub[1], body, requested_by=actor))
        elif len(sub) == 3 and sub[0] == "devices" and sub[2] in ("identify", "test-button"):
            key = body.get("key") if sub[2] == "test-button" else None
            if sub[2] == "test-button" and not isinstance(key, int):
                raise ValueError("key must be an integer")
            result = manager.send_command(sub[2], device_id=safe_id(sub[1], "device id"), key=key, wait=5.0)
            self._send_json(200 if result.get("status") == "ok" else 409, result)
        elif sub == ["profiles"]:
            duplicate_from = body.get("duplicate_from")
            self._send_json(201, manager.config.create_profile(body.get("name"), str(duplicate_from) if duplicate_from else None))
        elif len(sub) == 3 and sub[0] == "profiles" and sub[2] == "update":
            self._send_json(200, manager.config.update_profile(sub[1], body))
        elif len(sub) == 3 and sub[0] == "profiles" and sub[2] == "duplicate":
            self._send_json(201, manager.config.create_profile(body.get("name") or "Profile copy", sub[1]))
        elif len(sub) == 3 and sub[0] == "profiles" and sub[2] == "delete":
            self._send_json(200, manager.config.delete_profile(sub[1]))
        elif len(sub) == 4 and sub[0] == "profiles" and sub[2] == "buttons":
            self._send_json(200, manager.set_button(sub[1], sub[3], body, requested_by=actor))
        elif sub == ["actions", "test"]:
            self._send_json(202, manager.start_test_action(body, requested_by=actor))
        elif sub == ["games", "test-launch"]:
            self._send_json(202, manager.start_test_action({**body, "action_type": "game"}, requested_by=actor))
        elif sub == ["scripts"]:
            self._send_json(201, manager.scripts.create(body))
        elif len(sub) == 3 and sub[0] == "scripts" and sub[2] == "update":
            self._send_json(200, manager.scripts.update(sub[1], body))
        elif len(sub) == 3 and sub[0] == "scripts" and sub[2] == "duplicate":
            self._send_json(201, manager.scripts.duplicate(sub[1]))
        elif len(sub) == 3 and sub[0] == "scripts" and sub[2] == "delete":
            script_id = hex_id(sub[1], "script id")
            manager.scripts.delete(script_id, manager.script_references(script_id))
            self._send_json(200, {"status": "deleted", "id": script_id})
        elif len(sub) == 3 and sub[0] == "scripts" and sub[2] == "test":
            self._send_json(202, manager.start_test_action({"action_type": "script", "script_id": sub[1]},
                                                           requested_by=actor))
        elif len(sub) == 3 and sub[0] == "jobs" and sub[2] == "cancel":
            self._send_json(200, manager.jobs.cancel(sub[1]))
        else:
            raise KeyError("not found")

    def _handle_streamdeck_image_upload(self, manager) -> None:
        content_type = str(self.headers.get("Content-Type") or "")
        if not content_type.lower().startswith("multipart/form-data"):
            raise ValueError("upload the image as multipart/form-data")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as error:
            raise ValueError("invalid content length") from error
        if length <= 0 or length > MAX_UPLOAD_BYTES + _UPLOAD_OVERHEAD:
            raise ValueError("images must be 5 MiB or smaller")
        parts = parse_multipart_file_parts(self.rfile.read(length), boundary_from_content_type(content_type))
        if len(parts) != 1:
            raise ValueError("upload exactly one image file")
        filename, declared_type, data = parts[0]
        meta = manager.images.save_upload(filename, data, declared_type)
        self._send_json(201, {**meta, "url": f"/v1/api/admin/integrations/streamdeck/images/{meta['id']}"})
