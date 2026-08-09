"""Batocera system -> EmulatorJS core mapping for in-browser ("Play in Browser") play.

EmulatorJS (https://github.com/EmulatorJS/EmulatorJS) is a self-contained set of
libretro cores compiled to WASM, vendored under ``app/web/static/emulatorjs/data/``
(engine files + a curated subset of core ``-wasm.data`` files -- not the full
~300MB upstream release, see the ``drone-batocera-emulationstation`` skill /
CLAUDE.md for the curation rationale). ``EJS_core`` accepts either a short alias
("nes", "snes", ...) or the literal libretro core name; this map uses the literal
core name directly (matches the vendored file names one-to-one, no alias-table
guessing needed).

Deliberately excludes systems that would need a threaded/SharedArrayBuffer core
(psp -- requires cross-origin-isolation headers we don't otherwise need) or a
BIOS+multi-disc-heavy setup (saturn, segacd) -- both addressable later without
changing this module's shape.

mame/fba/fbneo are included despite a real compatibility caveat: MAME and
FBNeo cores are strict about matching one specific upstream romset revision
(exact per-file CRCs, split-vs-merged conventions, BIOS requirements), and the
vendored cores here track an older/independent romset generation than
whatever Batocera's own bundled MAME/FBNeo build expects. A ROM that boots
fine on the actual device is not guaranteed to boot in the browser core --
unlike every other system in this map, where "shows up" reliably means "will
work". ROMSET_SENSITIVE_SYSTEMS flags that caveat for the UI to surface
instead of silently promising the same reliability as everything else.
"""

from typing import Dict, FrozenSet, Optional

SYSTEM_CORE_MAP: Dict[str, str] = {
    "nes": "fceumm",
    "snes": "snes9x",
    "gb": "gambatte",
    "gbc": "gambatte",
    "gba": "mgba",
    "megadrive": "genesis_plus_gx",
    "mastersystem": "genesis_plus_gx",
    "gamegear": "genesis_plus_gx",
    "n64": "mupen64plus_next",
    "psx": "mednafen_psx_hw",
    "pcengine": "mednafen_pce",
    "pcfx": "mednafen_pcfx",
    "ngp": "mednafen_ngp",
    "ngpc": "mednafen_ngp",
    "wswan": "mednafen_wswan",
    "wswanc": "mednafen_wswan",
    "virtualboy": "beetle_vb",
    "colecovision": "gearcoleco",
    "atari2600": "stella2014",
    "atari7800": "prosystem",
    "atari5200": "a5200",
    "3do": "opera",
    "mame": "mame2003_plus",
    "fba": "fbneo",
    "fbneo": "fbneo",
}

# Systems in SYSTEM_CORE_MAP where core support existing doesn't mean a given
# ROM will actually boot -- see the module docstring. The frontend uses this
# to show a compatibility caveat instead of the plain "Play in Browser" button
# it shows for every other system.
ROMSET_SENSITIVE_SYSTEMS: FrozenSet[str] = frozenset({"mame", "fba", "fbneo"})


def browser_play_core_for_system(system: str) -> Optional[str]:
    """The EmulatorJS core id for ``system``, or ``None`` if unsupported."""
    return SYSTEM_CORE_MAP.get(str(system or "").strip().lower())


def browser_play_is_romset_sensitive(system: str) -> bool:
    """Whether ``system``'s browser core needs an exact romset match to boot."""
    return str(system or "").strip().lower() in ROMSET_SENSITIVE_SYSTEMS
