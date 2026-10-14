"""PPSSPP ad hoc multiplayer (PSP).

PPSSPP's ad hoc mode needs an ad hoc ("PRO") server. Newer builds embed one
(``EnableAdhocServer`` in the ``[Network]`` section of ``ppsspp.ini``); the
host runs it and every guest points ``ProAdhocServer`` at the host. The adapter
only offers the mechanism when the installed build's own configuration file
contains those keys -- their presence is the proof the build supports them. The
keys are global in PPSSPP, so they are changed for the session and restored by
the journaled ``ConfigGuard``.
"""

from pathlib import Path
from typing import Dict, List, Optional

from .. import models as m
from .base import ASSISTED, MANUAL, CONTENT_TITLE, TIER_EXPERIMENTAL, EmulatorAdapter

ADHOC_SERVER_PORT = 27312  # PPSSPP's documented PRO ad hoc server port


def parse_ini_keys(text: str, section: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    current = ""
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip().lower()
        elif current == section.lower() and "=" in stripped and not stripped.startswith(("#", ";")):
            key, _, value = stripped.partition("=")
            values[key.strip()] = value.strip()
    return values


class PPSSPPAdapter(EmulatorAdapter):
    id = "ppsspp"
    name = "PPSSPP ad hoc"
    tier = TIER_EXPERIMENTAL
    automation = ASSISTED
    systems = frozenset({"psp"})
    mechanisms = (m.PPSSPP_ADHOC,)
    binaries = ("PPSSPPSDL", "ppsspp", "PPSSPPQt")
    extra_binary_paths = ("/usr/bin/PPSSPPSDL", "/usr/ppsspp/PPSSPPSDL")
    protocols = (f"PPSSPP PRO ad hoc server (TCP/UDP, server port {ADHOC_SERVER_PORT})",)
    permissions = ("Edit the [Network] section of ppsspp.ini for the session (journaled, restored)",)
    requirements = (
        "A PPSSPP build whose ppsspp.ini has EnableWlan, ProAdhocServer and EnableAdhocServer",
        "PPSSPP has been run once so its configuration file exists",
        "Players choose Ad hoc multiplayer inside the game",
    )
    limitations = (
        "PSP titles differ widely in ad hoc support; none are marked verified until a session succeeds",
        "Drone cannot navigate the game's own multiplayer menu",
    )
    content_match = CONTENT_TITLE

    def ini_path(self) -> Path:
        return Path(self.probe.settings.userdata_root) / "system" / "configs" / "ppsspp" / "PSP" / "SYSTEM" / "ppsspp.ini"

    def network_keys(self) -> Dict[str, str]:
        return parse_ini_keys(self.probe.read_text(self.ini_path()), "Network")

    def _detect_features(self) -> Dict[str, dict]:
        if not self.detect_installed():
            return {m.PPSSPP_ADHOC: {"available": False, "reason": "PPSSPP is not installed"}}
        keys = self.network_keys()
        if not keys:
            return {m.PPSSPP_ADHOC: {"available": False, "builtin_server": False,
                                     "reason": "ppsspp.ini has no [Network] section yet; start PPSSPP once to create it"}}
        missing = [name for name in ("EnableWlan", "ProAdhocServer") if name not in keys]
        if missing:
            return {m.PPSSPP_ADHOC: {"available": False, "builtin_server": False,
                                     "reason": f"This PPSSPP build's configuration lacks {', '.join(missing)}"}}
        builtin = "EnableAdhocServer" in keys
        return {m.PPSSPP_ADHOC: {"available": True, "builtin_server": builtin,
                                 "reason": "" if builtin else "This build has no built-in ad hoc server, so it can join but not host"}}

    def launcher_preserves_network(self) -> bool:
        files = self.probe.glob("/usr/lib/python3*/site-packages/configgen/generators/ppsspp/*Generator.py")
        sources = [self.probe.read_text(path) for path in files]
        # Configgen builds with their own networking options may overwrite ppsspp.ini.
        # Do not assume the user's ini survives a launch on an uninspected build.
        return bool(sources) and all(sources) and not any(
            key in text for text in sources for key in ("EnableWlan", "ProAdhocServer", "EnableAdhocServer"))

    def effective_automation(self) -> str:
        return ASSISTED if self.launcher_preserves_network() else MANUAL

    def assess_game(self, game: dict, overrides: Optional[dict] = None) -> Optional[dict]:
        if game.get("system") != "psp":
            return None
        configured = self.probe.conf_value("psp", game.get("rom_name", ""), "emulator")
        if configured and configured != "ppsspp":
            return self.assessment(m.PPSSPP_ADHOC, m.UNSUPPORTED,
                                   [f"This game is configured for {configured}; select standalone PPSSPP in Batocera first."])
        players = game.get("players")
        extra = {"emulator": {"adapter": self.id, "name": "ppsspp", "core": "", "version": self.detect_version(), "core_version": ""},
                 "max_players": min(players, 4) if players else None}
        feature = self.detect_features()[m.PPSSPP_ADHOC]
        if not self.detect_installed():
            return self.assessment(m.PPSSPP_ADHOC, m.UNSUPPORTED, ["PPSSPP is not installed."], **extra)
        if not feature["available"]:
            return self.assessment(m.PPSSPP_ADHOC, m.UNSUPPORTED, [feature["reason"]], **extra)
        reasons = ["PSP titles vary in ad hoc support; this one has not been tested here."]
        if not feature.get("builtin_server"):
            reasons.append(feature["reason"])
        if players == 1:
            return self.assessment(m.PPSSPP_ADHOC, m.UNSUPPORTED, ["Game metadata lists a single player."], **extra)
        if players is None:
            return self.assessment(m.PPSSPP_ADHOC, m.UNKNOWN, reasons + ["The game's player count is not recorded."], **extra)
        return self.assessment(m.PPSSPP_ADHOC, m.EXPERIMENTAL, reasons, **extra)

    def validate_configuration(self, session: dict, role: str) -> List[str]:
        problems = super().validate_configuration(session, role)
        if problems:
            return problems
        feature = self.detect_features()[m.PPSSPP_ADHOC]
        if not feature["available"]:
            problems.append(feature["reason"])
        elif role == m.HOST_ROLE and not feature.get("builtin_server"):
            problems.append("This PPSSPP build has no built-in ad hoc server, so it cannot host.")
        return problems

    def prepare_session(self, session: dict, role: str, context: dict) -> dict:
        steps = ["When the game has started, open its Ad hoc / Wireless multiplayer menu."]
        if self.effective_automation() == MANUAL:
            address = "127.0.0.1" if role == m.HOST_ROLE else context.get("host_address", "the host")
            return {"role": role, "port": ADHOC_SERVER_PORT, "manual_steps": [
                "This build's launcher has not been confirmed to preserve PPSSPP networking settings. Configure networking in EmulationStation/PPSSPP before launching.",
                f"Enable WLAN; set the PRO ad hoc server to {address}. Enable the built-in server on the host only.", *steps]}

        if role == m.HOST_ROLE:
            steps.insert(0, "Drone enabled PPSSPP's built-in ad hoc server for this session.")
        else:
            steps.insert(0, f"Drone pointed PPSSPP at the host's ad hoc server ({context.get('host_address', 'host')}).")
        return {"role": role, "port": ADHOC_SERVER_PORT, "manual_steps": steps}

    def session_port(self, orchestrator, session: dict) -> int:
        return ADHOC_SERVER_PORT

    def configure_networking(self, session: dict, role: str, context: dict) -> List[dict]:
        path = str(self.ini_path())
        if role == m.HOST_ROLE:
            values = (("EnableWlan", "True"), ("EnableAdhocServer", "True"), ("ProAdhocServer", "127.0.0.1"))
        else:
            values = (("EnableWlan", "True"), ("EnableAdhocServer", "False"), ("ProAdhocServer", context["host_address"]))
        keys = self.network_keys()
        return [{"path": path, "kind": "ini", "section": "Network", "key": key, "value": value}
                for key, value in values if key in keys]
