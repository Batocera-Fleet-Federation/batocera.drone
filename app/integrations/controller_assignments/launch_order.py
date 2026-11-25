#!/usr/bin/env python3
"""Reorder EmulationStation's player controllers before ``emulatorlauncher``.

EmulationStation decides player order in memory and passes it to the launcher
as ``-p<n>index/guid/name/devicepath/nbbuttons/nbhats/nbaxes`` arguments;
configgen trusts those arguments completely, and ``gameStart`` scripts run
only after they have been loaded. The Controller Assignments integration
therefore points each system's ``<command>`` at this file::

    <bin>/drone-controller-order --runtime <state>/runtime.json -- <original command...>

This module is deliberately **self-contained and stdlib-only**: the
integration copies its source verbatim to ``bin/drone-controller-order`` so a
launch never imports the Drone application (which may be mid-update). It is
also imported by the Drone for classification and previews, so the same
rules run in both places.

Behaviour, in order of authority:

1. A controller Batocera's own player assignment pins (``INPUT P<n>GUID/NAME/
   PATH`` in ``es_settings.cfg``) and that EmulationStation actually passed
   stays exactly where EmulationStation put it.
2. Light guns are never moved (they are ignored by the integration).
3. Every other controller is ordered by the most specific matching rule
   into the slots that remain open: a game rule, then a system rule, then an
   enabled preset, then (when the global "best effort" setting is on) a
   per-system fit score (family, analog stick, button count) with the default
   order breaking ties, then the default order alone. Controllers a rule does
   not mention follow in EmulationStation's original order. Nobody is dropped
   and the set of slot numbers is unchanged.

Any failure falls back to the untouched original command: this wrapper must
never be the reason a game does not start.
"""

import json
import os
import re
import shlex
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

PLAYER_KEYS = ("index", "guid", "name", "devicepath", "nbbuttons", "nbhats", "nbaxes")
_PLAYER_ARG = re.compile(r"^-p([1-8])(" + "|".join(PLAYER_KEYS) + r")$")
_USB_PORT = re.compile(r"^\d+-\d+(?:\.\d+)*$")
_GUN_NAME = re.compile(r"lightgun|light gun|sinden|gun4ir|aimtrak|guncon|g-con|wiimote ir", re.IGNORECASE)
MAX_PLAYERS = 8
LOG_PREFIX = "[drone-controller-order]"

# Ordered most specific first: the first matching family wins.
CONTROLLER_TYPES = (
    ("gamecube", "GameCube"),
    ("n64", "Nintendo 64"),
    ("snes", "Super Nintendo"),
    ("nes", "NES / Famicom"),
    ("sega", "Sega"),
    ("playstation", "PlayStation"),
    ("xbox", "Xbox"),
    ("nintendo", "Nintendo (Switch / Pro)"),
    ("arcade", "Arcade stick"),
    ("generic", "Other gamepad"),
)
TYPE_IDS = tuple(item[0] for item in CONTROLLER_TYPES)

# Exact (vendor, product) identities, checked first. Sources: SDL's
# controller tables, the Linux hid-nintendo driver, and common clones and
# adapters. The Switch Online NES pads report Joy-Con IDs (057e:2006/2007),
# so they are recognised by name instead.
_KNOWN_IDS = {
    "gamecube": {("057e", "0337"),                   # Nintendo / Mayflash Wii U GameCube adapter
                 ("0079", "1843"), ("0079", "1844"),  # Mayflash GameCube adapters in PC mode
                 ("20d6", "a711")},                  # PowerA wired GameCube-style pad
    "n64": {("057e", "2019")},                       # Nintendo Switch Online N64
    "snes": {("057e", "2017"),                       # Nintendo Switch Online SNES
             ("0583", "2060"),                       # iBuffalo classic USB pad
             ("0079", "0011"), ("0810", "e501")},    # the very common USB SNES-style clones
    "nes": set(),                                    # recognised by name (NSO NES pads reuse Joy-Con IDs)
    "sega": {("057e", "201e"),                       # Nintendo Switch Online Mega Drive / Genesis
             ("0ca3", None)},                        # Sega Corp (Mega Drive / Astro City mini pads)
    "playstation": {("054c", None),                  # Sony
                    ("0810", "0001"), ("0810", "0003")},  # "Twin USB" PlayStation-to-USB adapters
    "xbox": {("045e", None), ("24c6", None), ("1bad", None), ("0e6f", None),  # Microsoft + XInput makers
             ("28de", "11ff")},                      # Steam virtual gamepad (XInput layout)
    "nintendo": {("057e", None), ("0f0d", "00c1")},  # Nintendo, HORIPAD for Switch
    "arcade": {("0079", "0006"),                     # DragonRise zero-delay encoders
               ("16c0", "05e1"),                     # Xin-Mo
               ("d209", None)},                      # Ultimarc
}


def _words(pattern):
    """Word boundaries that also treat ``_``, ``/`` and ``-`` as separators."""
    return re.compile(r"(?<![a-z0-9])(?:" + pattern + r")(?![a-z0-9])", re.I)


# Ordered most specific first: the first match wins.
_NAME_PATTERNS = (
    ("gamecube", _words(r"gamecube|game cube|ngc|gc adapter|gamecube adapter|wii u gamecube")),
    ("n64", _words(r"n64|nintendo 64|nintendo64|8bitdo 64|64 controller")),
    ("snes", _words(r"snes|super nintendo|super famicom|sfc|sn30|sf30|sn30 pro|snes30")),
    ("nes", _words(r"nes|famicom|nes30|n30|nes controller.*")),
    ("sega", _words(r"sega|genesis|mega ?drive|megadrive|saturn|dreamcast|master ?system|m30|"
                    r"astro city|control pad")),
    ("playstation", _words(r"playstation|dualshock ?[1-4]?|dualsense|ps[1-5]|sony|ds[34]|twin usb|twin shock")),
    ("xbox", _words(r"x-?box|xinput|x-?input")),
    ("nintendo", _words(r"pro controller|joy-?con|nintendo|switch")),
    ("arcade", _words(r"arcade|arcade stick|fight ?stick|xin-?mo|dragonrise|ultimarc|ultrastik|"
                      r"zero delay|jamma|i-?pac|encoder")),
)
_VENDOR_ONLY_ORDER = ("sega", "playstation", "xbox", "nintendo", "arcade")

# Families whose controllers always have an analog stick.
ANALOG_FAMILIES = frozenset(("gamecube", "n64", "playstation", "xbox", "nintendo"))
# Families that are digital pads/sticks (d-pad only).
DIGITAL_FAMILIES = frozenset(("nes", "snes", "sega", "arcade"))

# What a system wants, for best-effort ordering: preferred families
# (best first), whether it needs an analog stick, and how many buttons
# (face + shoulder) a controller should have to play it comfortably.
SYSTEM_PROFILES = {
    "nes": {"label": "NES-style", "families": ("nes", "snes", "sega", "nintendo", "xbox", "playstation"),
            "analog": False, "buttons": 2},
    "snes": {"label": "SNES-style", "families": ("snes", "nintendo", "sega", "xbox", "playstation", "nes"),
             "analog": False, "buttons": 6},
    "n64": {"label": "Nintendo 64", "families": ("n64", "gamecube", "nintendo", "xbox", "playstation"),
            "analog": True, "buttons": 6},
    "gamecube": {"label": "GameCube", "families": ("gamecube", "nintendo", "xbox", "playstation", "n64"),
                 "analog": True, "buttons": 8},
    "sega": {"label": "Sega 2D", "families": ("sega", "snes", "arcade", "nintendo", "xbox", "playstation", "nes"),
             "analog": False, "buttons": 3},
    "saturn": {"label": "Saturn", "families": ("sega", "snes", "playstation", "xbox", "nintendo", "arcade"),
               "analog": False, "buttons": 6},
    "dreamcast": {"label": "Dreamcast", "families": ("sega", "xbox", "gamecube", "playstation", "nintendo"),
                  "analog": True, "buttons": 4},
    "psx": {"label": "PlayStation", "families": ("playstation", "xbox", "nintendo", "snes"),
            "analog": False, "buttons": 8},
    "ps-modern": {"label": "PlayStation 2+", "families": ("playstation", "xbox", "nintendo"),
                  "analog": True, "buttons": 10},
    "xbox": {"label": "Xbox", "families": ("xbox", "playstation", "nintendo"), "analog": True, "buttons": 10},
    "switch": {"label": "Switch / Wii U / 3DS", "families": ("nintendo", "xbox", "playstation", "gamecube"),
               "analog": True, "buttons": 10},
    "arcade": {"label": "Arcade", "families": ("arcade", "sega", "snes", "playstation", "xbox", "nintendo"),
               "analog": False, "buttons": 4},
}
SYSTEM_PROFILE_BY_SYSTEM = {}
for _profile, _systems in {
    "nes": ("nes", "famicom", "fds", "nesh", "nes-msu", "gb", "gbc", "gbh", "gbch", "pcengine", "pcenginecd",
            "tg16", "tgcd", "supergrafx", "colecovision", "sg1000"),
    "snes": ("snes", "sfc", "snes-msu1", "snesh", "satellaview", "sufami", "sgb", "gba", "gbah", "nds", "virtualboy",
             "pcfx", "neogeocd", "3do"),
    "n64": ("n64", "n64-jp", "n64h", "n64dd", "iqueplayer"),
    "gamecube": ("gamecube", "gamecube-jp", "gamecubeh", "triforce"),
    "sega": ("megadrive", "genesis", "megadrive-jp", "mastersystem", "mark3", "gamegear", "sega32x", "segacd",
             "megacd", "megacd32x", "sega32xcd", "pico"),
    "saturn": ("saturn", "saturn-jp"),
    "dreamcast": ("dreamcast", "dreamcast-jp"),
    "psx": ("psx",),
    "ps-modern": ("ps2", "ps3", "ps4", "psp", "psvita", "namco2x6"),
    "xbox": ("xbox", "xbox360", "xbla"),
    "switch": ("switch", "switch2", "wiiu", "3ds"),
    "arcade": ("mame", "fbneo", "neogeo", "neogeomvs", "cps1", "cps2", "cps3", "naomi", "naomi2", "atomiswave",
               "model1", "model2", "model3", "segastv", "hikaru", "daphne", "lindbergh", "namco22", "namco10",
               "namco11", "namco12", "namco3xx", "konamigx", "igspgm", "zinc", "gaelco", "cave3rd",
               "segasystem32", "arcade", "teknoparrot"),
}.items():
    for _system in _systems:
        SYSTEM_PROFILE_BY_SYSTEM[_system] = _profile


# ------------------------------------------------------------ device facts
def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _event_name(devicepath):
    name = os.path.basename(str(devicepath or ""))
    return name if re.fullmatch(r"event\d+", name) else ""


def guid_ids(guid):
    """Vendor/product (lower-case hex) encoded in an SDL2 joystick GUID."""
    text = str(guid or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", text):
        return "", ""
    vendor = text[10:12] + text[8:10]
    product = text[18:20] + text[16:18]
    return vendor, product


def device_facts(devicepath, guid="", sys_root="/sys", udev_root="/run/udev/data"):
    """Stable identity and classification hints for an evdev node."""
    vendor, product = guid_ids(guid)
    port = ""
    gun = False
    event = _event_name(devicepath)
    if event:
        base = Path(sys_root) / "class" / "input" / event / "device"
        vendor = _read(base / "id" / "vendor").lower() or vendor
        product = _read(base / "id" / "product").lower() or product
        try:
            resolved = Path(os.path.realpath(str(base)))
            ports = [part for part in resolved.parts if _USB_PORT.fullmatch(part)]
            if ports:
                port = "usb:" + ports[-1]
        except OSError:
            pass
        if not port:
            uniq = _read(base / "uniq")
            if uniq:
                port = "bt:" + uniq.lower()
        dev = _read(Path(sys_root) / "class" / "input" / event / "dev")
        if dev:
            gun = "ID_INPUT_GUN=1" in _read(Path(udev_root) / f"c{dev}")
    return {"vendor": vendor, "product": product, "port": port, "gun": gun}


def classify(name, vendor="", product=""):
    """Controller family: exact USB identity, then name, then vendor."""
    vendor = str(vendor or "").lower()
    product = str(product or "").lower()
    for type_id, ids in _KNOWN_IDS.items():
        if (vendor, product) in ids:
            return type_id
    text = str(name or "").replace("_", " ")
    for type_id, pattern in _NAME_PATTERNS:
        if pattern.search(text):
            return type_id
    for type_id in _VENDOR_ONLY_ORDER:
        if (vendor, None) in _KNOWN_IDS[type_id]:
            return type_id
    return "generic"


def _int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def has_analog(controller):
    if controller["type"] in ANALOG_FAMILIES:
        return True
    if controller["type"] in DIGITAL_FAMILIES:
        return False
    axes = controller.get("axes")
    return axes is None or axes >= 4


def fit_score(controller, profile):
    """How well a controller suits a system profile (higher is better)."""
    families = profile["families"]
    family = controller["type"]
    if family in families:
        score = 100 - 8 * families.index(family)
    elif family == "generic":
        score = 35
    elif family == "arcade":
        score = 10
    else:
        score = 25
    if profile["analog"] and not has_analog(controller):
        score -= 40
    buttons = controller.get("buttons")
    if buttons is not None and buttons < profile["buttons"]:
        score -= 15
    return score


def best_effort_order(controllers, system, tie_break):
    """Controllers ranked for ``system``; ``None`` when the system is unknown."""
    profile = SYSTEM_PROFILES.get(SYSTEM_PROFILE_BY_SYSTEM.get(system, ""))
    if profile is None:
        return None

    def tie(controller):
        for index, selector in enumerate(tie_break or []):
            if selector_matches(selector, controller):
                return index
        return len(tie_break or [])
    return sorted(controllers, key=lambda c: (-fit_score(c, profile), tie(c), c["slot"]))


def is_gun(name, facts=None):
    return bool((facts or {}).get("gun")) or bool(_GUN_NAME.search(str(name or "")))


# --------------------------------------------------------- argv handling
def parse_players(argv):
    """Split ``argv`` into (other args, insert position, {slot: {key: value}})."""
    players = {}
    rest = []
    insert_at = None
    i = 0
    while i < len(argv):
        match = _PLAYER_ARG.match(argv[i])
        if match and i + 1 < len(argv):
            if insert_at is None:
                insert_at = len(rest)
            players.setdefault(int(match.group(1)), {})[match.group(2)] = argv[i + 1]
            i += 2
            continue
        rest.append(argv[i])
        i += 1
    return rest, (len(rest) if insert_at is None else insert_at), players


def build_argv(rest, insert_at, assignment):
    group = []
    for slot in sorted(assignment):
        values = assignment[slot]
        for key in PLAYER_KEYS:
            if key in values:
                group.extend([f"-p{slot}{key}", values[key]])
    return rest[:insert_at] + group + rest[insert_at:]


def option_value(argv, flag):
    for index, token in enumerate(argv[:-1]):
        if token == flag:
            return argv[index + 1]
    return ""


# ------------------------------------------------- batocera's own pins
def read_batocera_pins(es_settings_file):
    """``{slot: {guid, name, path}}`` from EmulationStation's player assignment."""
    pins = {}
    try:
        root = ET.parse(str(es_settings_file)).getroot()
    except (OSError, ET.ParseError):
        return pins
    values = {}
    for node in root.iter():
        name = node.get("name") or ""
        match = re.fullmatch(r"INPUT P([1-8])(GUID|NAME|PATH)", name)
        if match:
            values.setdefault(int(match.group(1)), {})[match.group(2).lower()] = str(node.get("value") or "")
    for slot, item in values.items():
        if item.get("guid") and item.get("name") and item.get("guid") != "-1":
            pins[slot] = {"guid": item["guid"], "name": item["name"].strip(), "path": item.get("path", "")}
    return pins


def _pin_port(path):
    ports = [part for part in Path(str(path or "")).parts if _USB_PORT.fullmatch(part)]
    return "usb:" + ports[-1] if ports else ""


# ---------------------------------------------------------- the ordering
def controller_view(slot, values, sys_root="/sys", udev_root="/run/udev/data"):
    name = str(values.get("name") or "").strip()
    facts = device_facts(values.get("devicepath"), values.get("guid"), sys_root, udev_root)
    return {
        "slot": slot, "name": name, "guid": str(values.get("guid") or ""),
        "devicepath": str(values.get("devicepath") or ""), "port": facts["port"],
        "vendor": facts["vendor"], "product": facts["product"],
        "type": classify(name, facts["vendor"], facts["product"]), "gun": is_gun(name, facts),
        "buttons": _int(values.get("nbbuttons")), "axes": _int(values.get("nbaxes")),
    }


def selector_matches(selector, controller):
    if not isinstance(selector, dict):
        return False
    if selector.get("kind") == "type":
        return selector.get("type") == controller["type"]
    if selector.get("kind") == "device":
        if str(selector.get("name") or "").strip() != controller["name"]:
            return False
        port = str(selector.get("port") or "")
        if port:
            # The physical socket is the stable identity; the GUID's version
            # field can differ between SDL and EmulationStation builds.
            return port == controller["port"]
        guid = str(selector.get("guid") or "").lower()
        return not guid or guid == controller["guid"].lower()
    return False


def compute_assignment(controllers, order, pins, ranker=None):
    """Return ``(assignment {slot: controller}, locks {slot: reason})``.

    ``controllers`` are views in EmulationStation's slot order. Pinned and
    gun controllers keep their slot; the rest fill the open slots by rule.
    """
    by_slot = sorted(controllers, key=lambda item: item["slot"])
    locks = {}
    for controller in by_slot:
        if controller["gun"]:
            locks[controller["slot"]] = "light gun"
    for pin_slot in sorted(pins):
        pin = pins[pin_slot]
        candidates = [c for c in by_slot if c["slot"] not in locks
                      and c["guid"] == pin["guid"] and c["name"] == pin["name"]]
        if not candidates:
            continue
        pin_port = _pin_port(pin.get("path"))
        chosen = (next((c for c in candidates if c["slot"] == pin_slot), None)
                  or next((c for c in candidates if pin_port and c["port"] == pin_port), None)
                  or candidates[0])
        locks[chosen["slot"]] = f"Batocera P{pin_slot}"
    free = [c for c in by_slot if c["slot"] not in locks]
    ordered = []
    if ranker is not None:
        ordered = list(ranker(free))
    else:
        for selector in order or []:
            for controller in free:
                if controller not in ordered and selector_matches(selector, controller):
                    ordered.append(controller)
    ordered.extend(c for c in free if c not in ordered)
    assignment = {c["slot"]: c for c in by_slot if c["slot"] in locks}
    open_slots = sorted(c["slot"] for c in free)
    for slot, controller in zip(open_slots, ordered):
        assignment[slot] = controller
    return assignment, locks


def rom_key(system, rom, roms_root):
    rom = str(rom or "")
    prefix = f"{str(roms_root).rstrip('/')}/{system}/"
    if rom.startswith(prefix):
        return f"{system}/{rom[len(prefix):]}"
    marker = f"/{system}/"
    if marker in rom:
        return f"{system}/{rom.rsplit(marker, 1)[1]}"
    return f"{system}/{os.path.basename(rom)}"


def resolve_rule(runtime, system, game_key):
    """``(source, order, ranked)`` for this launch.

    ``ranked`` means "score controllers for this system" (a preset or the
    global best-effort setting); ``order`` is then only the tie-breaker.
    """
    games = runtime.get("games") if isinstance(runtime.get("games"), dict) else {}
    systems = runtime.get("systems") if isinstance(runtime.get("systems"), dict) else {}
    default = runtime.get("default") if isinstance(runtime.get("default"), list) else []
    if isinstance(games.get(game_key), dict):
        return "game", list(games[game_key].get("order") or []), False
    rule = systems.get(system)
    if isinstance(rule, dict):
        if rule.get("mode") == "best-effort" and system in SYSTEM_PROFILE_BY_SYSTEM:
            return str(rule.get("source") or "preset"), list(default), True
        if rule.get("mode") != "best-effort":
            return str(rule.get("source") or "system"), list(rule.get("order") or []), False
    if runtime.get("auto") and system in SYSTEM_PROFILE_BY_SYSTEM:
        return "best-effort", list(default), True
    if default:
        return "default", list(default), False
    return "none", [], False


def plan(argv, runtime, *, sys_root="/sys", udev_root="/run/udev/data"):
    """Pure-ish planning step: returns (new argv, report)."""
    rest, insert_at, players = parse_players(argv)
    system = option_value(argv, "-system")
    rom = option_value(argv, "-rom")
    game_key = rom_key(system, rom, runtime.get("roms_root") or "/userdata/roms")
    source, order, ranked = resolve_rule(runtime, system, game_key)
    controllers = [controller_view(slot, values, sys_root, udev_root) for slot, values in sorted(players.items())]
    report = {"system": system, "game": game_key, "rule": source, "changed": False,
              "profile": SYSTEM_PROFILE_BY_SYSTEM.get(system, "") if ranked else "",
              "before": [_public(c) for c in controllers], "players": [], "warnings": []}
    if not runtime.get("enabled") or source == "none" or not controllers:
        report["players"] = [dict(_public(c), lock="") for c in controllers]
        return argv, report
    pins = read_batocera_pins(runtime.get("es_settings_file") or "")
    ranker = None
    if ranked:
        ranker = lambda free: best_effort_order(free, system, order)  # noqa: E731
    assignment, locks = compute_assignment(controllers, order, pins, ranker)
    new_players = {slot: players[controller["slot"]] for slot, controller in assignment.items()}
    report["players"] = [dict(_public(assignment[slot]), slot=slot, lock=locks.get(slot, ""))
                         for slot in sorted(assignment)]
    report["changed"] = any(controller["slot"] != slot for slot, controller in assignment.items())
    placed = {c["slot"] for c in controllers}
    for selector in order:
        if selector.get("kind") == "device" and not any(selector_matches(selector, c) for c in controllers):
            report["warnings"].append(
                f"{selector.get('name') or 'A controller'} was not passed to this game "
                f"(not connected, or beyond EmulationStation's {MAX_PLAYERS} players)")
    if len(placed) >= MAX_PLAYERS:
        report["warnings"].append(f"EmulationStation passed {MAX_PLAYERS} controllers; any others were left out")
    return build_argv(rest, insert_at, new_players), report


def _public(controller):
    return {key: controller.get(key) for key in ("slot", "name", "type", "port", "gun", "buttons", "axes")}


# --------------------------------------------------------------- runtime
def _write_report(path, report):
    path = Path(path)
    try:
        if path.parent.is_symlink() or path.is_symlink():
            return
        payload = json.dumps(dict(report, at=time.strftime("%Y-%m-%dT%H:%M:%S%z")), indent=2) + "\n"
        fd, raw = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.chmod(raw, 0o644)
            owner = path.parent.stat()
            if os.geteuid() == 0 and (owner.st_uid, owner.st_gid) != (0, 0):
                os.chown(raw, owner.st_uid, owner.st_gid)
            os.replace(raw, path)
        finally:
            if os.path.exists(raw):
                os.unlink(raw)
    except OSError:
        pass


def split_command(argv):
    """``[--runtime PATH] -- command...`` -> (runtime path, command argv)."""
    runtime_path = ""
    args = list(argv)
    if len(args) >= 2 and args[0] == "--runtime":
        runtime_path = args[1]
        args = args[2:]
    if args and args[0] == "--":
        args = args[1:]
    return runtime_path, args


def main(argv=None):
    runtime_path, command = split_command(sys.argv[1:] if argv is None else argv)
    if not command:
        print(f"{LOG_PREFIX} no command to run", file=sys.stderr)
        return 2
    final = command
    try:
        with open(runtime_path, "r", encoding="utf-8") as handle:
            runtime = json.load(handle)
        if isinstance(runtime, dict):
            final, report = plan(command, runtime)
            if runtime.get("report_file"):
                _write_report(runtime["report_file"], report)
            order = ", ".join(f"P{p['slot']}={p['name']}" for p in report["players"])
            print(f"{LOG_PREFIX} rule={report['rule']} changed={report['changed']} {order}", file=sys.stderr)
    except Exception as error:  # noqa: BLE001 - never block a launch
        final = command
        print(f"{LOG_PREFIX} left controller order unchanged: {error}", file=sys.stderr)
    try:
        os.execvp(final[0], final)
    except OSError as error:
        # Never be the reason a game does not start: hand the original
        # command to the shell, exactly as EmulationStation would have.
        print(f"{LOG_PREFIX} exec failed ({error}); running the original command", file=sys.stderr)
        os.execv("/bin/sh", ["/bin/sh", "-c", " ".join(shlex.quote(part) for part in command)])
    return 127  # pragma: no cover - exec does not return


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
