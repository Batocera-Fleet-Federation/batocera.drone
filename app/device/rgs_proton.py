"""Keep RGS port forwarding and network traffic on Proton when enabled.

RGS owns its download service and replaces it during updates. Drone applies
idempotent helper patches at startup and adds a post-update hook to RGS's stable
upgrade script. When Proton port forwarding is active, it binds the RGS Python
service's network sockets to the VPN interface and verifies the VPN route.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
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
RGS_MAIN_SCRIPT = Path(os.environ.get(
    "DRONE_RGS_MAIN_SCRIPT", "/userdata/system/rgs/download_service/download_service.py"
))
DRONE_APP_HELPER = Path(os.environ.get(
    "DRONE_RGS_PROTON_HELPER", "/userdata/system/drone-app/app/device/rgs_proton.py"
))
BACKUP_DIR = Path(os.environ.get(
    "DRONE_RGS_PROTON_BACKUP_DIR", "/userdata/system/backups/hotfixes/rgs-proton-port-forwarding"
))

_PATCH_MARKER = "# >>> drone-proton-pmp-gateway"
_UPDATER_MARKER = "# >>> drone-proton-pmp-post-update"
_SERVICE_GUARD_MARKER = "# >>> drone-rgs-vpn-socket-guard"
_UPNP_PATCH_MARKER = "# >>> drone-proton-pmp-upnp-bypass"

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

_UPNP_HELPER_BLOCK = '''# >>> drone-proton-pmp-upnp-bypass
def _drone_proton_pmp_active():
    """Return whether Drone enabled Proton's port-forwarding credential."""
    import os

    auth_path = os.environ.get("DRONE_PROTON_AUTH_FILE")
    config_path = os.environ.get(
        "DRONE_PROTON_VPN_CONFIG", "/userdata/system/drone-app/vpn/client.ovpn"
    )
    if not auth_path:
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
            return credentials.readline().strip().lower().endswith("+pmp")
    except OSError:
        return False
# <<< drone-proton-pmp-upnp-bypass
'''

_VPN_SOCKET_BLOCK = '''# >>> drone-proton-vpn-socket-binding
import errno as _drone_errno
import socket as _drone_socket
import subprocess as _drone_subprocess
import time as _drone_time

def _drone_proton_route_ready(force=False):
    now = _drone_time.monotonic()
    cached = getattr(_drone_proton_route_ready, "_cache", None)
    if not force and cached and now - cached[0] < 0.05:
        return cached[1]
    try:
        route = _drone_subprocess.run(
            ["ip", "-4", "route", "get", "1.1.1.1"],
            capture_output=True, text=True, timeout=2, check=True
        ).stdout
        route_fields = route.split()
        ready = any(
            route_fields[index:index + 2] == ["dev", "tun0"]
            for index in range(len(route_fields) - 1)
        )
    except (OSError, _drone_subprocess.SubprocessError):
        ready = False
    _drone_proton_route_ready._cache = (now, ready)
    return ready

_drone_original_socket = _drone_socket.socket

class _DroneProtonBoundSocket(_drone_original_socket):
    def _drone_bind_to_tunnel(self, address):
        if self.family not in (_drone_socket.AF_INET, _drone_socket.AF_INET6):
            return
        host = address[0] if isinstance(address, tuple) and address else ""
        if host in ("127.0.0.1", "::1"):
            return
        if not _drone_proton_route_ready():
            raise OSError(_drone_errno.ENETUNREACH, "Proton VPN route via tun0 is unavailable")
        self.setsockopt(_drone_socket.SOL_SOCKET, _drone_socket.SO_BINDTODEVICE, b"tun0\\0")

    def bind(self, address):
        self._drone_bind_to_tunnel(address)
        return super().bind(address)

    def connect(self, address):
        self._drone_bind_to_tunnel(address)
        return super().connect(address)

    def connect_ex(self, address):
        try:
            self._drone_bind_to_tunnel(address)
        except OSError as error:
            return error.errno or _drone_errno.ENETUNREACH
        return super().connect_ex(address)

    def sendto(self, data, *args):
        address = args[-1] if args else None
        if address is not None:
            self._drone_bind_to_tunnel(address)
        return super().sendto(data, *args)

_drone_socket.socket = _DroneProtonBoundSocket
_drone_original_getaddrinfo = _drone_socket.getaddrinfo

def _drone_vpn_getaddrinfo(*args, **kwargs):
    if not _drone_proton_route_ready(force=True):
        raise _drone_socket.gaierror(_drone_socket.EAI_AGAIN, "Proton VPN route via tun0 is unavailable")
    return _drone_original_getaddrinfo(*args, **kwargs)

_drone_socket.getaddrinfo = _drone_vpn_getaddrinfo
# <<< drone-proton-vpn-socket-binding
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


def _patch_upnp_helper(path: Path | None = None) -> str:
    if path is None:
        path = RGS_NATPMP_HELPER.with_name("upnp_helper.py")
    if not path.is_file():
        return f"RGS UPnP helper not present: {path}"
    source = path.read_text(encoding="utf-8")
    if _UPNP_PATCH_MARKER in source:
        return "RGS UPnP helper already patched"

    method = re.search(
        r"(?m)^    async def _discover_upnp\(self\)(?:\s*->[^:]+)?:\s*$",
        source,
    )
    if not method:
        return "RGS UPnP helper changed upstream; no changes applied"
    body_start = source.find("\n", method.end()) + 1
    guard = (
        "        if _drone_proton_pmp_active():\n"
        "            logger.info(\"Proton port forwarding is enabled; skipping router UPnP to keep RGS on VPN\")\n"
        "            return False\n"
    )
    updated = (
        source[:method.start()]
        + _UPNP_HELPER_BLOCK
        + source[method.start():body_start]
        + guard
        + source[body_start:]
    )
    try:
        compile(updated, str(path), "exec")
    except SyntaxError as error:
        return f"Patched RGS UPnP helper failed syntax validation ({error}); no changes applied"

    _backup_once(path, source)
    _atomic_write(path, updated)
    return "Patched RGS UPnP discovery to skip the home router in Proton mode"


def _patch_main_script(*, enabled: bool, path: Path = RGS_MAIN_SCRIPT) -> str:
    if not path.is_file():
        return f"RGS main service script not present: {path}"
    source = path.read_text(encoding="utf-8")
    marker = "# >>> drone-proton-vpn-socket-binding"
    if enabled and marker in source:
        return "RGS main service already binds network sockets to Proton VPN"
    if not enabled and marker in source:
        block = re.compile(
            r"(?ms)^# >>> drone-proton-vpn-socket-binding\n.*?"
            r"^# <<< drone-proton-vpn-socket-binding\n?"
        )
        updated, count = block.subn("", source, count=1)
        if count:
            _atomic_write(path, updated.lstrip("\n"))
            return "Removed the Proton-only socket binding because +pmp is disabled"
        return "RGS VPN socket patch markers are malformed; no changes applied"
    if not enabled:
        return "Proton +pmp is not enabled; no RGS socket patch is needed"
    updated = _VPN_SOCKET_BLOCK + "\n" + source
    try:
        compile(updated, str(path), "exec")
    except SyntaxError as error:
        return f"Patched RGS network guard failed syntax validation ({error}); no changes applied"
    _backup_once(path, source)
    _atomic_write(path, updated)
    return "Patched RGS sockets and DNS lookups to require the Proton tun0 route"


def _ensure_tun_route() -> None:
    try:
        route = subprocess.run(
            ["ip", "-4", "route", "get", "1.1.1.1"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"Could not verify Proton VPN routing: {error}") from error
    if not any(route[index:index + 2] == ["dev", "tun0"] for index in range(len(route) - 1)):
        raise RuntimeError("RGS is configured for Proton +pmp, but the internet route is not through tun0")


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
    if _SERVICE_GUARD_MARKER in source:
        return "RGS service launcher already verifies VPN routing before startup"

    # Upgrade the earlier pre-start hook in place on existing installations.
    legacy_hook = re.compile(
        rf"(?ms)^    {re.escape(_UPDATER_MARKER)}\n.*?"
        r"^    # <<< drone-proton-pmp-post-update\n"
    )
    source, _ = legacy_hook.subn("", source, count=1)

    anchor = "    # Start the service\n"
    if source.count(anchor) != 1:
        return "RGS service launcher layout changed upstream; no startup hook installed"
    hook = (
        f"    {_UPDATER_MARKER}\n"
        f"    {_SERVICE_GUARD_MARKER}\n"
        f"    python3 {shlex.quote(str(DRONE_APP_HELPER))} prepare-service \"$$\" || {{ "
        "echo 'Drone Proton VPN route check failed; RGS will not start' >&2; exit 1; }\n"
        "    # <<< drone-proton-pmp-post-update\n"
    )
    updated = source.replace(anchor, hook + anchor, 1)
    _backup_once(path, source)
    _atomic_write(path, updated)
    return "Installed pre-start patch hook in the RGS service launcher"


def apply(*, include_updater: bool = True) -> list[str]:
    results = [_patch_natpmp_helper(), _patch_upnp_helper()]
    results.append(_patch_main_script(enabled=_proton_pmp_credentials_enabled()))
    if include_updater:
        results.append(_patch_rgs_service())
        results.append(_patch_rgs_updater())
    return results


def _proton_pmp_credentials_enabled() -> bool:
    config_path = Path(os.environ.get(
        "DRONE_PROTON_VPN_CONFIG", "/userdata/system/drone-app/vpn/client.ovpn"
    ))
    auth_path = Path(os.environ.get(
        "DRONE_PROTON_AUTH_FILE", "/userdata/system/drone-app/vpn/auth.txt"
    ))
    try:
        for line in config_path.read_text(encoding="utf-8").splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) == 2 and fields[0] == "auth-user-pass":
                candidate = fields[1].strip().strip('"')
                if candidate:
                    auth_path = Path(candidate) if os.path.isabs(candidate) else config_path.parent / candidate
                break
        return auth_path.read_text(encoding="utf-8").splitlines()[0].strip().lower().endswith("+pmp")
    except (OSError, IndexError):
        return False


def prepare_service(parent_pid: int) -> list[str]:
    """Patch RGS and verify Proton routing before the service starts."""
    proton_pmp = _proton_pmp_credentials_enabled()
    results = apply(include_updater=False)
    if proton_pmp:
        patch_failed = any(
            "not present" in result or "changed upstream" in result or "no changes applied" in result
            for result in results
        )
        if patch_failed:
            raise RuntimeError("RGS Proton-only discovery could not be patched safely")
        _ensure_tun_route()
        results.append("RGS VPN socket binding active; internet route uses tun0")
    else:
        results.append("Proton +pmp is not enabled; RGS socket guard is not needed")
    return results


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    action = args[0] if args else "apply"
    if action not in ("apply", "apply-helper", "prepare-service"):
        print("Usage: rgs_proton.py [apply|apply-helper|prepare-service PID]", file=sys.stderr)
        return 2
    try:
        if action == "prepare-service":
            if len(args) != 2 or not args[1].isdigit():
                print("prepare-service requires the RGS launcher PID", file=sys.stderr)
                return 2
            results = prepare_service(int(args[1]))
        else:
            results = apply(include_updater=(action == "apply"))
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        print(f"[drone-rgs-proton] ERROR: {error}", file=sys.stderr)
        return 1
    for result in results:
        print(f"[drone-rgs-proton] {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
