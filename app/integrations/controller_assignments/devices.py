"""Connected game controllers, read from ``/proc`` and sysfs only.

No device is opened and nothing probes USB (see the Stream Deck
``hid_enumerate`` gotcha): this is a text parse of
``/proc/bus/input/devices`` plus a few sysfs attribute reads. Light guns,
keyboards and mice are not listed; only joystick (``js*``) devices are.
"""

import re
from pathlib import Path
from typing import List

from .launch_order import classify, device_facts, is_gun


def _sdl_guid(bus: str, vendor: str, product: str, version: str) -> str:
    def le(value: str) -> str:
        value = (value or "0").lower().rjust(4, "0")[-4:]
        return value[2:4] + value[0:2]
    if not all(re.fullmatch(r"[0-9a-fA-F]{1,4}", part or "") for part in (bus, vendor, product, version)):
        return ""
    return f"{le(bus)}0000{le(vendor)}0000{le(product)}0000{le(version)}0000"


def connected_controllers(proc_root: Path = Path("/proc"), sys_root: str = "/sys",
                          udev_root: str = "/run/udev/data") -> List[dict]:
    try:
        text = (Path(proc_root) / "bus" / "input" / "devices").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    controllers: List[dict] = []
    for block in text.split("\n\n"):
        name_match = re.search(r'^N: Name="(.*)"\s*$', block, re.M)
        handlers = re.search(r"^H: Handlers=(.*)$", block, re.M)
        ids = re.search(r"^I: Bus=(\w+) Vendor=(\w+) Product=(\w+) Version=(\w+)", block, re.M)
        if not name_match or not handlers:
            continue
        tokens = handlers.group(1).split()
        js = next((token for token in tokens if re.fullmatch(r"js\d+", token)), "")
        event = next((token for token in tokens if re.fullmatch(r"event\d+", token)), "")
        if not js or not event:
            continue
        name = name_match.group(1).strip()
        guid = _sdl_guid(*ids.groups()) if ids else ""
        facts = device_facts(f"/dev/input/{event}", guid, sys_root, udev_root)
        if is_gun(name, facts):
            continue
        controllers.append({
            "name": name, "guid": guid, "port": facts["port"], "event": event,
            "vendor": facts["vendor"], "product": facts["product"],
            "type": classify(name, facts["vendor"], facts["product"]),
        })
    controllers.sort(key=lambda item: (item["name"].lower(), item["port"]))
    return controllers
