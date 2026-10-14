"""Dolphin Netplay (GameCube / Wii).

Dolphin's Netplay is operated from Dolphin's own windows (Tools -> Start
NetPlay...). Dolphin offers no command-line or RPC to start or join a Netplay
room, and Batocera launches it game-only, so this adapter does **not** claim
automated launch. It does the part that can be done safely and is what most
often fails: checking installation, Dolphin version equality, the game's ID and
content fingerprint across peers, then handing the user exact steps (including
the host address/port the Drone worked out).
"""

import re
from pathlib import Path
from typing import List, Optional

from .. import models as m
from .base import CONTENT_EXACT, MANUAL, TIER_EXPERIMENTAL, EmulatorAdapter

DOLPHIN_DEFAULT_PORT = 2626
# Dolphin's release numbers are YYMM[letter] (e.g. 2506a) or the older 5.0-NNNN; a dotted number alone would miss them.
DOLPHIN_VERSION = re.compile(r"\b\d{4}[a-z]?\b|\d+\.\d+(?:[.-][0-9A-Za-z]+)*")
GAMECUBE_MAGIC_OFFSET = 0x1C
GAMECUBE_MAGIC = bytes.fromhex("C2339F3D")
WII_MAGIC_OFFSET = 0x18
WII_MAGIC = bytes.fromhex("5D1C9EA3")


def read_game_id(path: Optional[Path]) -> str:
    """Six-character disc ID of a plain ``.iso``/``.gcm`` image, else ``""``.

    Compressed containers (RVZ, WIA, GCZ, WBFS, CISO) carry the header in
    format-specific places and are deliberately not parsed; matching then
    relies on the content fingerprint instead.
    """
    try:
        if not path or Path(path).suffix.lower() not in (".iso", ".gcm"):
            return ""
        with open(path, "rb") as handle:
            header = handle.read(0x40)
    except OSError:
        return ""
    if len(header) < 0x20:
        return ""
    if header[GAMECUBE_MAGIC_OFFSET:GAMECUBE_MAGIC_OFFSET + 4] != GAMECUBE_MAGIC and \
            header[WII_MAGIC_OFFSET:WII_MAGIC_OFFSET + 4] != WII_MAGIC:
        return ""
    raw = header[:6]
    return raw.decode("ascii") if all(0x30 <= byte <= 0x5A and not 0x3A <= byte <= 0x40 for byte in raw) else ""


class DolphinAdapter(EmulatorAdapter):
    id = "dolphin"
    name = "Dolphin Netplay"
    tier = TIER_EXPERIMENTAL
    automation = MANUAL
    systems = frozenset({"gamecube", "wii"})
    mechanisms = (m.DOLPHIN_NETPLAY,)
    binaries = ("dolphin-emu", "dolphin-emu-nogui", "Dolphin")
    extra_binary_paths = ("/usr/bin/dolphin-emu", "/usr/bin/dolphin-emu-nogui")
    protocols = ("Dolphin Netplay (direct connection, TCP/UDP, default host port 2626)",)
    permissions = ("Read the game image header", "Read-only checks of the installed Dolphin")
    requirements = (
        "Host and guests run the same Dolphin version",
        "All players have the same game image (same game ID and content)",
        "Netplay is started from Dolphin's own window (Tools -> Start NetPlay...)",
    )
    limitations = (
        "Dolphin exposes no command-line or API to start Netplay: Drone validates and instructs, the players press the buttons",
        "Batocera launches Dolphin game-only, so the Netplay window needs Dolphin's desktop interface",
        "Only plain .iso/.gcm images have their game ID read; other formats are matched by content fingerprint",
    )
    content_match = CONTENT_EXACT

    def _detect_features(self) -> dict:
        gui = self.detect_installed() and "nogui" not in (self.binary() or "").lower()
        return {m.DOLPHIN_NETPLAY: {"available": bool(gui), "reason": "" if gui else
                                   "A Dolphin GUI build is required for the manual Netplay lobby; a headless binary is insufficient"}}

    def detect_version(self) -> str:
        return self._cached("version", lambda: self.probe.version_of(self.binary(), pattern=DOLPHIN_VERSION))

    def assess_game(self, game: dict, overrides: Optional[dict] = None) -> Optional[dict]:
        if game.get("system") not in self.systems:
            return None
        version = self.detect_version()
        players = game.get("players")
        extra = {"emulator": {"adapter": self.id, "name": "dolphin", "core": "", "version": version, "core_version": ""},
                 "max_players": min(players, 4) if players else None,
                 "game_id": read_game_id(game.get("path"))}
        if not self.detect_installed():
            return self.assessment(m.DOLPHIN_NETPLAY, m.UNSUPPORTED, ["Dolphin is not installed."], **extra)
        if not self.detect_features()[m.DOLPHIN_NETPLAY]["available"]:
            return self.assessment(m.DOLPHIN_NETPLAY, m.UNSUPPORTED,
                                   [self.detect_features()[m.DOLPHIN_NETPLAY]["reason"]], **extra)
        reasons = ["Dolphin Netplay is started from Dolphin's own window; Drone lists the steps."]
        if not version:
            return self.assessment(m.DOLPHIN_NETPLAY, m.REQUIRES_CONFIGURATION,
                                   reasons + ["The installed Dolphin did not report a version, so equality with other players cannot be checked."], **extra)
        if players == 1:
            return self.assessment(m.DOLPHIN_NETPLAY, m.UNSUPPORTED, reasons + ["Game metadata lists a single player."], **extra)
        if players is None:
            return self.assessment(m.DOLPHIN_NETPLAY, m.UNKNOWN, reasons + ["The game's player count is not recorded."], **extra)
        if not extra["game_id"]:
            reasons.append("The game ID could not be read from this image format; peers are compared by content fingerprint.")
        return self.assessment(m.DOLPHIN_NETPLAY, m.LIKELY_COMPATIBLE, reasons, **extra)

    def validate_configuration(self, session: dict, role: str) -> List[str]:
        problems = super().validate_configuration(session, role)
        if problems:
            return problems
        if not self.detect_features()[m.DOLPHIN_NETPLAY]["available"]:
            return [self.detect_features()[m.DOLPHIN_NETPLAY]["reason"]]
        host_version = (session.get("emulator") or {}).get("version", "")
        mine = self.detect_version()
        if not mine:
            problems.append("Unable to start Dolphin Netplay: this machine's Dolphin did not report its version.")
        elif role == m.GUEST_ROLE and host_version and host_version != mine:
            problems.append("Unable to start Dolphin Netplay: the host and client are running incompatible Dolphin versions "
                            f"(host {host_version}, this machine {mine}).")
        return problems

    def prepare_session(self, session: dict, role: str, context: dict) -> dict:
        port = context.get("port") or DOLPHIN_DEFAULT_PORT
        title = (session.get("game") or {}).get("title", "the game")
        if role == m.HOST_ROLE:
            steps = [f"Open Dolphin's window and choose Tools -> Start NetPlay... Select '{title}', connection type Direct, port {port}.",
                     "Start hosting, then wait for the other players to appear in the NetPlay window."]
        else:
            address = context.get("host_address", "the host")
            steps = [f"Open Dolphin's window and choose Tools -> Start NetPlay..., then Connect.",
                     f"Enter host {address} and port {port}. Make sure '{title}' is selected in the game list."]
        return {"role": role, "port": port, "manual_steps": steps}
