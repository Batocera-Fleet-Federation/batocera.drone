"""Screen contract every ``ui/screens/*.py`` implements.

ImGui rendering remains single-threaded, but Drone API actions run on daemon
worker threads.  Keeping blocking network I/O out of the render loop lets the
loading spinner animate, keeps controller input responsive, and prevents a
slow peer request from making the native client look frozen.

The synchronous screen methods remain useful outside the draw loop (and in
unit tests); only UI callbacks opt into background execution.
"""

import threading
import traceback
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class _BackgroundAction:
    label: str
    action: Callable[[], None]
    thread: Optional[threading.Thread] = None
    error: Optional[Exception] = None


class Screen:
    def on_enter(self) -> None:
        """Called once by Navigator.go_to right before this screen becomes current."""

    def draw(self, navigator) -> None:
        raise NotImplementedError

    def defer_action(self, label: str, action: Callable[[], None]) -> bool:
        """Schedule one UI action to start outside the render thread.

        Returns ``False`` while another action is queued or running so repeat
        input cannot create duplicate downloads, mounts, or restores.
        """
        if getattr(self, "_deferred_action", None) is not None:
            return False
        self._deferred_action = _BackgroundAction(str(label or "Loading..."), action)
        return True

    def run_deferred_action(self) -> None:
        """Start a queued action or retire a completed worker.

        Called once per render frame. The action label remains available for
        every frame while the worker is alive, which is what makes the shared
        spinner visibly animate instead of freezing on its first frame.
        """
        pending: Optional[_BackgroundAction] = getattr(self, "_deferred_action", None)
        if pending is None:
            return
        if pending.thread is None:
            pending.thread = threading.Thread(
                target=self._execute_background_action,
                args=(pending,),
                name="drone-ports-ui-action",
                daemon=True,
            )
            pending.thread.start()
            return
        if pending.thread.is_alive():
            return
        self._deferred_action = None
        if pending.error is not None:
            self._background_action_error = str(pending.error)

    @staticmethod
    def _execute_background_action(pending: _BackgroundAction) -> None:
        try:
            pending.action()
        except Exception as error:  # keep an unexpected worker failure from closing the UI
            pending.error = error
            traceback.print_exc()

    def wait_for_deferred_action(self, timeout: float = 5.0) -> None:
        """Test/support helper that waits for the current background action."""
        self.run_deferred_action()
        pending: Optional[_BackgroundAction] = getattr(self, "_deferred_action", None)
        if pending is None or pending.thread is None:
            return
        pending.thread.join(timeout)
        if pending.thread.is_alive():
            raise TimeoutError(f"Timed out waiting for {pending.label}")
        self.run_deferred_action()

    @property
    def deferred_action_label(self) -> Optional[str]:
        pending: Optional[_BackgroundAction] = getattr(self, "_deferred_action", None)
        return pending.label if pending is not None else None
