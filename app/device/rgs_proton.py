"""Keep RGS NAT-PMP discovery on Proton's VPN gateway when port forwarding is enabled.

RGS owns its download service and replaces it during updates. Drone applies
this narrow, idempotent compatibility patch at startup and adds a post-update
hook to RGS's stable upgrade script so future updates get patched immediately.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

RGS_NATPMP_HELPER = Path(os.environ.get(
    "DRONE_RGS_NATPMP_HELPER", "/userdata/system/rgs/download_service/natpmp_helper.py"
))
RGS_UPGRADE_SCRIPT = Path(os.environ.get(
    "DRONE_RGS_UPGRADE_SCRIPT", "/userdata/roms/rgs/rgs_upgrade.sh"
))
RGS_SERVICE_SCRIPT = Path(os.environ.get(
    "DRONE_RGS_SERVICE_SCRIPT", "/userdata/system/services/rgs_download"
))
DRONE_APP_HELPER = Path(os.environ.get(
    "DRONE_RGS_PROTON_HELPER", "/userdata/system/drone-app/app/device/rgs_proton.py"
))
BACKUP_DIR = Path(os.environ.get(
    "DRONE_RGS_PROTON_BACKUP_DIR", "/userdata/system/backups/hotfixes/rgs-proton-port-forwarding"
))

_PATCH_MARKER = "# >>> drone-proton-pmp-gateway"
_UPDATER_MARKER = "# >>> drone-proton-pmp-post-update"

_NATPMP_HELPER_BLOCK = '''# >>> drone-proton-pmp-gateway
def _drone_proton_natpmp_gateway():
    """Use Proton's NAT-PMP endpoint only when Drone enabled +pmp."""
    import os

    auth_path = os.environ.get("DRONE_PROTON_AUTH_FILE")
    if not auth_path:
        config_path = os.environ.get(
            "DRONE_PROTON_VPN_CONFIG", "/userdata/system/drone-app/vpn/client.ovpn"
        )
        auth_path = "/userdata/system/drone-app/vpn/auth.txt"
        try:
            with open(config_path, "r", encoding="utf-8") as config:
                for line in config:
                    fields = line.strip().split(None, 1)
                    if len(fields) == 2 and fields[0] == "auth-user-pass":
                        candidate = fields[1].strip().strip('"')
                        if candidate:
                            auth_path = candidate if os.path.isabs(candidate) else os.path.join(
                                os.path.dirname(config_path), candidate
                            )
                        break
        except OSError:
            pass
    try:
        with open(auth_path, "r", encoding="utf-8") as credentials:
            return "10.2.0.1" if credentials.readline().strip().lower().endswith("+pmp") else None
    except OSError:
        return None
# <<< drone-proton-pmp-gateway
'''


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        if path.exists():
            shutil.copymode(path, temporary)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _backup_once(path: Path, content: str) -> None:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    backup = BACKUP_DIR / f"{path.name}.pristine-{digest}"
    if not backup.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup.write_text(content, encoding="utf-8")
        try:
            shutil.copymode(path, backup)
        except OSError:
            pass


def _patch_natpmp_helper(path: Path = RGS_NATPMP_HELPER) -> str:
    if not path.is_file():
        return f"RGS NAT-PMP helper not present: {path}"
    source = path.read_text(encoding="utf-8")
    if _PATCH_MARKER in source:
        return "RGS NAT-PMP helper already patched"

    function = re.search(r"(?m)^def discover_gateway\(\):\s*$", source)
    if not function:
        return "RGS NAT-PMP helper changed upstream; no changes applied"
    doc_start = source.find('"""', function.end())
    doc_end = source.find('"""', doc_start + 3) if doc_start >= 0 else -1
    if doc_end < 0:
        return "RGS NAT-PMP helper has an unexpected discover_gateway docstring; no changes applied"
    body_start = source.find("\n", doc_end + 3) + 1
    if not source.startswith("    gateway = None", body_start):
        return "RGS NAT-PMP helper has an unexpected discover_gateway body; no changes applied"

    updated = (
        source[:function.start()]
        + _NATPMP_HELPER_BLOCK
        + source[function.start():body_start]
        + "    proton_gateway = _drone_proton_natpmp_gateway()\n"
        + "    if proton_gateway:\n"
        + "        logger.info(f\"NAT-PMP: using Proton VPN gateway {proton_gateway}\")\n"
        + "        return proton_gateway\n"
        + source[body_start:]
    )
    try:
        compile(updated, str(path), "exec")
    except SyntaxError as error:
        return f"Patched RGS NAT-PMP helper failed syntax validation ({error}); no changes applied"

    _backup_once(path, source)
    _atomic_write(path, updated)
    return "Patched RGS NAT-PMP helper for Proton VPN"


def _patch_rgs_updater(path: Path = RGS_UPGRADE_SCRIPT) -> str:
    if not path.is_file():
        return f"RGS upgrade script not present: {path}"
    source = path.read_text(encoding="utf-8")
    if _UPDATER_MARKER in source:
        return "RGS upgrade script already has the Drone post-update hook"

    # RGS's updater removes its downloaded update script after applying it.
    # Hook after the final cleanup command so the just-installed helper is
    # repaired before the upgrade script exits.
    matches = list(re.finditer(r"(?m)^rm \$UPGRADE_SCRIPT\s*$", source))
    if len(matches) < 2:
        return "RGS upgrade script layout changed upstream; no hook installed"
    match = matches[-1]
    hook = (
        "\n"
        f"{_UPDATER_MARKER}\n"
        f"python3 {shlex.quote(str(DRONE_APP_HELPER))} apply-helper || "
        "echo 'Drone Proton NAT-PMP integration could not be re-applied' >&2\n"
        "# <<< drone-proton-pmp-post-update"
    )
    updated = source[:match.end()] + hook + source[match.end():]
    _backup_once(path, source)
    _atomic_write(path, updated)
    return "Installed Drone post-update hook in the RGS upgrade script"


def _patch_rgs_service(path: Path = RGS_SERVICE_SCRIPT) -> str:
    if not path.is_file():
        return f"RGS service launcher not present: {path}"
    source = path.read_text(encoding="utf-8")
    if _UPDATER_MARKER in source:
        return "RGS service launcher already applies the Drone patch before startup"

    anchor = "    # Start the service\n"
    if source.count(anchor) != 1:
        return "RGS service launcher layout changed upstream; no startup hook installed"
    hook = (
        f"    {_UPDATER_MARKER}\n"
        f"    python3 {shlex.quote(str(DRONE_APP_HELPER))} apply-helper || "
        "echo 'Drone Proton NAT-PMP integration could not be re-applied' >&2\n"
        "    # <<< drone-proton-pmp-post-update\n"
    )
    updated = source.replace(anchor, hook + anchor, 1)
    _backup_once(path, source)
    _atomic_write(path, updated)
    return "Installed pre-start patch hook in the RGS service launcher"


def apply(*, include_updater: bool = True) -> list[str]:
    results = [_patch_natpmp_helper()]
    if include_updater:
        results.append(_patch_rgs_service())
        results.append(_patch_rgs_updater())
    return results


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    action = args[0] if args else "apply"
    if action not in ("apply", "apply-helper"):
        print("Usage: rgs_proton.py [apply|apply-helper]", file=sys.stderr)
        return 2
    for result in apply(include_updater=(action == "apply")):
        print(f"[drone-rgs-proton] {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
