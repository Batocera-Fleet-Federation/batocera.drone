"""Screen contract every ``ui/screens/*.py`` implements.

The native client is intentionally single-threaded.  A network call made in
the same ImGui frame as a button click prevents that frame from reaching the
display, which used to make actions look as though they had frozen the app.
``defer_action`` gives every screen the same two-frame pattern: arm the work,
render a visible loading panel, then execute the blocking call at the start of
the next frame while that already-presented panel remains on screen.

The synchronous screen methods remain useful outside the draw loop (and in
unit tests); only UI callbacks opt into deferral.
"""

from typing import Callable, Optional, Tuple


class Screen:
    def on_enter(self) -> None:
        """Called once by Navigator.go_to right before this screen becomes current."""

    def draw(self, navigator) -> None:
        raise NotImplementedError

    def defer_action(self, label: str, action: Callable[[], None]) -> bool:
        """Schedule one UI action for the next frame.

        Returns ``False`` when another action is already armed so double-clicks
        cannot queue duplicate downloads/mounts/restores.
        """
        if getattr(self, "_deferred_action", None) is not None:
            return False
        self._deferred_action = (str(label or "Loading..."), action)
        return True

    def run_deferred_action(self) -> None:
        pending: Optional[Tuple[str, Callable[[], None]]] = getattr(self, "_deferred_action", None)
        if pending is None:
            return
        # Clear before invoking so the action may deliberately schedule a
        # follow-up operation without being rejected as a duplicate.
        self._deferred_action = None
        _label, action = pending
        action()

    @property
    def deferred_action_label(self) -> Optional[str]:
        pending = getattr(self, "_deferred_action", None)
        return pending[0] if pending is not None else None
