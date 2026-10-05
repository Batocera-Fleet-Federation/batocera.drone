"""Administrator-authored custom scripts -- the only editable executable action.

Scripts live only in ``<integration>/scripts/<32-hex-id>.sh`` or ``.py``
(+ ``.json`` metadata). IDs are server-generated and re-validated on every
access, so no request can name a path. Bash is ``<id>.sh``; Python 3 is
``<id>.py``. Metadata records ``language`` (``bash`` or ``python3``). A legacy
``.sh`` whose metadata has no language is Bash.

Execution runs the saved file directly (its shebang picks the interpreter)
through ``ProcessRunner`` with ``shell=False``, a clean environment (Drone's
own environment -- which may hold tokens -- is not inherited), a timeout, and
bounded output. Python 3 is the Batocera system ``python3`` on that safe
``PATH``; this feature does not install packages or use the Stream Deck
tooling virtualenv. Built-ins and Launch Game never use this module.
"""

import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from .config import clean_text, hex_id, new_id
from .logs import log_event
from .paths import StreamDeckPaths, atomic_write_bytes, atomic_write_json, read_json
from .process import ProcessRunner


MAX_SCRIPT_BYTES = 64 * 1024
_SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Primary shebang is the starter. Extra shebangs stay valid for that language
# so existing Bash scripts that begin with #!/bin/sh keep running.
_LANGUAGES = {
    "bash": {
        "extension": ".sh",
        "shebangs": ("#!/bin/bash", "#!/bin/sh"),
    },
    "python3": {
        "extension": ".py",
        "shebangs": ("#!/usr/bin/env python3",),
    },
}
DEFAULT_SCRIPT = "#!/bin/bash\n# Runs as the Drone service on this Batocera machine.\necho \"Hello from Stream Deck\"\n"
DEFAULT_PYTHON_SCRIPT = (
    "#!/usr/bin/env python3\n"
    "# Runs as the Drone service on this Batocera machine.\n"
    "print(\"Hello from Stream Deck\")\n"
)


class ScriptInUseError(ValueError):
    def __init__(self, references: List[dict]) -> None:
        super().__init__(f"script is assigned to {len(references)} button(s); unassign it first")
        self.references = references


def _shebang_line(code: str) -> str:
    return code.split("\n", 1)[0].strip()


def _language_for_shebang(line: str) -> Optional[str]:
    for language, spec in _LANGUAGES.items():
        if line in spec["shebangs"]:
            return language
    return None


class ScriptStore:
    def __init__(self, paths: StreamDeckPaths, runner: Optional[ProcessRunner] = None) -> None:
        self.paths = paths
        self.runner = runner or ProcessRunner()
        self._lock = threading.Lock()

    def _meta_path(self, script_id: str) -> Path:
        return self.paths.owned("scripts", f"{script_id}.json")

    def _candidate_paths(self, script_id: str) -> Dict[str, Path]:
        return {
            language: self.paths.owned("scripts", f"{script_id}{spec['extension']}")
            for language, spec in _LANGUAGES.items()
        }

    def _resolve(self, script_id: str, *, require_meta: bool):
        """Return ``(id, path, meta_path, meta, language)`` or ``None`` when missing.

        ``require_meta`` matches ``get`` (metadata and file both required).
        ``run`` still executes a directly owned file whose metadata is absent.
        A declared language only resolves that extension, so a script cannot be
        executed under the other interpreter. With no language field, ``.sh``
        is Bash and wins over a stray ``.py``.
        """
        script_id = hex_id(script_id, "script id")
        meta_path = self._meta_path(script_id)
        candidates = self._candidate_paths(script_id)
        meta = read_json(meta_path, None)
        if not isinstance(meta, dict):
            if require_meta:
                raise KeyError("script not found")
            meta = {}
        declared = meta.get("language") if isinstance(meta, dict) else None
        if declared not in _LANGUAGES:
            declared = None
        search = [declared] if declared else list(_LANGUAGES)
        for language in search:
            path = candidates[language]
            if path.is_file() and not path.is_symlink():
                return script_id, path, meta_path, meta, declared or language
        if require_meta:
            raise KeyError("script not found")
        return None

    @staticmethod
    def _validate_payload(payload: dict, current: Optional[dict] = None) -> dict:
        current = current or {}
        name = clean_text(payload.get("name", current.get("name")), 80)
        if not name:
            raise ValueError("script name is required")
        code = payload.get("code", current.get("code"))
        if not isinstance(code, str):
            raise ValueError("script code is required")
        code = code.replace("\r\n", "\n").replace("\r", "\n")
        if "\x00" in code:
            raise ValueError("script code must be text")
        if not code.startswith("#!"):
            raise ValueError("script must start with an interpreter line, e.g. #!/bin/bash or #!/usr/bin/env python3")
        if len(code.encode("utf-8")) > MAX_SCRIPT_BYTES:
            raise ValueError("script is larger than 64 KiB")
        if not code.endswith("\n"):
            code += "\n"
        shebang = _shebang_line(code)
        inferred = _language_for_shebang(shebang)
        if "language" in payload and payload.get("language") is not None:
            language = payload.get("language")
        elif current.get("language") in _LANGUAGES:
            language = current.get("language")
        else:
            language = inferred
        if not isinstance(language, str) or language not in _LANGUAGES:
            raise ValueError("script language must be bash or python3")
        if inferred != language:
            expected = _LANGUAGES[language]["shebangs"][0]
            if inferred is None:
                raise ValueError(
                    f"unsupported script interpreter for {language}; use {expected}"
                )
            raise ValueError(
                f"script interpreter does not match language {language}; use {expected}"
            )
        return {
            "name": name,
            "description": clean_text(payload.get("description", current.get("description", "")), 300),
            "code": code,
            "language": language,
        }

    def _write(self, script_id: str, values: dict, created_at: Optional[str] = None) -> dict:
        language = values["language"]
        script_path = self._candidate_paths(script_id)[language]
        meta_path = self._meta_path(script_id)
        self.paths.ensure(self.paths.scripts_dir)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # Write the new file before dropping the previous extension so a crash
        # cannot leave the script with neither body.
        atomic_write_bytes(script_path, values["code"].encode("utf-8"), mode=0o700)
        meta = {
            "id": script_id,
            "name": values["name"],
            "description": values["description"],
            "language": language,
            "created_at": created_at or now,
            "updated_at": now,
        }
        atomic_write_json(meta_path, meta)
        for other, path in self._candidate_paths(script_id).items():
            if other != language and path.is_file() and not path.is_symlink():
                path.unlink()
        return {**meta, "code": values["code"], "size": len(values["code"].encode("utf-8"))}

    def list(self) -> List[dict]:
        rows = []
        try:
            entries = sorted(self.paths.scripts_dir.glob("*.json"))
        except OSError:
            return []
        for meta_path in entries:
            try:
                row = self.get(meta_path.stem, include_code=False)
            except (KeyError, ValueError):
                continue
            rows.append(row)
        rows.sort(key=lambda row: row["name"].lower())
        return rows

    def get(self, script_id: str, *, include_code: bool = True) -> dict:
        script_id, script_path, _meta_path, meta, language = self._resolve(script_id, require_meta=True)
        row = {
            "id": script_id,
            "name": clean_text(meta.get("name"), 80) or "Script",
            "description": clean_text(meta.get("description"), 300),
            "language": language,
            "created_at": meta.get("created_at"),
            "updated_at": meta.get("updated_at"),
            "size": script_path.stat().st_size,
        }
        if include_code:
            row["code"] = script_path.read_text(encoding="utf-8", errors="replace")
        return row

    def create(self, payload: dict) -> dict:
        values = self._validate_payload(payload or {})
        with self._lock:
            return self._write(new_id(), values)

    def update(self, script_id: str, payload: dict) -> dict:
        with self._lock:
            current = self.get(script_id)
            values = self._validate_payload(payload or {}, current)
            return self._write(current["id"], values, created_at=current.get("created_at"))

    def duplicate(self, script_id: str) -> dict:
        with self._lock:
            current = self.get(script_id)
            values = self._validate_payload({"name": f"Copy of {current['name']}"[:80]}, current)
            return self._write(new_id(), values)

    def delete(self, script_id: str, references: Optional[List[dict]] = None) -> None:
        if references:
            raise ScriptInUseError(references)
        with self._lock:
            script_id = hex_id(script_id, "script id")
            meta_path = self._meta_path(script_id)
            candidates = list(self._candidate_paths(script_id).values())
            if not meta_path.exists() and not any(path.exists() for path in candidates):
                raise KeyError("script not found")
            for path in (*candidates, meta_path):
                if path.is_symlink() or path.is_file():
                    path.unlink()

    def environment(self, extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        env = {
            "PATH": _SAFE_PATH,
            "HOME": os.environ.get("HOME") or "/userdata/system",
            "LANG": os.environ.get("LANG") or "C.UTF-8",
            "DRONE_STREAMDECK_ROOT": str(self.paths.root),
        }
        for key, value in (extra or {}).items():
            if key.startswith("DRONE_STREAMDECK_") and value is not None:
                env[key] = "".join(ch for ch in str(value) if ch.isprintable())[:200]
        return env

    def run(self, script_id: str, *, timeout: float = 30.0, context: Optional[Mapping[str, str]] = None,
            cancel_event: Optional[threading.Event] = None, requested_by: str = "") -> dict:
        try:
            resolved = self._resolve(script_id, require_meta=False)
        except KeyError:
            resolved = None
        if resolved is None:
            return {"status": "failed", "error": "Script not found. Assign another script to this button.",
                    "exit_code": None, "stdout": "", "stderr": "", "duration_seconds": 0}
        script_id, script_path, _meta_path, meta, _language = resolved
        os.chmod(script_path, 0o700)
        result = self.runner.run(str(script_path), (), timeout=min(300.0, max(1.0, float(timeout))),
                                 cwd=self.paths.scripts_dir, env=self.environment(context),
                                 cancel_event=cancel_event)
        if result.start_error:
            status = "failed"
        elif result.timed_out:
            status = "timed-out"
        elif result.cancelled:
            status = "cancelled"
        else:
            status = "completed" if result.exit_code == 0 else "failed"
        log_event("custom-script", script_id=script_id, name=meta.get("name"), status=status,
                  exit_code=result.exit_code, duration=result.duration_seconds, requested_by=requested_by,
                  trigger=(context or {}).get("DRONE_STREAMDECK_TRIGGER"))
        payload = result.to_dict()
        payload["status"] = status
        if result.start_error:
            payload["error"] = result.start_error
        elif status != "completed":
            payload["error"] = f"Script {status}: {result.summary()}"
        return payload
