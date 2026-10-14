"""The emulator adapter contract.

An adapter turns "play this game together" into the concrete, *verified*
steps one emulator supports. The conceptual operations from the design map to:

=========================  ====================================================
DetectInstalled            ``detect_installed``
DetectVersion              ``detect_version``
DetectSupportedFeatures    ``detect_features``
DetectGameCompatibility    ``assess_game``
ValidateConfiguration      ``validate_configuration``
PrepareSession             ``prepare_session``
ConfigureNetworking        ``configure_networking``
ConfigureControllers       ``configure_controllers``
LaunchHost / LaunchClient  ``launch`` (role-aware)
CheckSessionHealth         ``check_session_health``
StopSession                ``stop_session``
RestoreConfiguration       done centrally by ``ConfigGuard`` (journaled)
Cleanup                    ``cleanup``
=========================  ====================================================

Rules: detect, don't assume; never advertise a capability the installed build
cannot provide; unsupported operations raise ``CapabilityError`` with a message
a person can act on. Adapters are side-effect free except ``launch``.
"""

import re
import time
from typing import Any, Callable, Dict, List, Optional

from .. import models as m
from ..environment import SystemProbe

# automation levels
FULL = "full"          # Drone configures and launches; only in-game steps remain
ASSISTED = "assisted"  # Drone validates/prepares; the user performs listed steps
MANUAL = "manual"      # Drone only validates and instructs

# adapter implementation tiers (surfaced in the UI and docs)
TIER_IMPLEMENTED = "implemented"
TIER_EXPERIMENTAL = "experimental"
TIER_DETECT_ONLY = "detect_only"

# readiness
R_READY = "ready"
R_MANUAL = "manual_steps_required"
R_CONFIG = "configuration_required"
R_NOT_INSTALLED = "not_installed"
R_UNSUPPORTED = "unsupported_build"

# how closely peers' content must match
CONTENT_EXACT = "exact"
CONTENT_TITLE = "title"
CONTENT_HOST_ONLY = "host_only"


def parse_players(value: Any) -> Optional[int]:
    """Largest player count in a gamelist ``players`` value such as ``"1-4"`` or ``"2"``."""
    numbers = [int(part) for part in re.findall(r"\d+", str(value or ""))]
    numbers = [number for number in numbers if 0 < number <= 16]
    return max(numbers) if numbers else None


class EmulatorAdapter:
    id = ""
    name = ""
    tier = TIER_EXPERIMENTAL
    automation = MANUAL
    systems = frozenset()
    mechanisms = ()
    binaries = ()
    extra_binary_paths = ()
    protocols = ()
    permissions = ()
    supported_versions = "any version exposing the features listed under Capabilities"
    requirements = ()
    limitations = ()
    content_match = CONTENT_EXACT
    needs_same_lan = False
    guest_launches = True  # False: guests join by following steps (e.g. Moonlight), Drone launches nothing for them
    awaits_host_port = False  # True: the host's server listens as soon as its game is up, so guests wait for the port first

    DETECTION_TTL = 30.0

    def __init__(self, probe: SystemProbe) -> None:
        self.probe = probe
        self._memo: Dict[str, tuple] = {}

    def _cached(self, key: str, compute: Callable[[], Any]) -> Any:
        """Detection shells out and reads files; a library page asks hundreds of times, so memoise briefly."""
        hit = self._memo.get(key)
        now = time.monotonic()
        if hit and now - hit[0] < self.DETECTION_TTL:
            return hit[1]
        value = compute()
        self._memo[key] = (now, value)
        return value

    def refresh(self) -> None:
        self._memo.clear()

    # ------------------------------------------------------------------ detect
    def binary(self) -> Optional[str]:
        return self._cached("binary", lambda: self.probe.find_binary(self.binaries, self.extra_binary_paths))

    def detect_installed(self) -> bool:
        return self.binary() is not None

    def detect_version(self) -> str:
        return self._cached("version", lambda: self.probe.version_of(self.binary()))

    def detect_features(self) -> Dict[str, dict]:
        """``{mechanism: {"available": bool, "reason": str}}`` -- only what the build really offers."""
        return self._cached("features", self._detect_features)

    def _detect_features(self) -> Dict[str, dict]:
        installed = self.detect_installed()
        return {mechanism: {"available": installed, "reason": "" if installed else f"{self.name} is not installed"}
                for mechanism in self.mechanisms}

    def effective_automation(self) -> str:
        """The automation level this machine can really deliver (may be lower than ``automation``)."""
        return self.automation

    def readiness(self) -> dict:
        if not self.detect_installed():
            return {"status": R_NOT_INSTALLED, "message": f"{self.name} was not found on this machine."}
        features = self.detect_features()
        if features and not any(item["available"] for item in features.values()):
            reason = next((item["reason"] for item in features.values() if item["reason"]), "")
            return {"status": R_UNSUPPORTED, "message": reason or f"This {self.name} build exposes no multiplayer features Drone can use."}
        if self.effective_automation() == MANUAL:
            return {"status": R_MANUAL, "message": f"{self.name} is installed; Drone validates the session and lists the steps you perform."}
        return {"status": R_READY, "message": f"{self.name} is installed and its multiplayer features were detected."}

    def capabilities(self) -> dict:
        installed = self.detect_installed()
        features = self.detect_features() if installed else {}
        return {
            "id": self.id,
            "name": self.name,
            "tier": self.tier,
            "automation": self.effective_automation(),
            "installed": installed,
            "version": self.detect_version() if installed else "",
            "systems": sorted(self.systems),
            "mechanisms": list(self.mechanisms),
            "features": features,
            "protocols": list(self.protocols),
            "supported_versions": self.supported_versions,
            "requirements": list(self.requirements),
            "permissions": list(self.permissions),
            "limitations": list(self.limitations),
            "readiness": self.readiness(),
        }

    # -------------------------------------------------------------- per game
    def supports_system(self, system: str) -> bool:
        return "*" in self.systems or system in self.systems

    def assess_game(self, game: dict, overrides: Optional[dict] = None) -> Optional[dict]:
        """Technical multiplayer assessment of one game, or ``None`` when out of scope."""
        return None

    def assessment(self, mechanism: str, classification: str, reasons: List[str], **extra: Any) -> dict:
        readiness = self.readiness()
        base = {
            "mechanism": mechanism,
            "adapter": self.id,
            "adapter_name": self.name,
            "tier": self.tier,
            "automation": self.effective_automation(),
            "classification": classification,
            "reasons": [m.clean_text(reason, 300) for reason in reasons],
            "readiness": readiness["status"],
            "readiness_message": readiness["message"],
            "ready": readiness["status"] in (R_READY, R_MANUAL) and classification in m.PLAYABLE_CLASSES
                     and classification != m.REQUIRES_CONFIGURATION,
            "content_match": self.content_match,
            "needs_same_lan": self.needs_same_lan,
            "emulator": {"adapter": self.id, "name": self.id, "core": "", "version": "", "core_version": ""},
            "max_players": None,
            "requirements": [],
        }
        base.update(extra)
        return base

    def validation_fingerprint(self, game: dict, emulator: dict) -> str:
        """Evidence is specific to the content/revision and inspected local settings.

        Exclude Drone's temporary per-ROM pins and credentials so cleanup does
        not invalidate a successful report. Uninspected BIOS/runtime settings
        still need the emulator's own compatibility checks.
        """
        def settings_digest():
            text = self.probe.read_text(self.probe.settings.batocera_conf_file, 4 * 1024 * 1024)
            lines = [line for line in text.splitlines() if ".netplay." not in line and
                     not re.match(r'^\s*[^=]+\["[^"\n]+"\]\.(?:emulator|core)\s*=', line)]
            return m.config_fingerprint("\n".join(lines))
        return m.config_fingerprint("v2", game.get("fingerprint"), game.get("rom_name"), game.get("size"),
                                    self._cached("settings-digest", settings_digest),
                                    *[emulator.get(key) for key in ("name", "core", "version", "core_version")])

    # --------------------------------------------------------------- sessions
    def validate_configuration(self, session: dict, role: str) -> List[str]:
        """Blocking problems for this machine to take ``role`` in ``session`` (empty list = fine)."""
        problems = []
        if not self.detect_installed():
            problems.append(f"{self.name} is not installed on this machine.")
        return problems

    def prepare_session(self, session: dict, role: str, context: dict) -> dict:
        """Pure plan for one participant: ports, manual steps, config edits. No side effects."""
        return {"role": role, "manual_steps": [], "edits": []}

    def session_port(self, orchestrator: Any, session: dict) -> int:
        """Session-scoped TCP port this mechanism listens on (0 = not applicable)."""
        return 0

    def configure_networking(self, session: dict, role: str, context: dict) -> List[dict]:
        return []

    def configure_controllers(self, session: dict, role: str, context: dict) -> List[dict]:
        # Controller mapping stays Batocera's: emulator player slots follow join order,
        # and the Drone never rewrites permanent pad mappings.
        return []

    def launch(self, session: dict, role: str, context: dict, launcher: Any) -> dict:
        if self.effective_automation() == MANUAL:
            raise m.CapabilityError(f"{self.name} cannot be launched into a multiplayer session by Drone; follow the listed steps.",
                                    "launch_not_automated")
        return launcher.launch_game(context["game_path"])

    def check_session_health(self, session: dict, context: dict) -> dict:
        return {"ok": True, "detail": ""}

    def stop_session(self, session: dict, context: dict) -> None:
        return None

    def cleanup(self, session: dict, context: dict) -> None:
        return None
