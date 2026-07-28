"""Fail-closed resolver management for the Drone-managed OpenVPN tunnel."""

from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from typing import Iterable, List, Optional


VPN_RESOLVER_BACKUP_FILENAME = "resolv.conf.before-vpn"


def resolver_path() -> Path:
    return Path(os.environ.get("DRONE_RESOLV_CONF", "/etc/resolv.conf")).resolve()


def backup_path(vpn_directory: Path) -> Path:
    return vpn_directory / VPN_RESOLVER_BACKUP_FILENAME


def _atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.drone-tmp")
    temporary.write_text(text, encoding="utf-8")
    try:
        temporary.chmod(mode)
    except OSError:
        pass
    temporary.replace(path)


def prepare(vpn_directory: Path, *, target: Optional[Path] = None) -> None:
    """Snapshot the pre-VPN resolver exactly once for later restoration."""
    target = target or resolver_path()
    saved = backup_path(vpn_directory)
    if saved.exists():
        return
    try:
        original = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        original = ""
    vpn_directory.mkdir(parents=True, exist_ok=True)
    _atomic_write(saved, original, 0o600)


def allow_bootstrap_dns(vpn_directory: Path, *, target: Optional[Path] = None) -> bool:
    """Temporarily restore pre-VPN DNS so OpenVPN can resolve its endpoint."""
    target = target or resolver_path()
    saved = backup_path(vpn_directory)
    try:
        original = saved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    _atomic_write(target, original)
    return True


def restore(vpn_directory: Path, *, target: Optional[Path] = None) -> bool:
    target = target or resolver_path()
    saved = backup_path(vpn_directory)
    try:
        original = saved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    _atomic_write(target, original)
    saved.unlink(missing_ok=True)
    return True


def sinkhole(*, target: Optional[Path] = None) -> None:
    target = target or resolver_path()
    _atomic_write(
        target,
        "# Managed by Batocera Drone: VPN DNS is fail-closed while the tunnel is unavailable.\n"
        "nameserver 127.0.0.1\n"
        "nameserver ::1\n"
        "options timeout:1 attempts:1\n",
    )


def apply(servers: Iterable[str], *, target: Optional[Path] = None) -> List[str]:
    target = target or resolver_path()
    normalized: List[str] = []
    for raw in servers:
        value = str(raw or "").strip().strip("[]")
        try:
            parsed = str(ipaddress.ip_address(value))
        except ValueError:
            continue
        if parsed not in normalized:
            normalized.append(parsed)
    if not normalized:
        raise ValueError("VPN provider did not supply a usable DNS server")
    lines = ["# Managed by Batocera Drone: DNS must traverse the active VPN tunnel."]
    lines.extend(f"nameserver {server}" for server in normalized)
    lines.append("options timeout:2 attempts:2")
    _atomic_write(target, "\n".join(lines) + "\n")
    return normalized


def provider_dns_servers(config_text: str, log_text: str = "") -> List[str]:
    """Extract provider-pushed/static ``dhcp-option DNS`` addresses."""
    configured = str(os.environ.get("DRONE_VPN_DNS") or "").strip()
    candidates = re.split(r"[,;\s]+", configured) if configured else []
    pattern = re.compile(r"(?:dhcp-option\s+DNS|dhcp-option\s+DNS6)[ ='\"]+([0-9A-Fa-f:.]+)", re.IGNORECASE)
    candidates.extend(match.group(1) for match in pattern.finditer(f"{config_text}\n{log_text}"))
    result: List[str] = []
    for candidate in candidates:
        try:
            normalized = str(ipaddress.ip_address(candidate.strip().strip("[]")))
        except ValueError:
            continue
        if normalized not in result:
            result.append(normalized)
    return result
