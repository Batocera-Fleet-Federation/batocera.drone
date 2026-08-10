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

fba/fbneo are included despite a real compatibility caveat: these cores are
strict about matching one specific upstream romset revision (exact per-file
CRCs, split-vs-merged conventions, BIOS requirements), and the vendored core
here tracks an older/independent romset generation than whatever Batocera's
own bundled build expects. A ROM that boots fine on the actual device is not
guaranteed to boot in the browser core -- unlike every other system in this
map, where "shows up" reliably means "will work". ROMSET_SENSITIVE_SYSTEMS
flags that caveat for the UI to surface instead of silently promising the
same reliability as everything else.

**mame is deliberately absent from this map entirely** (not just flagged
sensitive) -- confirmed live against a real ~4,400-ROM full romset (folder
named "mame-265", i.e. built for MAME 0.265/current) that the vendored
``mame2003_plus`` core (~0.78-era romset conventions, the standard choice
across every EmulatorJS deployment because a browser-shippable WASM build of
current MAME doesn't exist -- the real thing is 400+MB as a native .so) is
decades of driver/CRC/file-split revisions away from what a modern full
romset actually contains. Unlike fba/fbneo (a narrower, closer version gap
where *some* ROMs plausibly still work), the gap here is large enough that
showing a "Play in Browser" button at all was judged more misleading than
useful for a MAME library shaped like this one -- most games will simply
fail to load. If a case ever justifies re-adding it (e.g. a specifically
mame2003_plus-compatible ROM set), reintroduce the mapping and let
ROMSET_SENSITIVE_SYSTEMS's caveat banner carry the risk again, same as
fba/fbneo.
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
    "fba": "fbneo",
    "fbneo": "fbneo",
}

# Systems in SYSTEM_CORE_MAP where core support existing doesn't mean a given
# ROM will actually boot -- see the module docstring. The frontend uses this
# to show a compatibility caveat instead of the plain "Play in Browser" button
# it shows for every other system.
ROMSET_SENSITIVE_SYSTEMS: FrozenSet[str] = frozenset({"fba", "fbneo"})


def browser_play_core_for_system(system: str) -> Optional[str]:
    """The EmulatorJS core id for ``system``, or ``None`` if unsupported."""
    return SYSTEM_CORE_MAP.get(str(system or "").strip().lower())


def browser_play_is_romset_sensitive(system: str) -> bool:
    """Whether ``system``'s browser core needs an exact romset match to boot."""
    return str(system or "").strip().lower() in ROMSET_SENSITIVE_SYSTEMS
