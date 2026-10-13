"""Press-time dispatcher: ``python3 -m app.integrations.media_keys.dispatch <binding-id>``.

hotkeygen runs this (through the generated ``drone-media-key`` launcher) once
per key event. It is deliberately independent of the Drone web service: it
reads ``config/media-keys.json`` straight from disk, so a restarting Drone can
never hang or lose a key press, and every outcome is logged and recorded in
``state/last-press.json`` for the admin page.

* disabled integration, unknown binding, or "none" -> does nothing;
* reboot/shutdown (and any binding with ``hold_seconds``) only fire after a
  typematic repeat *train* has lasted the hold duration -- a single press,
  a hold released early, or a series of separate taps does nothing;
* built-ins go through the shared ``BatoceraControl`` catalog; scripts go
  through the shared ``ScriptStore`` (``shell=False``, clean environment,
  timeout, bounded output). When started as root, scripts drop to the owner of
  the integration directory (the Drone service user) before running.
"""

import json
import os
import math
import sys
import time
from typing import Any, Callable, Optional

from .config import ConfigStore, MANAGED_KEYS, hold_identity, hold_seconds, requires_hold
from .paths import MediaKeysPaths

try:
    from ..shared.batocera_control import BUILTIN_COMMANDS, BatoceraControl
    from ..shared.files import atomic_write_json, read_json
    from ..shared.process import ProcessRunner
    from ..shared.scripts import ScriptStore
    from ..streamdeck.logs import format_event
    from ...common.logging_setup import _drone_log
except ImportError:  # pragma: no cover - flat execution
    from integrations.shared.batocera_control import BUILTIN_COMMANDS, BatoceraControl  # type: ignore
    from integrations.shared.files import atomic_write_json, read_json  # type: ignore
    from integrations.shared.process import ProcessRunner  # type: ignore
    from integrations.shared.scripts import ScriptStore  # type: ignore
    from integrations.streamdeck.logs import format_event  # type: ignore
    from common.logging_setup import _drone_log  # type: ignore

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX development hosts
    fcntl = None  # type: ignore


SCRIPT_TIMEOUT_SECONDS = 30.0
# Physical key (hotkeygen): Linux typematic is one delayed repeat (commonly
# 250-600 ms) then a rapid train (~25-40 Hz). Discrete taps -- even 450 ms
# apart, faster than a naive "repeat gap" -- never produce that train, so the
# post-delay gap must be tighter than a human tap. The admin Hold-to-test
# control is a different origin: the UI posts ~350 ms pulses over HTTP, so it
# uses a looser pulse window rather than typematic timing.
HOLD_FIRST_GAP_SECONDS = 0.75
HOLD_REPEAT_GAP_SECONDS = 0.20
TEST_HOLD_GAP_SECONDS = 1.0
HOLD_ORIGINS = ("key", "test")


def log_media_key(event: str, **fields: Any) -> None:
    line = format_event(event, **fields).replace("[streamdeck]", "[media-keys]", 1)
    try:
        _drone_log(line, also_stdout=False)
    except Exception:  # noqa: BLE001 - logging must never break a key press
        pass


def script_owner(paths: MediaKeysPaths) -> Optional[tuple]:
    """``(uid, gid)`` scripts must drop to when this process is root, else ``None``."""
    try:
        if os.geteuid() != 0:
            return None
        info = os.stat(paths.root)
    except (AttributeError, OSError):
        return None
    return (info.st_uid, info.st_gid) if info.st_uid != 0 else None


class HoldGate:
    """Tracks a key's repeat events; passes once held long enough, once per hold.

    ``origin="key"`` (hotkeygen dispatcher) requires a typematic train: one
    delayed first repeat, then events at repeat rate. Separate taps cannot
    accumulate even when they arrive faster than the first-repeat delay.
    ``origin="test"`` (admin Hold-to-test pulses) uses a wider HTTP pulse
    window. The two origins never share progress.
    """

    def __init__(self, paths: MediaKeysPaths, clock: Callable[[], float] = time.time) -> None:
        self.paths = paths
        self.clock = clock

    def _locked(self):
        class _Lock:
            def __enter__(inner):
                inner.fd = None
                if fcntl is not None:
                    self.paths.ensure(self.paths.state_dir)
                    inner.fd = os.open(str(self.paths.state_dir / "hold.lock"),
                                       os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
                    fcntl.flock(inner.fd, fcntl.LOCK_EX)

            def __exit__(inner, *_exc):
                if inner.fd is not None:
                    os.close(inner.fd)
        return _Lock()

    @staticmethod
    def _max_gap(origin: str, repeats: int) -> float:
        if origin == "test":
            return TEST_HOLD_GAP_SECONDS
        return HOLD_FIRST_GAP_SECONDS if repeats == 0 else HOLD_REPEAT_GAP_SECONDS

    def check(self, binding_id: str, seconds: float, identity: Optional[dict] = None,
              *, origin: str = "key") -> str:
        """Return ``"fire"``, ``"holding"`` (still counting) or ``"fired"`` (already acted).

        ``identity`` (see ``hold_identity``) must match the stored hold, otherwise
        the hold restarts from zero. ``origin`` must also match.
        """
        origin = origin if origin in HOLD_ORIGINS else "key"
        now = self.clock()
        with self._locked():
            state = read_json(self.paths.hold_file, {}) or {}
            entry = state.get(binding_id) if isinstance(state, dict) else None
            def timestamp(value: Any) -> Optional[float]:
                if isinstance(value, bool):
                    return None
                try:
                    parsed = float(value)
                except (TypeError, ValueError, OverflowError):
                    return None
                return parsed if math.isfinite(parsed) else None

            last = timestamp(entry.get("last")) if isinstance(entry, dict) else None
            started = timestamp(entry.get("start")) if isinstance(entry, dict) else None
            # Corrupt persisted data starts a fresh window. Never let malformed,
            # infinite, or future timestamps satisfy a dangerous hold.
            repeats = entry.get("repeats") if isinstance(entry, dict) else None
            intact = (
                isinstance(entry, dict)
                and last is not None and started is not None
                and last <= now and started <= now and started <= last
                and isinstance(entry.get("fired"), bool)
                and isinstance(repeats, int) and not isinstance(repeats, bool) and repeats >= 0
                and entry.get("identity") == identity
                and entry.get("origin") == origin
            )
            if not intact or now - last > self._max_gap(origin, repeats):
                entry = {"start": now, "last": now, "fired": False, "repeats": 0,
                         "identity": identity, "origin": origin}
                outcome = "holding"
            else:
                entry["last"] = now
                entry["repeats"] = repeats + 1
                if entry.get("fired"):
                    outcome = "fired"
                elif now - started >= seconds:
                    entry["fired"] = True
                    outcome = "fire"
                else:
                    outcome = "holding"
            state = state if isinstance(state, dict) else {}
            state[binding_id] = entry
            self.paths.ensure(self.paths.state_dir)
            atomic_write_json(self.paths.hold_file, state)
        return outcome


def record_press(paths: MediaKeysPaths, binding_id: str, result: dict, clock: Callable[[], float] = time.time) -> None:
    try:
        paths.ensure(paths.state_dir)
        data = read_json(paths.press_file, {}) or {}
        data = data if isinstance(data, dict) else {}
        data[binding_id] = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock())),
            "status": str(result.get("status") or "unknown")[:40],
            "error": str(result.get("error") or "")[:300],
        }
        atomic_write_json(paths.press_file, data)
    except Exception:  # noqa: BLE001 - recording is best effort
        pass


def execute_binding(binding: dict, paths: MediaKeysPaths, *, control: Optional[BatoceraControl] = None,
                    scripts: Optional[ScriptStore] = None, requested_by: str = "key",
                    run_as: Optional[tuple] = None) -> dict:
    """Run one validated binding's action (no hold gating, no enabled check)."""
    kind = binding.get("action_type")
    if kind == "builtin":
        control = control or BatoceraControl(log=log_media_key)
        return control.run_builtin(str(binding.get("action_id")))
    if kind == "script":
        scripts = scripts or media_keys_script_store(paths)
        result = scripts.run(str(binding.get("script_id")), timeout=SCRIPT_TIMEOUT_SECONDS,
                             context={"DRONE_MEDIA_KEYS_TRIGGER": requested_by}, requested_by=requested_by,
                             run_as=run_as)
        status = "ok" if result.get("status") == "completed" else "error"
        return {"status": status, "error": result.get("error", ""), "exit_code": result.get("exit_code")}
    return {"status": "skipped"}


def media_keys_script_store(paths: MediaKeysPaths, runner: Optional[ProcessRunner] = None) -> ScriptStore:
    return ScriptStore(paths, runner, env_prefix="DRONE_MEDIA_KEYS_", log=log_media_key,
                       target_noun="key binding")


def handle_press(binding_id: str, paths: Optional[MediaKeysPaths] = None, *,
                 control: Optional[BatoceraControl] = None, scripts: Optional[ScriptStore] = None,
                 clock: Callable[[], float] = time.time, run_as: Optional[tuple] = None,
                 record: bool = True) -> dict:
    paths = paths or MediaKeysPaths.default()
    if binding_id not in MANAGED_KEYS:
        log_media_key("press-rejected", binding=str(binding_id)[:40])
        return {"status": "unknown-binding"}
    config = ConfigStore(paths).load()
    if not config["enabled"]:
        return {"status": "disabled"}
    binding = config["bindings"].get(binding_id) or {"action_type": "none"}
    if binding.get("action_type") == "none":
        return {"status": "skipped"}
    if requires_hold(binding):
        gate = HoldGate(paths, clock).check(binding_id, hold_seconds(binding), hold_identity(config, binding))
        if gate != "fire":
            return {"status": gate}
    if binding.get("action_type") == "script" and run_as is None:
        run_as = script_owner(paths)
    try:
        result = execute_binding(binding, paths, control=control, scripts=scripts, run_as=run_as)
    except Exception as error:  # noqa: BLE001 - a key press must always end with a logged result
        result = {"status": "error", "error": f"{type(error).__name__}: {error}"}
    log_media_key("press", binding=binding_id, action=binding.get("action_id") or binding.get("script_id"),
                  status=result.get("status"), error=result.get("error"))
    if record and result.get("status") not in ("holding", "fired"):
        record_press(paths, binding_id, result, clock)
    return result


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        return 2
    try:
        result = handle_press(argv[0])
    except Exception as error:  # noqa: BLE001
        log_media_key("press-failed", binding=argv[0][:40], error=f"{type(error).__name__}: {error}")
        return 1
    return 0 if result.get("status") in ("ok", "skipped", "disabled", "holding", "fired") else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
