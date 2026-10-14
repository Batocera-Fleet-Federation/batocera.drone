"""One supervised housekeeping thread: recovery, expiry and liveness checks."""
import threading
from pathlib import Path
from typing import Any, Optional

from .orchestrator import get_orchestrator

SWEEP_SECONDS = 10.0
STARTUP_DELAY_SECONDS = 3.0
_LOCK = threading.Lock()
_THREAD: Optional[threading.Thread] = None
_STOP: Optional[threading.Event] = None


def _loop(settings: Any, repository: Any, stop: threading.Event) -> None:
    if stop.wait(STARTUP_DELAY_SECONDS) or not Path(str(settings.userdata_root)).exists():
        return
    orchestrator = get_orchestrator(settings, repository)
    try:
        orchestrator.recover()
    except Exception:  # noqa: BLE001
        pass
    while not stop.wait(SWEEP_SECONDS):
        try:
            if not Path(str(settings.userdata_root)).exists():
                continue
            # Disabling multiplayer must not strand completed-session journals or generated credentials.
            orchestrator.tick()
        except Exception:  # noqa: BLE001
            continue


def start_multiplayer_worker(settings: Any, repository: Any) -> Optional[threading.Thread]:
    global _THREAD, _STOP
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return None
        stop = threading.Event()  # each generation owns its event; a restart cannot revive an old loop
        thread = threading.Thread(target=_loop, args=(settings, repository, stop), name="drone-multiplayer-worker", daemon=True)
        _THREAD, _STOP = thread, stop
        thread.start()
        return thread


def stop_multiplayer_worker() -> None:
    """Join the worker before server teardown removes persistent state."""
    with _LOCK:
        thread, stop = _THREAD, _STOP
        if stop is not None:
            stop.set()
    if thread is not None and thread is not threading.current_thread():
        thread.join()  # peer probes/launch waits already have bounded timeouts
