"""Administrator-authored custom scripts -- the only editable executable action.

Scripts live only in ``<integration>/scripts/<32-hex-id>.sh`` (+ ``.json``
metadata). IDs are server-generated and re-validated on every access, so no
request can name a path. Execution runs the saved file directly (its shebang
picks the interpreter) through ``ProcessRunner`` with ``shell=False``, a clean
environment (Drone's own environment -- which may hold tokens -- is not
inherited), a timeout, and bounded output. Built-ins and Launch Game never use
this module.
"""

import json
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
DEFAULT_SCRIPT = "#!/bin/bash\n# Runs as the Drone service on this Batocera machine.\necho \"Hello from Stream Deck\"\n"
_SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class ScriptInUseError(ValueError):
    def __init__(self, references: List[dict]) -> None:
        super().__init__(f"script is assigned to {len(references)} button(s); unassign it first")
        self.references = references


class ScriptStore:
    def __init__(self, paths: StreamDeckPaths, runner: Optional[ProcessRunner] = None) -> None:
        self.paths = paths
        self.runner = runner or ProcessRunner()
        self._lock = threading.Lock()

    def _files(self, script_id: str) -> tuple:
        script_id = hex_id(script_id, "script id")
        return (self.paths.owned("scripts", f"{script_id}.sh"),
                self.paths.owned("scripts", f"{script_id}.json"))

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
            raise ValueError("script must start with an interpreter line, e.g. #!/bin/bash")
        if len(code.encode("utf-8")) > MAX_SCRIPT_BYTES:
            raise ValueError("script is larger than 64 KiB")
        if not code.endswith("\n"):
            code += "\n"
        return {
            "name": name,
            "description": clean_text(payload.get("description", current.get("description", "")), 300),
            "code": code,
        }

    def _write(self, script_id: str, values: dict, created_at: Optional[str] = None) -> dict:
        script_path, meta_path = self._files(script_id)
        self.paths.ensure(self.paths.scripts_dir)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        atomic_write_bytes(script_path, values["code"].encode("utf-8"), mode=0o700)
        meta = {"id": script_id, "name": values["name"], "description": values["description"],
                "created_at": created_at or now, "updated_at": now}
        atomic_write_json(meta_path, meta)
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
        script_path, meta_path = self._files(script_id)
        meta = read_json(meta_path, None)
        if not isinstance(meta, dict) or not script_path.is_file() or script_path.is_symlink():
            raise KeyError("script not found")
        row = {
            "id": hex_id(script_id),
            "name": clean_text(meta.get("name"), 80) or "Script",
            "description": clean_text(meta.get("description"), 300),
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
            script_path, meta_path = self._files(script_id)
            if not script_path.exists() and not meta_path.exists():
                raise KeyError("script not found")
            for path in (script_path, meta_path):
                path.unlink(missing_ok=True)

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
        script_path, _meta_path = self._files(script_id)
        if not script_path.is_file() or script_path.is_symlink():
            return {"status": "failed", "error": "Script not found. Assign another script to this button.",
                    "exit_code": None, "stdout": "", "stderr": "", "duration_seconds": 0}
        meta = read_json(_meta_path, {}) or {}
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
        log_event("custom-script", script_id=hex_id(script_id), name=meta.get("name"), status=status,
                  exit_code=result.exit_code, duration=result.duration_seconds, requested_by=requested_by,
                  trigger=(context or {}).get("DRONE_STREAMDECK_TRIGGER"))
        payload = result.to_dict()
        payload["status"] = status
        if result.start_error:
            payload["error"] = result.start_error
        elif status != "completed":
            payload["error"] = f"Script {status}: {result.summary()}"
        return payload
