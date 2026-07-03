"""Reverse-proxy bridge: runs the FastAPI app under uvicorn (localhost, in-process daemon
thread) and lets the stdlib TLS server delegate migrated /v1/api/* routes to it.

Design constraints (see plan + drone-tls-handshake / p2p-transfer skills):
- The stdlib ``DroneThreadingHTTPServer`` stays the only public TLS/mTLS listener and keeps
  enforcing IP-block + unauth rate limit + Basic auth BEFORE anything is proxied here.
- ``/peer/*`` is never proxied (mTLS, getpeercert, live cert injection stay on stdlib).
- Entirely OPT-IN (DRONE_API_FASTAPI_BRIDGE=1) and fully guarded: any import/startup failure
  leaves the bridge inactive so the stdlib server keeps serving 100% of routes (no outage).

This top-level module imports only stdlib; FastAPI/uvicorn/pydantic are imported lazily inside
``maybe_start`` after the vendored deps are put on sys.path, so importing this module is safe on
a stdlib-only device.
"""

import os
import platform
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError
from urllib.request import Request, urlopen

_BRIDGE: Optional["_Bridge"] = None
_START_LOCK = threading.Lock()

# Headers safe to forward to / return from the localhost FastAPI app.
_FORWARD_REQUEST_HEADERS = ("Authorization", "Content-Type", "Accept", "Accept-Language")


def _vendor_dirs() -> list:
    base = Path(__file__).resolve().parent.parent / "vendor"
    # vendor/common holds the pure-Python deps; vendor/<machine> holds the arch-specific
    # pydantic_core binary (see scripts/vendor_deps.sh).
    return [base / "common", base / platform.machine()]


def _activate_vendor() -> None:
    for path in _vendor_dirs():
        if path.exists() and str(path) not in sys.path:
            sys.path.insert(0, str(path))


def _free_localhost_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Bridge:
    def __init__(self, port: int, owned_exact: set, owned_prefixes: tuple):
        self.port = port
        self.owned_exact = owned_exact
        self.owned_prefixes = tuple(owned_prefixes)

    def owns(self, api_path: str) -> bool:
        return api_path in self.owned_exact or api_path.startswith(self.owned_prefixes)

    def proxy(self, handler, method: str) -> None:
        """Forward the current stdlib request (already authenticated) to the FastAPI app and
        write its response back through the handler."""
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        body = handler.rfile.read(length) if length > 0 else None
        url = f"http://127.0.0.1:{self.port}{handler.path}"
        headers = {}
        for name in _FORWARD_REQUEST_HEADERS:
            value = handler.headers.get(name)
            if value:
                headers[name] = value
        request = Request(url, data=body, method=method, headers=headers)
        try:
            with urlopen(request, timeout=30) as response:
                status = response.status
                data = response.read()
                content_type = response.headers.get("Content-Type", "application/json")
        except HTTPError as error:  # FastAPI returned a 4xx/5xx — relay it verbatim
            status = error.code
            data = error.read()
            content_type = error.headers.get("Content-Type", "application/json") if error.headers else "application/json"
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(data)))
        if hasattr(handler, "_send_security_headers"):
            handler._send_security_headers()
        handler.end_headers()
        if method != "HEAD":
            handler.wfile.write(data)


def maybe_start(settings=None) -> Optional["_Bridge"]:
    """Start the FastAPI bridge if enabled + importable; otherwise return None (legacy serves)."""
    global _BRIDGE
    if _BRIDGE is not None:
        return _BRIDGE
    if os.environ.get("DRONE_API_FASTAPI_BRIDGE", "0") != "1":
        return None
    with _START_LOCK:
        if _BRIDGE is not None:
            return _BRIDGE
        try:
            _activate_vendor()
            import uvicorn  # noqa: WPS433 (lazy: needs vendored deps on sys.path)

            try:
                from .api_app import OWNED_EXACT, OWNED_PREFIXES, app, set_settings
            except ImportError:  # pragma: no cover - flat execution
                from web.api_app import OWNED_EXACT, OWNED_PREFIXES, app, set_settings  # type: ignore

            if settings is not None:
                set_settings(settings)
            port = _free_localhost_port()
            config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
            server = uvicorn.Server(config)
            # uvicorn installs signal handlers, which is illegal off the main thread.
            server.install_signal_handlers = lambda: None  # type: ignore[assignment]
            thread = threading.Thread(target=server.run, name="drone-api-bridge", daemon=True)
            thread.start()
            for _ in range(200):  # up to ~10s for uvicorn to bind
                if getattr(server, "started", False):
                    break
                time.sleep(0.05)
            if not getattr(server, "started", False):
                raise RuntimeError("uvicorn did not start within timeout")
            _BRIDGE = _Bridge(port, set(OWNED_EXACT), tuple(OWNED_PREFIXES))
            print(f"FastAPI bridge active on 127.0.0.1:{port} (owns {sorted(OWNED_EXACT)} + {OWNED_PREFIXES})", flush=True)
            return _BRIDGE
        except Exception as error:  # noqa: BLE001 - never let the optional layer break serving
            print(
                f"FastAPI bridge disabled, falling back to legacy stdlib dispatch: {error}",
                file=sys.stderr,
                flush=True,
            )
            _BRIDGE = None
            return None


def active() -> Optional["_Bridge"]:
    return _BRIDGE
