"""Client for EmulationStation's loopback HTTP API (Batocera ES, port 1234).

``POST /launch`` (body = the ROM path ES knows) makes ES itself start the game
through ``ViewController::launch`` -> the system's ``es_systems.cfg`` command ->
``emulatorlauncher`` with the controller arguments, per-game settings, shaders,
hooks and environment that only ES can supply. That is why Stream Deck launches
go through ES instead of running ``emulatorlauncher.py``/emulator binaries
directly. ``GET /runningGame`` is consulted only as a secondary "frontend has
settled" signal after the emulator process is gone.

The endpoint is loopback-only by default (``DRONE_ES_API_URL`` may point it at
another local port for development); nothing here is reachable remotely.
"""

import http.client
import json
import os
from typing import Optional, Tuple
from urllib.parse import urlparse


DEFAULT_ES_API_URL = "http://127.0.0.1:1234"


class EmulationStationUnavailable(OSError):
    """EmulationStation's local API did not answer (ES stopped or API disabled)."""


class EmulationStationApi:
    def __init__(self, base_url: Optional[str] = None, timeout: float = 5.0) -> None:
        parsed = urlparse(base_url or os.environ.get("DRONE_ES_API_URL") or DEFAULT_ES_API_URL)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("the EmulationStation API must be a local http:// URL")
        self.host = parsed.hostname
        self.port = parsed.port or 1234
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Optional[bytes] = None) -> Tuple[int, bytes]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            headers = {"User-Agent": "Batocera-Drone-StreamDeck"}
            if body is not None:
                headers["Content-Type"] = "text/plain; charset=utf-8"
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.read(64 * 1024)
        except (OSError, http.client.HTTPException) as error:
            raise EmulationStationUnavailable(f"EmulationStation API unavailable: {error}") from error
        finally:
            connection.close()

    def launch(self, rom_path: str) -> Tuple[int, str]:
        status, body = self._request("POST", "/launch", rom_path.encode("utf-8"))
        return status, body.decode("utf-8", "replace")[:300]

    def running_game(self) -> Optional[bool]:
        """True/False when ES answers; ``None`` when the API is unreachable.

        Only an HTTP 200 whose JSON object names a game path counts as "running",
        so an unexpected response shape can never block a launch indefinitely.
        """
        try:
            status, body = self._request("GET", "/runningGame")
        except EmulationStationUnavailable:
            return None
        if status != 200:
            return False
        try:
            payload = json.loads(body.decode("utf-8", "replace") or "{}")
        except ValueError:
            return False
        return isinstance(payload, dict) and bool(payload.get("path"))

    def reachable(self) -> bool:
        return self.running_game() is not None
