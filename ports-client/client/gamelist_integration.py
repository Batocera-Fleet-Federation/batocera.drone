#!/usr/bin/env python3
"""Keep the native Drone launcher's EmulationStation artwork metadata valid.

The Ports release bundle owns one launcher and its artwork images, but the
surrounding gamelist.xml belongs to the user and may contain many unrelated
Ports entries.  This helper updates only the Drone entry and writes the file
atomically.  It is usable both as an imported helper in tests and as a small
post-install command from the shell installer/API update worker.
"""

import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

LAUNCHER_FILE = "batocera-drone-client.sh"
DISPLAY_NAME = "Batocera Drone"
MARQUEE_RELATIVE_PATH = "./images/batocera-drone_marquee.png"
IMAGE_RELATIVE_PATH = "./images/main.jpg"
THUMBNAIL_RELATIVE_PATH = IMAGE_RELATIVE_PATH


def _normalized_launcher_path(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.rstrip("/")


def _is_drone_entry(game: ET.Element) -> bool:
    path = _normalized_launcher_path(game.findtext("path") or "")
    return path == LAUNCHER_FILE or path.endswith(f"/{LAUNCHER_FILE}")


def ensure_ports_gamelist(ports_dir: Path) -> dict:
    ports_dir = Path(ports_dir).resolve()
    launcher = ports_dir / LAUNCHER_FILE
    marquee = ports_dir / MARQUEE_RELATIVE_PATH.removeprefix("./")
    image = ports_dir / IMAGE_RELATIVE_PATH.removeprefix("./")
    if not launcher.is_file():
        raise FileNotFoundError(f"Ports launcher is missing: {launcher}")
    if not marquee.is_file() or marquee.stat().st_size <= 0:
        raise FileNotFoundError(f"Ports marquee is missing or empty: {marquee}")
    if not image.is_file() or image.stat().st_size <= 0:
        raise FileNotFoundError(f"Ports image is missing or empty: {image}")

    gamelist = ports_dir / "gamelist.xml"
    if gamelist.exists():
        tree = ET.parse(gamelist)
        root = tree.getroot()
        if root.tag != "gameList":
            raise ValueError(f"Unexpected Ports gamelist root: {root.tag}")
    else:
        root = ET.Element("gameList")
        tree = ET.ElementTree(root)

    game = next((entry for entry in root.findall("game") if _is_drone_entry(entry)), None)
    created = game is None
    if game is None:
        game = ET.SubElement(root, "game")
        ET.SubElement(game, "path").text = f"./{LAUNCHER_FILE}"
        ET.SubElement(game, "name").text = DISPLAY_NAME

    marquee_node = game.find("marquee")
    previous = str(marquee_node.text or "") if marquee_node is not None else ""
    changed = created or previous != MARQUEE_RELATIVE_PATH
    if marquee_node is None:
        marquee_node = ET.SubElement(game, "marquee")
    marquee_node.text = MARQUEE_RELATIVE_PATH

    image_node = game.find("image")
    previous_image = str(image_node.text or "") if image_node is not None else ""
    changed = changed or previous_image != IMAGE_RELATIVE_PATH
    if image_node is None:
        image_node = ET.SubElement(game, "image")
    image_node.text = IMAGE_RELATIVE_PATH

    # Several Batocera themes, including Hypermax-Plus-PixN's default
    # gamecarousel view, render {game:thumbnail} beside md_marquee instead of
    # rendering md_image. Point both metadata roles at the same complete Drone
    # artwork so it is visible across detailed, grid, and carousel themes.
    thumbnail_node = game.find("thumbnail")
    previous_thumbnail = str(thumbnail_node.text or "") if thumbnail_node is not None else ""
    changed = changed or previous_thumbnail != THUMBNAIL_RELATIVE_PATH
    if thumbnail_node is None:
        thumbnail_node = ET.SubElement(game, "thumbnail")
    thumbnail_node.text = THUMBNAIL_RELATIVE_PATH

    if changed:
        try:
            ET.indent(tree, space="  ")
        except AttributeError:  # pragma: no cover - Python < 3.9 compatibility
            pass
        temp_path = gamelist.with_name(f".{gamelist.name}.drone-{os.getpid()}.tmp")
        try:
            tree.write(temp_path, encoding="utf-8", xml_declaration=True)
            temp_path.replace(gamelist)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    return {
        "status": "updated" if changed else "current",
        "gamelist": str(gamelist),
        "marquee": MARQUEE_RELATIVE_PATH,
        "image": IMAGE_RELATIVE_PATH,
        "thumbnail": THUMBNAIL_RELATIVE_PATH,
        "entry_created": created,
    }


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: gamelist_integration.py <ports_dir>", file=sys.stderr)
        return 2
    try:
        result = ensure_ports_gamelist(Path(args[0]))
    except (OSError, ValueError, ET.ParseError) as error:
        print(f"Could not update the Drone Ports gamelist entry: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
