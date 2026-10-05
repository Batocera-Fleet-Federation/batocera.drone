"""Small in-memory background jobs so long work never blocks a web request.

Used for install/repair progress, script tests and Test Action / Test Game
Launch. Jobs are polled by ID (``GET .../jobs/<id>``), can be cancelled when
the work supports it (scripts), and only the most recent ones are retained.
"""

import threading
import time
import uuid
from typing import Callable, Dict, List, Optional

from .config import hex_id

MAX_JOBS = 50
MAX_RUNNING_JOBS = 4


class JobHandle:
    def __init__(self, registry: "JobRegistry", job_id: str) -> None:
        self.registry = registry
        self.id = job_id
        self.cancel_event = threading.Event()

    def progress(self, message: str) -> None:
        self.registry._update(self.id, message=str(message), steps_append=str(message))


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: Dict[str, dict] = {}
        self._handles: Dict[str, JobHandle] = {}
        self._lock = threading.Lock()

    def _update(self, job_id: str, *, steps_append: Optional[str] = None, **values) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.update(values)
            if steps_append and (not job["steps"] or job["steps"][-1] != steps_append):
                job["steps"].append(steps_append)
            job["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def start(self, kind: str, work: Callable[[JobHandle], dict], *, description: str = "",
              cancellable: bool = False, on_finish: Optional[Callable[[dict], None]] = None) -> dict:
        job_id = uuid.uuid4().hex
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        handle = JobHandle(self, job_id)
        with self._lock:
            if sum(job["status"] == "running" for job in self._jobs.values()) >= MAX_RUNNING_JOBS:
                raise RuntimeError("Too many tests are running; wait for one to finish or cancel it.")
            self._jobs[job_id] = {"id": job_id, "kind": kind, "description": description, "status": "running",
                                  "message": "Starting...", "steps": [], "result": None, "error": "",
                                  "cancellable": cancellable, "started_at": now, "updated_at": now, "finished_at": None}
            self._handles[job_id] = handle
            for stale in list(self._jobs)[:-MAX_JOBS]:
                if self._jobs[stale]["status"] != "running":
                    self._jobs.pop(stale, None)
                    self._handles.pop(stale, None)

        def run() -> None:
            started = time.monotonic()
            try:
                result = work(handle) or {}
                status = "cancelled" if handle.cancel_event.is_set() else "completed"
                if isinstance(result, dict) and result.get("status") in ("error", "failed", "timed-out", "busy", "unavailable"):
                    status = "failed"
                self._update(job_id, status=status, result=result,
                             error=str(result.get("error") or "") if isinstance(result, dict) else "")
            except Exception as error:  # noqa: BLE001 - surfaced to the UI, never raised
                self._update(job_id, status="failed", error=str(error) or error.__class__.__name__)
            self._update(job_id, finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                         duration_seconds=round(time.monotonic() - started, 3))
            if on_finish is not None:
                try:
                    on_finish(self.get(job_id))
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=run, name=f"streamdeck-job-{kind}", daemon=True).start()
        return self.get(job_id)

    def get(self, job_id: str) -> dict:
        job_id = hex_id(job_id, "job id")
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError("job not found")
            return {**job, "steps": list(job["steps"])}

    def cancel(self, job_id: str) -> dict:
        job_id = hex_id(job_id, "job id")
        with self._lock:
            job = self._jobs.get(job_id)
            handle = self._handles.get(job_id)
            if job is None or handle is None:
                raise KeyError("job not found")
            if not job["cancellable"]:
                raise ValueError("this job cannot be cancelled")
        handle.cancel_event.set()
        return self.get(job_id)

    def running(self, kinds: Optional[List[str]] = None) -> Optional[dict]:
        with self._lock:
            for job in reversed(list(self._jobs.values())):
                if job["status"] == "running" and (kinds is None or job["kind"] in kinds):
                    return {**job, "steps": list(job["steps"])}
        return None

    def latest(self, kinds: List[str]) -> Optional[dict]:
        with self._lock:
            for job in reversed(list(self._jobs.values())):
                if job["kind"] in kinds:
                    return {**job, "steps": list(job["steps"])}
        return None
