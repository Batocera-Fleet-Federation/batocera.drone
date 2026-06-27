"""The Drone's FastAPI app: the typed, auto-OpenAPI surface for /v1/api/*.

Phase 1 owns the OpenAPI/Swagger surface (replacing the hand-written spec, merged with the
remaining legacy paths so docs stay complete during the phased migration) plus a typed
``/api-info`` endpoint that exercises the response-model -> OpenAPI -> proxy vertical.

Served in-process by uvicorn (see api_bridge) and reached through the stdlib server's reverse
proxy, which has already enforced auth + front-door rate limiting. Imports FastAPI/pydantic, so
it is loaded only behind the DRONE_API_FASTAPI_BRIDGE flag (vendored deps) or in tests.
"""

from fastapi import Depends, FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, RedirectResponse

try:  # package vs. flat-module execution (mirrors app/main.py)
    from .route_config import API_PREFIX, api_url
    from .api_models import ApiInfoResponse, OvermindStatusResponse
    from .app_version import drone_app_version
except ImportError:  # pragma: no cover - flat execution path
    from route_config import API_PREFIX, api_url  # type: ignore
    from api_models import ApiInfoResponse, OvermindStatusResponse  # type: ignore
    from app_version import drone_app_version  # type: ignore


# Paths (prefix-stripped, as api_routes.py sees them) the stdlib dispatch should proxy here.
OWNED_EXACT = {"/openapi.json", "/api-info", "/swagger", "/admin/integrations/overmind/status"}
OWNED_PREFIXES = ("/docs",)


_settings = None


def set_settings(settings) -> None:
    """Bind the running server's Settings so FastAPI routes use the same instance."""
    global _settings
    _settings = settings


def get_settings():
    global _settings
    if _settings is None:  # pragma: no cover - exercised only outside the bridge
        try:
            from .drone_api import Settings
        except ImportError:
            from drone_api import Settings  # type: ignore
        _settings = Settings.from_env()
    return _settings

app = FastAPI(
    title="Batocera Drone API",
    version=drone_app_version(),
    docs_url=api_url("/docs"),
    openapi_url=api_url("/openapi.json"),
    redoc_url=None,
)


@app.get(api_url("/api-info"), response_model=ApiInfoResponse, tags=["meta"])
def api_info() -> ApiInfoResponse:
    return ApiInfoResponse(
        app_version=drone_app_version(),
        api_prefix=API_PREFIX,
        docs_url=api_url("/docs"),
        openapi_url=api_url("/openapi.json"),
        migrated_paths=sorted(api_url(p) for p in OWNED_EXACT | {pre for pre in OWNED_PREFIXES}),
    )


@app.get(api_url("/swagger"), include_in_schema=False)
def swagger_redirect() -> RedirectResponse:
    return RedirectResponse(api_url("/docs"))


@app.get(api_url("/admin/integrations/overmind/status"), response_model=OvermindStatusResponse, tags=["admin"])
def admin_overmind_status(settings=Depends(get_settings)):
    # Matches the legacy admin gate (api_routes.py:205) since the proxy bypasses it.
    if not settings.admin_enabled:
        return JSONResponse(status_code=403, content={"error": "admin disabled"})
    try:
        from .drone_api import build_overmind_status
    except ImportError:  # pragma: no cover - flat execution
        from drone_api import build_overmind_status  # type: ignore
    return build_overmind_status(settings)


def _merged_openapi() -> dict:
    """FastAPI-generated schema for migrated routes, merged with the legacy hand-written spec
    so /openapi.json documents both during the phased migration."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    try:  # legacy spec lives in the stdlib module; import lazily to avoid an import cycle
        try:
            from .drone_api import OPENAPI_SPEC as LEGACY  # type: ignore
        except ImportError:  # pragma: no cover
            from drone_api import OPENAPI_SPEC as LEGACY  # type: ignore
    except Exception:
        LEGACY = {}
    for path, item in (LEGACY or {}).get("paths", {}).items():
        prefixed = path if path.startswith(API_PREFIX) else api_url(path)
        schema.setdefault("paths", {}).setdefault(prefixed, item)
    app.openapi_schema = schema
    return schema


app.openapi = _merged_openapi  # type: ignore[assignment]
