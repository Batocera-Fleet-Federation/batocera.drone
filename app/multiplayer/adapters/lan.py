"""Generic LAN multiplayer (PS2, Xbox, DOS/PC, Dreamcast ...).

Many emulated consoles reach their "LAN" through the emulator's own virtual
network adapter, and the games discover each other with broadcasts. A routed
VPN link (Tailscale) is layer 3 and carries no broadcast, so Drone cannot make
those games discover each other by itself. What it *can* do reliably is test
peer connectivity and latency, work out the host's reachable address, check the
game's recorded capability information, and tell the user
whether a relay/tunnel is needed. No relay ships with Drone: the relay is an
isolated interface (``multiplayer.network.RelayProvider``) that is ``none`` by
default.
"""

from typing import Optional

from .. import models as m
from .base import CONTENT_TITLE, MANUAL, R_MANUAL, TIER_EXPERIMENTAL, EmulatorAdapter

LAN_SYSTEMS = frozenset({"ps2", "xbox", "dos", "windows", "dreamcast", "naomi", "atomiswave", "gamecube"})


class GenericLanAdapter(EmulatorAdapter):
    id = "lan"
    name = "Generic LAN"
    tier = TIER_EXPERIMENTAL
    automation = MANUAL
    systems = LAN_SYSTEMS
    mechanisms = (m.EMULATED_LAN,)
    protocols = ("Game-specific TCP/UDP ports; broadcast discovery needs a layer-2 relay",)
    permissions = ("Outbound connectivity tests to paired peers",)
    requirements = (
        "A game and emulator that support network play, configured by the player",
        "Layer-2 reachability (same LAN or a relay) when the game discovers hosts by broadcast",
    )
    limitations = (
        "No relay ships with Drone; broadcast-dependent games work on one LAN only",
        "UDP reachability cannot be proven by a connection test and is reported as unverified",
    )
    content_match = CONTENT_TITLE
    needs_same_lan = True  # unknown game requirements: conservatively refuse routed/broadcast assumptions

    def detect_installed(self) -> bool:
        return True  # network tooling is built in; the emulator itself is the player's choice

    def detect_version(self) -> str:
        return ""

    def readiness(self) -> dict:
        return {"status": R_MANUAL, "message": "Drone tests connectivity and lists the steps; the game's own LAN menu does the rest."}

    def assess_game(self, game: dict, overrides: Optional[dict] = None) -> Optional[dict]:
        if game.get("system") not in self.systems:
            return None
        players = game.get("players")
        reasons = ["This game's LAN requirements have not been recorded.",
                   "Broadcast discovery does not cross Tailscale; use the same LAN or a relay."]
        extra = {"max_players": min(players, m.MAX_PLAYERS_LIMIT) if players else None,
                 "emulator": {"adapter": self.id, "name": "emulator-native", "core": "", "version": "", "core_version": ""}}
        if players == 1:
            return self.assessment(m.EMULATED_LAN, m.UNSUPPORTED, ["Game metadata lists a single player."], **extra)
        return self.assessment(m.EMULATED_LAN, m.UNKNOWN, reasons, **extra)

    def prepare_session(self, session: dict, role: str, context: dict) -> dict:
        steps = ["Configure the emulator's network adapter the way its documentation describes (Drone does not change it).",
                 "Start the game and use its LAN / System Link menu."]
        if role == m.GUEST_ROLE:
            steps.append(f"The host is reachable at {context.get('host_address', 'the host')}.")
        return {"role": role, "manual_steps": steps}
