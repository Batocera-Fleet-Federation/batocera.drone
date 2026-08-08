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
(psp -- requires cross-origin-isolation headers we don't otherwise need), a
romset-version-sensitive arcade core (mame/fba -- upstream romset drift makes
"why won't my arcade game load" a real support burden), or a BIOS+multi-disc-heavy
setup (saturn, segacd) -- all addressable later without changing this module's shape.
"""

from typing import Dict, Optional

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
}


def browser_play_core_for_system(system: str) -> Optional[str]:
    """The EmulatorJS core id for ``system``, or ``None`` if unsupported."""
    return SYSTEM_CORE_MAP.get(str(system or "").strip().lower())
