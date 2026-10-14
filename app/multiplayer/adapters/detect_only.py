"""Capability-aware, detection-only adapters for emulators Drone does not automate.

Networking support varies between these emulators and builds. Drone detects the
installation and says plainly that native networking is not automated; remote
streaming (the ``streaming`` adapter) is offered instead where it applies.
Nothing here is presented as working multiplayer.
"""

from typing import Optional

from .. import models as m
from .base import MANUAL, TIER_DETECT_ONLY, EmulatorAdapter


class DetectOnlyAdapter(EmulatorAdapter):
    tier = TIER_DETECT_ONLY
    automation = MANUAL
    mechanisms = ()

    def __init__(self, probe, *, adapter_id: str, name: str, systems, binaries, note: str, extra_paths=()) -> None:
        super().__init__(probe)
        self.id = adapter_id
        self.name = name
        self.systems = frozenset(systems)
        self.binaries = tuple(binaries)
        self.extra_binary_paths = tuple(extra_paths)
        self.limitations = (note, "Remote streaming can host this emulator's local multiplayer instead")

    def readiness(self) -> dict:
        if not self.detect_installed():
            return {"status": "not_installed", "message": f"{self.name} was not found on this machine."}
        return {"status": "unsupported_build", "message": f"{self.name} is installed; Drone does not automate its native networking."}

    def assess_game(self, game: dict, overrides: Optional[dict] = None) -> Optional[dict]:
        if not self.supports_system(game.get("system", "")):
            return None
        reason = (f"{self.name} has its own networking features that Drone does not automate."
                  if self.detect_installed() else f"{self.name} is not installed.")
        return self.assessment(m.EMULATOR_NATIVE_NETWORK, m.UNSUPPORTED, [reason],
                               emulator={"adapter": self.id, "name": self.id, "core": "", "version": self.detect_version(),
                                         "core_version": ""})


# (id, display name, Batocera systems, candidate binaries, note). Binary names are
# probed, never assumed: a missing one simply reports "not installed".
EXPERIMENTAL_EMULATORS = (
    ("rpcs3", "RPCS3", ("ps3",), ("rpcs3",), "RPCS3 network features (e.g. PSN emulation) are configured inside RPCS3"),
    ("pcsx2", "PCSX2", ("ps2",), ("pcsx2-qt", "pcsx2"), "PCSX2 DEV9 network and System Link are configured inside PCSX2"),
    ("xemu", "xemu", ("xbox",), ("xemu",), "xemu System Link needs a bridged or tunnelled network set up outside Drone"),
    ("cemu", "Cemu", ("wiiu",), ("cemu", "Cemu"), "Cemu online features are configured inside Cemu"),
    ("flycast", "Flycast", ("dreamcast", "naomi", "atomiswave"), ("flycast",), "Flycast network play is configured inside Flycast"),
    ("melonds", "melonDS", ("nds",), ("melonDS", "melonds"), "melonDS local-network play is configured inside melonDS"),
    ("azahar", "Azahar", ("3ds",), ("azahar", "azahar-qt"), "Azahar multiplayer rooms are configured inside Azahar"),
    ("vita3k", "Vita3K", ("psvita",), ("Vita3K", "vita3k"), "Vita3K has no networked multiplayer Drone can drive"),
    ("xenia", "Xenia", ("xbox360",), ("xenia", "xenia_canary"), "Xenia networking is experimental upstream"),
    ("shadps4", "shadPS4", ("ps4",), ("shadps4", "shadPS4"), "shadPS4 networking is experimental upstream"),
)


def build_detect_only_adapters(probe):
    return [DetectOnlyAdapter(probe, adapter_id=i, name=n, systems=s, binaries=b, note=note)
            for i, n, s, b, note in EXPERIMENTAL_EMULATORS]
