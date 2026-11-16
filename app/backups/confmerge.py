"""Secret removal and key-level merging for line-based ``key = value`` config files.

Backups never carry credentials: Wi-Fi keys, RetroAchievements/ScreenScraper/netplay
passwords and API tokens are removed from ``batocera.conf`` and from every ``.cfg`` /
``.conf`` / ``.ini`` / ``.properties`` file when an archive is built. Each removed
``(section, key)`` is recorded in the manifest so a restore can put this machine's own
value back in place.

``batocera.conf`` is never copied whole. A restore merges it key by key: secrets and
(unless the user opts in) hardware/network-specific keys keep this machine's values;
keys only present locally stay in overlay mode and are dropped in exact mode.

Pure text logic, stdlib only.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

SCRUBBABLE_SUFFIXES = frozenset({".cfg", ".conf", ".ini", ".properties"})

_SECTION = re.compile(r"^\s*\[([^\]\r\n]{1,200})\]\s*$")
_KEY_LINE = re.compile(r"^\s*([^#;\s=\[][^=]*?)\s*=(.*)$")
_EXACT_SECRET_NAMES = frozenset({"key", "pass", "passwd", "psk", "pin", "pwd"})
_SECRET_FRAGMENTS = ("password", "passphrase", "secret", "token", "apikey", "api_key", "api-key",
                     "privatekey", "private_key", "credential")
_KEY_QUALIFIERS = frozenset({"api", "auth", "private", "access", "secret", "license", "licence"})

# batocera.conf keys that describe this particular machine rather than a preference.
MACHINE_KEY_PREFIXES = (
    "system.hostname", "system.timezone", "system.ssh", "system.security",
    "wifi.", "wifi2.", "wifi3.", "network.",
    "global.videooutput", "global.videomode", "es.resolution", "display.",
    "audio.device", "audio.profile",
)

MAX_REPORTED_CHANGES = 500


def _normalized_key(key: str) -> str:
    return key.strip().strip('"').strip()


def is_secret_key(key: str) -> bool:
    lowered = _normalized_key(key).lower()
    last = re.split(r"[.\]]", lowered)[-1].strip('"[] ')
    if last in _EXACT_SECRET_NAMES:
        return True
    if any(fragment in last for fragment in _SECRET_FRAGMENTS):
        return True
    words = [word for word in re.split(r"[^a-z0-9]+", last) if word]
    return "key" in words and bool(_KEY_QUALIFIERS.intersection(words))


def is_machine_key(key: str) -> bool:
    lowered = _normalized_key(key).lower()
    return any(lowered == prefix.rstrip(".") or lowered.startswith(prefix) for prefix in MACHINE_KEY_PREFIXES)


def decode(data: bytes) -> str:
    return data.decode("utf-8", errors="surrogateescape")


def encode(text: str) -> bytes:
    return text.encode("utf-8", errors="surrogateescape")


def _join(lines: Sequence[str]) -> str:
    return "\n".join(lines) + ("\n" if lines else "")


def _entries(text: str) -> List[Tuple[Optional[str], Optional[str], str]]:
    """(section, key or None, raw line) for every line."""
    section: Optional[str] = None
    rows = []
    for line in text.splitlines():
        header = _SECTION.match(line)
        if header:
            section = header.group(1).strip()
            rows.append((section, None, line))
            continue
        match = _KEY_LINE.match(line)
        rows.append((section, _normalized_key(match.group(1)) if match else None, line))
    return rows


def scrub(text: str, suffix: str) -> Tuple[str, List[List[Optional[str]]]]:
    """Remove secret ``key = value`` lines; returns the text and the removed (section, key) pairs."""
    if suffix.lower() not in SCRUBBABLE_SUFFIXES:
        return text, []
    kept, redacted = [], []
    for section, key, line in _entries(text):
        if key is not None and is_secret_key(key):
            pair = [section, key]
            if pair not in redacted:
                redacted.append(pair)
            continue
        kept.append(line)
    if not redacted:
        return text, []
    return _join(kept), redacted


def restore_redacted(backup_text: str, local_text: Optional[str], redacted: Iterable[Sequence]) -> str:
    """Put this machine's own values for the keys a backup had removed back into its file."""
    wanted = {(pair[0], pair[1]) for pair in redacted if isinstance(pair, (list, tuple)) and len(pair) == 2}
    if not wanted or not local_text:
        return backup_text
    pending: Dict[Optional[str], List[str]] = {}
    for section, key, line in _entries(local_text):
        if key is not None and (section, key) in wanted:
            pending.setdefault(section, []).append(line)
    if not pending:
        return backup_text
    out: List[str] = []
    current: Optional[str] = None
    for section, key, line in _entries(backup_text):
        if key is None and _SECTION.match(line):
            out.extend(pending.pop(current, []))
            current = section
        out.append(line)
    out.extend(pending.pop(current, []))
    for section, lines in pending.items():
        if section is not None:
            out.append(f"[{section}]")
        out.extend(lines)
    return _join(out)


def parse_values(text: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for _section, key, line in _entries(text or ""):
        if key is not None:
            values[key] = line.split("=", 1)[1].strip()
    return values


def merge_batocera_conf(
    backup_text: str,
    local_text: Optional[str],
    *,
    exact: bool,
    include_machine_keys: bool,
) -> Tuple[str, dict]:
    """The ``batocera.conf`` a restore writes, and the per-key changes it makes."""
    local_text = local_text or ""

    def keep_local(key: str) -> bool:
        return is_secret_key(key) or (not include_machine_keys and is_machine_key(key))

    local_lines: Dict[str, str] = {}
    for _section, key, line in _entries(local_text):
        if key is not None:
            local_lines[key] = line
    out: List[str] = []
    seen = set()
    for _section, key, line in _entries(backup_text):
        if key is None:
            out.append(line)
            continue
        if key in seen:
            continue
        seen.add(key)
        if keep_local(key):
            if key in local_lines:
                out.append(local_lines[key])
            continue
        out.append(line)
    for key, line in local_lines.items():
        if key not in seen and (keep_local(key) or not exact):
            out.append(line)
    merged = _join(out)
    before, after = parse_values(local_text), parse_values(merged)
    changes = {
        "added": [{"key": key, "value": after[key]} for key in after if key not in before],
        "changed": [{"key": key, "old": before[key], "new": after[key]}
                    for key in after if key in before and before[key] != after[key]],
        "removed": [{"key": key, "old": before[key]} for key in before if key not in after],
    }
    for name, rows in changes.items():
        changes[name] = [row for row in rows if not is_secret_key(row["key"])][:MAX_REPORTED_CHANGES]
    return merged, changes
