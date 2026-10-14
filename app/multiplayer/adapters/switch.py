"""Nintendo Switch local-wireless / LAN play (Ryubing/Ryujinx, Eden).

Switch "local wireless" maps to LDN, which emulators carry either over a
LAN-level protocol or through room servers. Whether a given build and game
support it varies, and LAN discovery depends on broadcast traffic that a
Tailscale (layer 3) link does not carry. This adapter therefore detects what is
installed, reads the emulator's own settings to see whether the build exposes a
multiplayer mode, and reports a precise requirement set -- it does not edit the
emulator, join rooms, or touch console authentication. Anything beyond
detection stays a manual step.
"""

import json
from pathlib import Path
from typing import Dict, Optional

from .. import models as m
from .base import CONTENT_EXACT, MANUAL, TIER_EXPERIMENTAL, EmulatorAdapter

RYUJINX_CONFIG = ("system", "configs", "Ryujinx", "Config.json")


class SwitchLdnAdapter(EmulatorAdapter):
    id = "switch"
    name = "Switch LDN / LAN play"
    tier = TIER_EXPERIMENTAL
    automation = MANUAL
    systems = frozenset({"switch"})
    mechanisms = (m.SWITCH_LDN,)
    binaries = ("Ryujinx", "ryujinx", "Ryubing", "ryubing", "eden", "Eden")
    extra_binary_paths = ("/usr/ryujinx/Ryujinx", "/usr/bin/ryujinx", "/usr/bin/eden")
    protocols = ("Emulated LDN / LAN play (UDP broadcast discovery on the local network)",)
    permissions = ("Read-only inspection of the emulator's settings file",)
    requirements = (
        "A Switch emulator build with a multiplayer mode (Ryubing/Ryujinx: multiplayer_mode in Config.json)",
        "All players on the same local network: LAN discovery uses broadcast, which Tailscale does not carry",
        "Identical game version and the same emulator family on every player",
    )
    limitations = (
        "Drone does not change emulator settings, join rooms or interact with console network authentication",
        "Local multiplayer does not imply LDN support: it is decided per game",
        "Eden's multiplayer uses its own room system and is detected only",
    )
    content_match = CONTENT_EXACT
    needs_same_lan = True

    def ryujinx_config(self) -> Dict[str, object]:
        path = Path(self.probe.settings.userdata_root).joinpath(*RYUJINX_CONFIG)
        try:
            data = json.loads(self.probe.read_text(path) or "{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def emulator_family(self) -> str:
        binary = (self.binary() or "").lower()
        if "eden" in binary:
            return "eden"
        return "ryujinx" if binary else ""

    def _detect_features(self) -> Dict[str, dict]:
        if not self.detect_installed():
            return {m.SWITCH_LDN: {"available": False, "reason": "No supported Switch emulator was found"}}
        if self.emulator_family() == "eden":
            return {m.SWITCH_LDN: {"available": False,
                                   "reason": "Eden's multiplayer uses room servers configured inside the emulator; Drone does not automate it"}}
        config = self.ryujinx_config()
        if "multiplayer_mode" not in config:
            return {m.SWITCH_LDN: {"available": False,
                                   "reason": "This emulator's settings expose no multiplayer mode (start it once, or update it)"}}
        return {m.SWITCH_LDN: {"available": True, "mode": str(config.get("multiplayer_mode")), "reason": ""}}

    def assess_game(self, game: dict, overrides: Optional[dict] = None) -> Optional[dict]:
        if game.get("system") != "switch":
            return None
        players = game.get("players")
        extra = {"emulator": {"adapter": self.id, "name": self.emulator_family(), "core": "", "version": self.detect_version(),
                              "core_version": ""},
                 "max_players": min(players, m.MAX_PLAYERS_LIMIT) if players else None}
        feature = self.detect_features()[m.SWITCH_LDN]
        if not self.detect_installed():
            return self.assessment(m.SWITCH_LDN, m.UNSUPPORTED, ["No supported Switch emulator is installed."], **extra)
        if not feature["available"]:
            return self.assessment(m.SWITCH_LDN, m.UNSUPPORTED, [feature["reason"]], **extra)
        reasons = ["LDN support is decided per game and is untested here.", "Every player must be on the same local network."]
        if players == 1:
            return self.assessment(m.SWITCH_LDN, m.UNSUPPORTED, ["Game metadata lists a single player."], **extra)
        if players is None:
            return self.assessment(m.SWITCH_LDN, m.UNKNOWN, reasons + ["The game's player count is not recorded."], **extra)
        return self.assessment(m.SWITCH_LDN, m.EXPERIMENTAL, reasons, **extra)

    def prepare_session(self, session: dict, role: str, context: dict) -> dict:
        steps = ["In the emulator's settings set the multiplayer mode to LAN/LDN (Drone does not change it).",
                 "Start the game, then use its local wireless / LAN menu: the host creates the room, the others join."]
        if role == m.GUEST_ROLE:
            steps.append("All players must be on the same local network as the host.")
        return {"role": role, "manual_steps": steps}
