"""Temporary, journaled emulator configuration changes.

Multiplayer needs a handful of keys in files the Drone does not own
(``batocera.conf``, ``ppsspp.ini``, ...). This layer changes *only* those keys,
records each prior value in a write-ahead journal (persisted in the Drone state
DB) before touching the file, writes atomically, and puts the originals back
when the session ends -- or on the next start if the Drone died mid-session.
A key the user changed after we set it is left alone on restore.

Targets are confined to the userdata tree, symlinks are refused, and keys and
values are validated so a peer-influenced string can never add lines.
"""

import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import models as m
from .persistence import Document, lock_for

_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f]")
_KV_KEY = re.compile(r'^[A-Za-z0-9_.\-]+(\["[^"\\\x00-\x1f=]{1,255}"\])?(\.[A-Za-z0-9_.\-]+)*$')
_INI_KEY = re.compile(r"^[A-Za-z0-9_.\- ]{1,64}$")
_INI_SECTION = re.compile(r"^[A-Za-z0-9_.\- ]{1,64}$")
MAX_CONFIG_BYTES = 4 * 1024 * 1024


def scoped_key(system: str, rom_name: str, key: str) -> str:
    """``system["rom"].key`` -- a batocera.conf key that applies to one game only."""
    system = m.clean_system(system)
    if not rom_name or '"' in rom_name or "\\" in rom_name or "=" in rom_name or _FORBIDDEN.search(rom_name):
        raise m.MultiplayerError("invalid_game", "The ROM file name cannot be used in a Batocera setting key")
    return f'{system}["{rom_name}"].{key}'


def _check_value(value: str) -> str:
    text = str(value)
    if _FORBIDDEN.search(text) or len(text) > 512:
        raise m.MultiplayerError("invalid_config_value", "Configuration values must be single-line text")
    return text


class ConfigGuard:
    def __init__(self, settings: Any, *, backup_dir: Optional[Path] = None) -> None:
        self.settings = settings
        self._journal = Document(settings, "config_journal", dict)
        self.userdata_root = Path(os.path.abspath(str(settings.userdata_root)))
        self.backup_dir = Path(backup_dir) if backup_dir else self.userdata_root / "system" / "drone-app" / "multiplayer-backups"

    # ------------------------------------------------------------------ safety
    def _confine(self, path: Path) -> Path:
        absolute = Path(os.path.abspath(str(path)))
        for candidate in (absolute, *absolute.parents):
            if candidate == self.userdata_root:
                break
            if candidate.is_symlink():
                raise m.MultiplayerError("unsafe_config_path", "Refusing to edit a configuration reached through a symlink")
        try:
            absolute.relative_to(self.userdata_root)
        except ValueError:
            raise m.MultiplayerError("unsafe_config_path", "Configuration files must live under the Batocera userdata tree") from None
        return absolute

    @staticmethod
    def _read(path: Path) -> Optional[str]:
        try:
            if path.stat().st_size > MAX_CONFIG_BYTES:
                raise m.MultiplayerError("config_too_large", f"{path.name} is unexpectedly large; not editing it")
            return path.read_text(encoding="utf-8", errors="surrogateescape")
        except FileNotFoundError:
            return None

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        mode = 0o644
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            pass
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    # ----------------------------------------------------------------- editing
    @staticmethod
    def _kv_find(lines: List[str], key: str) -> Optional[int]:
        found = None
        for index, line in enumerate(lines):
            if line.lstrip().startswith(("#", ";")):
                continue
            name, separator, _ = line.partition("=")
            if separator and name.strip() == key:
                found = index
        return found

    @staticmethod
    def _kv_value(line: str) -> str:
        return line.partition("=")[2].strip()

    def _kv_get(self, text: str, edit: dict) -> Optional[str]:
        lines = text.splitlines()
        index = self._kv_find(lines, edit["key"])
        return None if index is None else self._kv_value(lines[index])

    def _kv_set(self, text: str, edit: dict, value: Optional[str]) -> str:
        lines = text.splitlines()
        index = self._kv_find(lines, edit["key"])
        if value is None:
            if index is not None:
                del lines[index]
        elif index is None:
            lines.append(f"{edit['key']}={value}")
        else:
            lines[index] = f"{edit['key']}={value}"
        return "\n".join(lines) + ("\n" if lines else "")

    @staticmethod
    def _ini_span(lines: List[str], section: str):
        start = end = None
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                if start is not None:
                    end = index
                    break
                if stripped[1:-1].strip().lower() == section.lower():
                    start = index
        if start is None:
            return None
        return start, end if end is not None else len(lines)

    def _ini_find(self, lines: List[str], edit: dict) -> Optional[int]:
        span = self._ini_span(lines, edit["section"])
        if span is None:
            return None
        found = None
        for index in range(span[0] + 1, span[1]):
            name, separator, _ = lines[index].partition("=")
            if separator and not name.lstrip().startswith(("#", ";")) and name.strip().lower() == edit["key"].lower():
                found = index
        return found

    def _ini_get(self, text: str, edit: dict) -> Optional[str]:
        lines = text.splitlines()
        index = self._ini_find(lines, edit)
        return None if index is None else self._kv_value(lines[index])

    def _ini_set(self, text: str, edit: dict, value: Optional[str]) -> str:
        lines = text.splitlines()
        span = self._ini_span(lines, edit["section"])
        if span is None:
            raise m.MultiplayerError("config_section_missing", f"[{edit['section']}] is not present in the emulator configuration")
        index = self._ini_find(lines, edit)
        if value is None:
            if index is not None:
                del lines[index]
        elif index is None:
            lines.insert(span[1], f"{edit['key']} = {value}")
        else:
            lines[index] = f"{lines[index].partition('=')[0].rstrip()} = {value}"
        return "\n".join(lines) + ("\n" if lines else "")

    def _get(self, kind: str, text: str, edit: dict) -> Optional[str]:
        return self._kv_get(text, edit) if kind == "kv" else self._ini_get(text, edit)

    def _set(self, kind: str, text: str, edit: dict, value: Optional[str]) -> str:
        return self._kv_set(text, edit, value) if kind == "kv" else self._ini_set(text, edit, value)

    @staticmethod
    def _validate_edit(kind: str, edit: dict) -> dict:
        key = str(edit.get("key") or "")
        if kind == "kv":
            if not _KV_KEY.fullmatch(key):
                raise m.MultiplayerError("invalid_config_key", "Not a valid Batocera setting key")
            return {"key": key, "section": "", "value": _check_value(edit.get("value"))}
        section = str(edit.get("section") or "")
        if not _INI_KEY.fullmatch(key) or not _INI_SECTION.fullmatch(section):
            raise m.MultiplayerError("invalid_config_key", "Not a valid INI section or key")
        return {"key": key, "section": section, "value": _check_value(edit.get("value"))}

    def read_value(self, path: Path, kind: str, key: str, section: str = "") -> Optional[str]:
        text = self._read(self._confine(path))
        return None if text is None else self._get(kind, text, {"key": key, "section": section})

    def apply(self, session_id: str, path: Path, kind: str, edits: List[dict]) -> dict:
        # All sessions share emulator files. Serialise the entire journal/write/verify transaction.
        with lock_for(self.settings, "config-files"):
            return self._apply(session_id, path, kind, edits)

    def watch_generated(self, session_id: str, path: Path, kind: str, edits: List[dict]) -> dict:
        """Journal expected generated values before configgen runs, without writing its file.

        The target may not exist yet. Cleanup removes only values matching this
        session's expected values; unrelated generated/user settings stay intact.
        """
        with lock_for(self.settings, "config-files"):
            return self._apply(session_id, path, kind, edits, watch=True)

    def _apply(self, session_id: str, path: Path, kind: str, edits: List[dict], *, watch: bool = False) -> dict:
        """Journal, back up and apply ``edits`` to one file; verify; roll back on any failure."""
        if kind not in ("kv", "ini"):
            raise ValueError("kind must be kv or ini")
        session_id = m.clean_session_id(session_id)
        target = self._confine(path)
        clean_edits = [self._validate_edit(kind, edit) for edit in edits]
        wanted = {(item["section"], item["key"]) for item in clean_edits}
        for owner, entry in self._journal.load().items():
            spec = entry.get("files", {}).get(str(target))
            if owner != session_id and spec and wanted.intersection((item["section"], item["key"]) for item in spec["edits"]):
                raise m.MultiplayerError("config_in_use", "Another multiplayer session owns these emulator settings. End it first.", 409)
        text = self._read(target)
        existed = text is not None
        if text is None:
            if not watch:
                raise m.MultiplayerError("config_missing", f"{target.name} does not exist; cannot prepare multiplayer settings", 409)
            text = ""
        records = []
        for edit in clean_edits:
            records.append({**edit, "original": self._get(kind, text, edit), "applied": edit["value"]})
        backup = str(self._backup(session_id, target, text)) if existed else ""

        def journal(store: dict) -> None:
            entry = store.setdefault(session_id, {"session_id": session_id, "files": {}, "created_at": m.now_iso()})
            existing = entry["files"].get(str(target))
            if existing:
                # A second apply to the same file keeps the first originals (the user's real values) and only
                # moves the value we expect to find at restore time.
                known = {(item["section"], item["key"]): item for item in existing["edits"]}
                for item in records:
                    previous = known.get((item["section"], item["key"]))
                    if previous is None:
                        existing["edits"].append(item)
                    else:
                        previous["applied"] = item["applied"]
            else:
                entry["files"][str(target)] = {"kind": kind, "backup": str(backup), "edits": records}
        self._journal.update(journal)  # write-ahead: recoverable even if the file write dies half way
        if watch:
            return {"file": target.name, "keys": [edit["key"] for edit in clean_edits], "watching": True}
        try:
            updated = text
            for edit in clean_edits:
                updated = self._set(kind, updated, edit, edit["value"])
            self._atomic_write(target, updated)
            check = self._read(target) or ""
            for edit in clean_edits:
                if self._get(kind, check, edit) != edit["value"]:
                    raise m.MultiplayerError("config_verify_failed", f"{target.name} did not accept the multiplayer settings", 500)
        except Exception:
            self.restore(session_id)
            raise
        return {"file": target.name, "keys": [edit["key"] for edit in clean_edits]}

    def _backup(self, session_id: str, target: Path, text: str) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = self.backup_dir / f"{session_id}-{target.name}.bak"
        if not backup.exists():
            backup.write_text(text, encoding="utf-8", errors="surrogateescape")
            os.chmod(backup, 0o600)
        return backup

    def pending(self, session_id: Optional[str] = None) -> Dict[str, dict]:
        journal = self._journal.load()
        return {key: value for key, value in journal.items() if session_id in (None, key)}

    def restore(self, session_id: str) -> dict:
        with lock_for(self.settings, "config-files"):
            return self._restore(session_id)

    def _restore(self, session_id: str) -> dict:
        """Put originals back. Returns ``{"restored": n, "left_alone": n, "errors": [...]}``; keeps the journal on error."""
        session_id = m.clean_session_id(session_id)
        entry = self._journal.load().get(session_id)
        result = {"restored": 0, "left_alone": 0, "errors": []}
        if not entry:
            return result
        for path_text, spec in list(entry["files"].items()):
            target, kind = Path(path_text), spec["kind"]
            try:
                target = self._confine(target)
                text = self._read(target)
                if text is None:
                    result["left_alone"] += len(spec["edits"])
                    backup = Path(spec.get("backup") or "")
                    if backup.name and backup.parent == self.backup_dir:
                        backup.unlink(missing_ok=True)
                    continue
                updated = text
                for edit in spec["edits"]:
                    current = self._get(kind, updated, edit)
                    if current != edit["applied"]:
                        result["left_alone"] += 1  # the user (or Batocera) changed it since: not ours to revert
                        continue
                    updated = self._set(kind, updated, edit, edit["original"])
                    result["restored"] += 1
                if updated != text:
                    self._atomic_write(target, updated)
                backup = Path(spec.get("backup") or "")
                if backup.name and backup.parent == self.backup_dir:
                    backup.unlink(missing_ok=True)
            except (OSError, m.MultiplayerError) as error:
                result["errors"].append(f"{Path(path_text).name}: {getattr(error, 'message', None) or error}")
        if not result["errors"]:
            self._journal.update(lambda store: store.pop(session_id, None))
        return result
