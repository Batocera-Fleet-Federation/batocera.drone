"""Entry point of the isolated Stream Deck hardware worker.

Started by the Drone (``manager.StreamDeckIntegration.start_runtime``) as::

    python3 -m app.integrations.streamdeck.worker --root <integration dir> \
        --roms-root /userdata/roms --parent-pid <drone pid>

It is the only process that imports python-elgato-streamdeck/Pillow, and only
from the integration's private ``lib/`` directory. It holds
``state/runtime.lock`` for its whole life (a second worker exits immediately),
writes its pid to ``state/runtime.pid``, logs to the size-rotating
``logs/runtime.log``, releases every device on SIGTERM, and exits on its own
when the Drone that started it is gone -- like aria2's
``--stop-with-process`` -- so a Drone restart or update never leaves a stale
worker holding the hardware.

``--probe`` enumerates/opens decks once and prints their capabilities as JSON
(used by "Test Connection" while the runtime is stopped).
"""

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .actions import BatoceraControl, BuiltInActionRegistry, RetroArchControl
from .dispatcher import ButtonDispatcher
from .emulationstation import EmulationStationApi
from .game_launcher import GameLauncher
from .game_runtime import GameRuntime
from .launch import LaunchGameCoordinator
from .logs import log_event, set_log_sink
from .paths import StreamDeckPaths, atomic_write_json, try_file_lock
from .process import ProcessRunner
from .runtime import StreamDeckRuntime
from .scripts import ScriptStore

LOOP_SECONDS = 0.25
LOCK_WAIT_SECONDS = 10.0
EXIT_DUPLICATE = 3


def configure_worker_logging(paths: StreamDeckPaths) -> None:
    try:
        from ...common.logging_setup import _TeeRotatingStream
    except ImportError:  # pragma: no cover - flat execution
        from common.logging_setup import _TeeRotatingStream  # type: ignore
    paths.ensure(paths.logs_dir)
    stream = _TeeRotatingStream(original_stream=None, log_path=paths.runtime_log, max_bytes=1024 * 1024, backup_count=3)
    sys.stdout = stream
    sys.stderr = stream
    set_log_sink(lambda line: print(line, flush=True))


def _parent_alive(parent_pid: Optional[int]) -> bool:
    if not parent_pid:
        return True
    if os.getppid() != parent_pid:
        return False
    try:
        os.kill(parent_pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def probe(paths: StreamDeckPaths) -> dict:
    from .devices import LibraryDeviceProvider

    report = []
    for device in LibraryDeviceProvider().enumerate():
        entry = {"model": device.model, "transport_id": device.transport_id}
        try:
            device.open()
            entry.update(device.get_capabilities())
            entry["communication"] = "ok"
        except Exception as error:  # noqa: BLE001
            entry["communication"] = f"failed: {error}"
        finally:
            try:
                device.close()
            except Exception:  # noqa: BLE001
                pass
        report.append(entry)
    return {"status": "ok" if report else "no-device", "devices": report}


def build_runtime(paths: StreamDeckPaths, roms_root: Path) -> StreamDeckRuntime:
    from .devices import LibraryDeviceProvider
    from .render import KeyRenderer

    runner = ProcessRunner()
    es_api = EmulationStationApi()
    game_runtime = GameRuntime(es_api=es_api)
    actions = BuiltInActionRegistry(BatoceraControl(runner), RetroArchControl(), game_runtime)
    coordinator = LaunchGameCoordinator(game_runtime, GameLauncher(roms_root, es_api), actions,
                                        lock_path=paths.launch_lock)
    scripts = ScriptStore(paths, runner)
    holder: dict = {}
    dispatcher = ButtonDispatcher(actions, coordinator, scripts,
                                  script_timeout=lambda: float((holder["runtime"].document.get("settings") or {})
                                                               .get("script_timeout_seconds", 30)))
    allowed = [paths.images_dir, roms_root]
    try:
        allowed.extend(entry for entry in roms_root.iterdir() if entry.is_dir())
    except OSError:
        pass
    renderer = KeyRenderer(paths.rendered_dir, allowed_roots=tuple(allowed))
    runtime = StreamDeckRuntime(paths, LibraryDeviceProvider(), renderer, dispatcher, actions, game_runtime)
    holder["runtime"] = runtime
    return runtime


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Batocera Drone Stream Deck runtime")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--roms-root", type=Path, required=True)
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--lock-wait", type=float, default=LOCK_WAIT_SECONDS, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    paths = StreamDeckPaths(args.root)
    lib_dir = str(paths.lib_dir)
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)

    if args.probe:
        print(json.dumps(probe(paths)))
        return 0

    configure_worker_logging(paths)
    paths.ensure_layout()
    deadline = time.monotonic() + max(0.0, args.lock_wait)
    while True:
        with try_file_lock(paths.runtime_lock) as acquired:
            if acquired:
                return _run(paths, args)
        if time.monotonic() >= deadline:
            log_event("runtime-duplicate", reason="another Stream Deck runtime holds the lock")
            return EXIT_DUPLICATE
        time.sleep(0.5)


def _run(paths: StreamDeckPaths, args: argparse.Namespace) -> int:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_args: stop.set())
    signal.signal(signal.SIGINT, lambda *_args: stop.set())
    atomic_write_json(paths.pid_file, {"pid": os.getpid(), "parent_pid": args.parent_pid,
                                       "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    log_event("runtime-started", pid=os.getpid(), python=sys.version.split()[0])
    runtime = None
    try:
        runtime = build_runtime(paths, args.roms_root)
        runtime.load_document(force=True)
        while not stop.is_set():
            if not _parent_alive(args.parent_pid):
                log_event("runtime-parent-exited", parent_pid=args.parent_pid)
                break
            try:
                runtime.tick()
            except Exception as error:  # noqa: BLE001 - the loop must survive anything
                log_event("runtime-error", scope="loop", error=str(error))
            stop.wait(LOOP_SECONDS)
    except Exception as error:  # noqa: BLE001 - e.g. missing HID backend
        log_event("runtime-failed", error=str(error))
        atomic_write_json(paths.worker_status_file, {"runtime": "error", "last_error": str(error),
                                                     "pid": os.getpid(), "devices": []})
        return 1
    finally:
        if runtime is not None:
            runtime.shutdown()
        paths.pid_file.unlink(missing_ok=True)
        log_event("runtime-stopped", pid=os.getpid())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
