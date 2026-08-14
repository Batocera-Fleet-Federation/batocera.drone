"""Stdlib-only HTTP client for the Drone API (session-cookie auth).

No third-party HTTP library — ``urllib.request`` + ``http.cookiejar`` handle
everything the Drone API needs (JSON in/out, a persisted session cookie).
This keeps ports-client's only vendored dependency the UI toolkit itself
(see ../requirements-vendor.txt).
"""

import http.client
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from http.cookiejar import LWPCookieJar
from typing import Any, Optional

from .config import ClientConfig
from .errors import AuthenticationError, DroneApiError

SESSION_COOKIE_NAME = "drone_session"
_TIMEOUT_SECONDS = 15


def _build_ssl_context(config: ClientConfig) -> Optional[ssl.SSLContext]:
    if config.http_only:
        return None
    if not config.ca_cert_file.exists():
        raise DroneApiError(
            f"Drone's TLS certificate was not found at {config.ca_cert_file} — "
            "is Drone installed and has it started at least once on this device?"
        )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=str(config.ca_cert_file))
    # Whichever cert Drone actually presents here (the reused device-identity
    # drone.crt in the common case, or a CN=localhost self-signed server.crt
    # as a last-resort fallback -- see config.py) has no SAN for 127.0.0.1,
    # so strict hostname checking would fail even though we've already
    # pinned the exact cert file Drone itself generated — the same trust
    # model the codebase already uses for peer-to-peer mTLS (pin the cert,
    # not the hostname).
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    return context


class DroneApiClient:
    """Talks to the Drone running on this device, exactly like the browser UI does."""

    def __init__(self, config: ClientConfig):
        self.config = config
        self.cookie_jar = LWPCookieJar(str(config.session_cookie_path))
        self._load_session()
        ssl_context = _build_ssl_context(config)
        handlers = [urllib.request.HTTPCookieProcessor(self.cookie_jar)]
        if ssl_context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=ssl_context))
        self._opener = urllib.request.build_opener(*handlers)

    def _load_session(self) -> None:
        try:
            self.cookie_jar.load(ignore_discard=True, ignore_expires=True)
        except (FileNotFoundError, OSError):
            pass

    def _save_session(self) -> None:
        self.config.session_cookie_path.parent.mkdir(parents=True, exist_ok=True)
        self.cookie_jar.save(ignore_discard=True, ignore_expires=True)

    @property
    def has_session_cookie(self) -> bool:
        return any(cookie.name == SESSION_COOKIE_NAME for cookie in self.cookie_jar)

    def _send(self, request: urllib.request.Request, *, method: str, path: str, timeout: float = _TIMEOUT_SECONDS) -> Any:
        # Every request is logged with its own elapsed time so a timeout can
        # be told apart from a fast failure after the fact, from the shared
        # log file alone -- this was genuinely hard to debug without it (see
        # ports-client/README.md's "Request Assets: same false-negative..."
        # section, root-caused only by cross-referencing the Drone's own
        # server-side log at the same timestamp, not from ports-client at all).
        print(f"-> {method} {path} timeout={timeout}s")
        started_at = time.monotonic()
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read()
                self._save_session()
        except urllib.error.HTTPError as error:
            elapsed = time.monotonic() - started_at
            raw = error.read()
            payload = _try_parse_json(raw)
            message = _extract_error_message(payload)
            print(f"<- {method} {path} HTTP {error.code} in {elapsed:.2f}s: {message or raw[:200]!r}", file=sys.stderr)
            if error.code == 401:
                raise AuthenticationError(message or "not authenticated", status=401) from None
            raise DroneApiError(message or f"{method} {path} failed: HTTP {error.code}", status=error.code) from None
        except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError, OSError) as error:
            elapsed = time.monotonic() - started_at
            print(f"<- {method} {path} FAILED after {elapsed:.2f}s (timeout budget {timeout}s): {error.__class__.__name__}: {error}", file=sys.stderr)
            raise DroneApiError(f"could not reach Drone at {self.config.base_url}: {error}") from None

        elapsed = time.monotonic() - started_at
        print(f"<- {method} {path} 200 in {elapsed:.2f}s")
        if not raw:
            return None
        return _try_parse_json(raw)

    def _request(self, method: str, path: str, *, body: Optional[dict] = None, timeout: float = _TIMEOUT_SECONDS) -> Any:
        url = f"{self.config.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        return self._send(request, method=method, path=path, timeout=timeout)

    def get(self, path: str, *, timeout: float = _TIMEOUT_SECONDS) -> Any:
        return self._request("GET", path, timeout=timeout)

    def post(self, path: str, body: Optional[dict] = None, *, timeout: float = _TIMEOUT_SECONDS) -> Any:
        return self._request("POST", path, body=body or {}, timeout=timeout)

    def post_multipart(self, path: str, field_name: str, filename: str, content: bytes) -> Any:
        """POST a single file as multipart/form-data -- stdlib-only, hand-built
        (no third-party HTTP library, per the repo-wide rule). Framed exactly
        as app/common/multipart.py's parse_multipart_files expects: the
        field name isn't actually checked server-side, but "config" is used
        here for consistency with drone.js's own upload convention.
        """
        url = f"{self.config.base_url}{path}"
        boundary = uuid.uuid4().hex
        body = (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
            f'Content-Type: application/octet-stream\r\n\r\n'
        ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        return self._send(request, method="POST", path=path)

    # --- auth -----------------------------------------------------------

    def login(self, username: str, password: str) -> str:
        """Log in and persist the resulting session cookie to disk. Returns the username."""
        result = self.post("/auth/login", {"username": username, "password": password})
        return result.get("username", username) if isinstance(result, dict) else username

    def session_status(self) -> dict:
        result = self.get("/auth/session")
        return result if isinstance(result, dict) else {"authenticated": False}

    def logout(self) -> None:
        try:
            self.post("/auth/logout")
        finally:
            for cookie in list(self.cookie_jar):
                if cookie.name == SESSION_COOKIE_NAME:
                    self.cookie_jar.clear(cookie.domain, cookie.path, cookie.name)
            self._save_session()


def _try_parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _extract_error_message(payload: Any) -> Optional[str]:
    """Most error bodies use a singular ``error`` string, but some (e.g.
    VPN connect/disconnect) use a plural ``errors`` array instead -- drone.js
    itself shipped with this exact gap once (only reading ``error``, so a
    real ``errors``-shaped failure fell through to a generic "Request
    failed: 400" with the actual reason discarded). Handle both here so
    every screen's error message is the real one, not a fallback."""
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if error:
        return str(error)
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        return "; ".join(str(item) for item in errors)
    return None
