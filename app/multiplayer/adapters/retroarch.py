"""RetroArch Netplay (libretro cores) -- the primary native mechanism.

Batocera starts RetroArch through ``emulatorlauncher``, which reads per-game
``batocera.conf`` keys. Netplay is requested with ``netplay.*`` keys scoped to
the one ROM (``<system>["<rom>"].netplay.mode`` ...), so nothing global is
touched and the keys are removed again when the session ends. The adapter only
claims *automated* launch after it finds every one of those keys in the
installed ``configgen`` sources; otherwise it falls back to manual guidance.
"""

from typing import Any, Dict, List, Optional
from pathlib import Path

from .. import models as m
from ..config_guard import scoped_key
from .base import CONTENT_EXACT, FULL, MANUAL, R_MANUAL, TIER_IMPLEMENTED, EmulatorAdapter

RETROARCH_SYSTEMS = frozenset({
    "nes", "fds", "snes", "satellaview", "sufami", "megadrive", "genesis", "mastersystem", "segacd", "sega32x",
    "gamegear", "sg1000", "neogeo", "fbneo", "mame", "pcengine", "pcenginecd", "supergrafx", "gb", "gbc", "gba",
    "atari2600", "atari5200", "atari7800", "lynx", "ngp", "ngpc", "wswan", "wswanc", "virtualboy", "colecovision",
    "intellivision", "vectrex", "msx1", "msx2", "pcfx",
})

# Candidate libretro cores per system. These are only *candidates*: a core is
# offered only if its library is actually installed, and never assumed default.
CANDIDATE_CORES = {
    "nes": ("fceumm", "nestopia", "mesen", "quicknes"),
    "fds": ("fceumm", "nestopia", "mesen"),
    "snes": ("snes9x", "bsnes", "mesen-s"),
    "satellaview": ("snes9x", "bsnes"),
    "sufami": ("snes9x", "bsnes"),
    "megadrive": ("genesisplusgx", "picodrive"),
    "genesis": ("genesisplusgx", "picodrive"),
    "mastersystem": ("genesisplusgx", "picodrive"),
    "gamegear": ("genesisplusgx", "picodrive"),
    "sg1000": ("genesisplusgx",),
    "segacd": ("genesisplusgx", "picodrive"),
    "sega32x": ("picodrive",),
    "neogeo": ("fbneo",),
    "fbneo": ("fbneo",),
    "mame": ("mame", "mame2003_plus", "fbneo"),
    "pcengine": ("mednafen_pce_fast", "mednafen_pce"),
    "pcenginecd": ("mednafen_pce_fast", "mednafen_pce"),
    "supergrafx": ("mednafen_supergrafx",),
    "gb": ("gambatte", "mgba"),
    "gbc": ("gambatte", "mgba"),
    "gba": ("mgba", "gpsp"),
    "atari2600": ("stella",),
    "lynx": ("handy", "mednafen_lynx"),
}

NETPLAY_KEYS = ("netplay.mode", "netplay.server.ip", "netplay.server.port", "netplay.password", "netplay.nickname")
CONFIGGEN_GLOBS = (
    "/usr/lib/python3*/site-packages/configgen/emulatorlauncher.py",
    "/usr/lib/python3*/site-packages/configgen/generators/libretro/*.py",
)
CORES_DIR = "/usr/lib/libretro"
INFO_DIR = "/usr/share/libretro/info"
DEFAULT_PORT = 55435


def parse_info(text: str) -> Dict[str, str]:
    values = {}
    for line in str(text or "").splitlines():
        name, separator, value = line.partition("=")
        if separator:
            values[name.strip()] = value.strip().strip('"')
    return values


class RetroArchAdapter(EmulatorAdapter):
    id = "retroarch"
    name = "RetroArch Netplay"
    tier = TIER_IMPLEMENTED
    automation = FULL
    systems = RETROARCH_SYSTEMS
    mechanisms = (m.RETROARCH_NETPLAY,)
    binaries = ("retroarch",)
    extra_binary_paths = ("/usr/bin/retroarch",)
    protocols = ("RetroArch Netplay over TCP (default port 55435)",)
    permissions = ("Edit per-game keys in batocera.conf (journaled, restored)", "Launch games through EmulationStation")
    requirements = (
        "RetroArch installed with a libretro core that supports deterministic save states",
        "Host and guests run the same core and core version, and the same content",
        "Batocera's emulatorlauncher recognises the netplay.* settings (detected, not assumed)",
    )
    limitations = (
        "Player slots are assigned by RetroArch in connection order; Drone sends launch offers in slot order; actual slots depend on confirmation/connection order",
        "Cores without deterministic save states can desync",
    )
    content_match = CONTENT_EXACT
    awaits_host_port = True

    def cores_dir(self) -> str:
        return CORES_DIR

    def _configgen_files(self) -> List[str]:
        files: List[str] = []
        for pattern in CONFIGGEN_GLOBS:
            files.extend(self.probe.glob(pattern))
        return files

    def netplay_keys_verified(self) -> Optional[bool]:
        """True/False when the launcher sources were readable, ``None`` when they could not be found."""
        files = self._configgen_files()
        if not files:
            return None
        return all(self.probe.file_contains_all(files, NETPLAY_KEYS).values())

    def _detect_features(self) -> Dict[str, dict]:
        if not self.detect_installed():
            return {m.RETROARCH_NETPLAY: {"available": False, "reason": "RetroArch is not installed"}}
        verified = self.netplay_keys_verified()
        reason = ""
        if verified is None:
            reason = "Batocera's launcher sources were not found, so automated Netplay launch could not be confirmed"
        elif verified is False:
            reason = "This Batocera build's launcher does not expose all netplay.* settings"
        return {m.RETROARCH_NETPLAY: {"available": True, "automated_launch": verified is True, "reason": reason}}

    def effective_automation(self) -> str:
        features = self.detect_features().get(m.RETROARCH_NETPLAY, {})
        return FULL if features.get("automated_launch") else MANUAL

    def readiness(self) -> dict:
        base = super().readiness()
        if base["status"] == R_MANUAL:
            reason = self.detect_features()[m.RETROARCH_NETPLAY]["reason"]
            base["message"] = f"RetroArch is installed. {reason}. Drone validates the session; start Netplay from EmulationStation."
        return base

    # ------------------------------------------------------------------ cores
    def core_info(self, core: str) -> Dict[str, str]:
        return parse_info(self.probe.read_text(f"{INFO_DIR}/{core}_libretro.info"))

    def core_installed(self, core: str) -> bool:
        return self.probe.exists(f"{self.cores_dir()}/{core}_libretro.so")

    def installed_candidates(self, system: str) -> List[str]:
        return [core for core in CANDIDATE_CORES.get(system, ()) if self.core_installed(core)]

    def resolve_emulator(self, system: str, rom_name: str, overrides: dict) -> Dict[str, str]:
        override_core = str((overrides.get("core_overrides") or {}).get(system) or "")
        emulator = self.probe.conf_value(system, rom_name, "emulator")
        core = override_core or self.probe.conf_value(system, rom_name, "core")
        return {"emulator": emulator, "core": core, "overridden": bool(override_core)}

    def assess_game(self, game: dict, overrides: Optional[dict] = None) -> Optional[dict]:
        system = game.get("system", "")
        if system not in RETROARCH_SYSTEMS:
            return None
        overrides = overrides or {}
        chosen = self.resolve_emulator(system, game.get("rom_name", ""), overrides)
        players = game.get("players")
        max_players = min(players, m.MAX_PLAYERS_LIMIT) if players else None
        emulator = {"adapter": self.id, "name": "libretro", "core": chosen["core"], "version": self.detect_version(),
                    "core_version": ""}
        extra = {"emulator": emulator, "max_players": max_players}
        if not self.detect_installed():
            return self.assessment(m.RETROARCH_NETPLAY, m.UNSUPPORTED, ["RetroArch is not installed."], **extra)
        if chosen["emulator"] and chosen["emulator"] != "libretro":
            return self.assessment(m.RETROARCH_NETPLAY, m.UNSUPPORTED,
                                   [f"This game is configured to run in {chosen['emulator']}, not a RetroArch core."], **extra)
        reasons: List[str] = []
        if not chosen["emulator"]:
            reasons.append("No emulator is set for this game in batocera.conf; assuming Batocera's libretro default.")
        if not chosen["core"]:
            candidates = self.installed_candidates(system)
            extra["candidate_cores"] = candidates
            if not candidates:
                return self.assessment(m.RETROARCH_NETPLAY, m.UNSUPPORTED,
                                       reasons + ["No known RetroArch core for this system is installed."], **extra)
            return self.assessment(m.RETROARCH_NETPLAY, m.REQUIRES_CONFIGURATION,
                                   reasons + [f"No core is selected for this game. Installed candidates: {', '.join(candidates)}. "
                                              "Pick one in the session or in Batocera."], **extra)
        if not self.core_installed(chosen["core"]):
            return self.assessment(m.RETROARCH_NETPLAY, m.INCOMPATIBLE,
                                   reasons + [f"The selected core '{chosen['core']}' is not installed."], **extra)
        info = self.core_info(chosen["core"])
        emulator["core_version"] = m.clean_version(info.get("display_version"))
        if chosen["overridden"]:
            reasons.append(f"Core '{chosen['core']}' comes from your Multiplayer emulator override.")
        if info.get("savestate", "true").lower() == "false":
            classification = m.UNSUPPORTED
            reasons.append(f"Core '{chosen['core']}' has no save states, which Netplay requires.")
        elif info.get("savestate_features", "").lower() == "deterministic":
            classification = m.LIKELY_COMPATIBLE
            reasons.append(f"Core '{chosen['core']}' reports deterministic save states.")
        elif info:
            classification = m.EXPERIMENTAL
            reasons.append(f"Core '{chosen['core']}' does not report deterministic save states; Netplay may desync.")
        else:
            classification = m.EXPERIMENTAL
            reasons.append(f"No libretro info file for '{chosen['core']}'; Netplay support is unverified.")
        if classification in (m.LIKELY_COMPATIBLE, m.EXPERIMENTAL):
            if players == 1:
                classification = m.UNSUPPORTED
                reasons.append("Game metadata lists a single player.")
            elif players is None and classification == m.LIKELY_COMPATIBLE:
                classification = m.UNKNOWN
                reasons.append("The game's player count is not recorded, so multiplayer support is unconfirmed.")
        return self.assessment(m.RETROARCH_NETPLAY, classification, reasons, **extra)

    # --------------------------------------------------------------- sessions
    def validate_configuration(self, session: dict, role: str) -> List[str]:
        problems = super().validate_configuration(session, role)
        if problems:
            return problems
        host = session.get("emulator") or {}
        game = session.get("game") or {}
        if role == m.GUEST_ROLE:
            local_core = host.get("core") or ""
            if not local_core:
                return ["The host has not selected a RetroArch core yet."]
            if not self.core_installed(local_core):
                problems.append(f"The host uses the '{local_core}' core, which is not installed here.")
            else:
                mine = m.clean_version(self.core_info(local_core).get("display_version"))
                if mine and host.get("core_version") and mine != host["core_version"]:
                    problems.append(f"Core '{local_core}' versions differ (host {host['core_version']}, this machine {mine}).")
            theirs, ours = host.get("version", ""), self.detect_version()
            if theirs and ours and theirs.split(".")[:2] != ours.split(".")[:2]:
                problems.append(f"RetroArch versions are incompatible (host {theirs}, this machine {ours}).")
        if not game.get("rom_name"):
            problems.append("The session does not name a game file.")
        return problems

    def prepare_session(self, session: dict, role: str, context: dict) -> dict:
        port = context.get("port") or DEFAULT_PORT
        steps = []
        if self.effective_automation() == MANUAL:
            if role == m.HOST_ROLE:
                steps.append(f"In EmulationStation choose this game, open its Netplay options and start it as host on port {port}.")
            else:
                steps.append(f"In EmulationStation open Netplay, join the host at {context.get('host_address', 'the host')} port {port}.")
        else:
            steps.append("Drone starts the game for you; you appear in the next free player slot once connected.")
        return {"role": role, "port": port, "manual_steps": steps}

    def session_port(self, orchestrator: Any, session: dict) -> int:
        networking = orchestrator.cfg.get()["networking"]
        in_use = {int((other.get("local") or {}).get("port") or 0) for other in orchestrator.store.active() if other["id"] != session["id"]}
        return orchestrator.network.allocate_port(networking["port_range_start"], networking["port_range_end"], in_use)

    def configure_networking(self, session: dict, role: str, context: dict) -> List[dict]:
        if self.effective_automation() == MANUAL:
            raise m.CapabilityError("This Batocera build's launcher cannot be configured for Netplay by Drone.", "netplay_keys_unverified")
        system, rom = context.get("system") or session["game"]["system"], context.get("rom_name") or session["game"]["rom_name"]
        conf = context["conf_path"]
        nickname = m.clean_nickname(context.get("nickname"), "Drone")
        values = [("netplay.nickname", nickname), ("netplay.password", context["secret"])]
        if role == m.HOST_ROLE:
            values += [("netplay.mode", "host"), ("netplay.server.port", str(context["port"]))]
        else:
            values += [("netplay.mode", "client"), ("netplay.server.ip", context["host_address"]),
                       ("netplay.server.port", str(context["port"]))]
        edits = [{"path": conf, "kind": "kv", "key": scoped_key(system, rom, key), "value": value} for key, value in values]
        core = (session.get("emulator") or {}).get("core")
        if core:
            # Pin the host's core for this ROM only so both sides run identical emulation.
            edits.append({"path": conf, "kind": "kv", "key": scoped_key(system, rom, "emulator"), "value": "libretro"})
            edits.append({"path": conf, "kind": "kv", "key": scoped_key(system, rom, "core"), "value": core})
        # Batocera configgen also writes the credential into its generated RetroArch profile.
        # Journal before launch, including a profile that does not exist yet, so it cannot retain
        # our join password after cleanup/restart. Watch only these fields; never overwrite the profile.
        generated = Path(self.probe.settings.userdata_root) / "system/configs/retroarch/retroarchcustom.cfg"
        for key in ("netplay_password", "netplay_spectate_password"):
            edits.append({"path": str(generated), "kind": "kv", "key": key,
                          "value": f'"{context["secret"]}"', "watch": True})
        return edits

    def check_session_health(self, session: dict, context: dict) -> dict:
        if context.get("role") == m.HOST_ROLE and context.get("port"):
            listening = self.probe.tcp_open("127.0.0.1", context["port"], 1.0)
            return {"ok": listening, "detail": "" if listening else f"Netplay is not listening on port {context['port']} yet."}
        return {"ok": True, "detail": ""}
