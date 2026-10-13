"""Stream Deck custom scripts -- the shared ``ScriptStore`` bound to this integration.

See ``integrations.shared.scripts`` for the storage, validation and execution
rules. Stream Deck only supplies its paths, ``DRONE_STREAMDECK_*`` environment
prefix and event log.
"""

from typing import Optional

from .logs import log_event
from .paths import StreamDeckPaths
from .process import ProcessRunner

try:
    from ..shared.scripts import (  # noqa: F401 - re-exported
        MAX_SCRIPT_BYTES, ScriptInUseError, ScriptStore as SharedScriptStore, _LANGUAGES, _SAFE_PATH,
    )
except ImportError:  # pragma: no cover - flat execution
    from integrations.shared.scripts import (  # type: ignore  # noqa: F401
        MAX_SCRIPT_BYTES, ScriptInUseError, ScriptStore as SharedScriptStore, _LANGUAGES, _SAFE_PATH,
    )


DEFAULT_SCRIPT = "#!/bin/bash\n# Runs as the Drone service on this Batocera machine.\necho \"Hello from Stream Deck\"\n"
DEFAULT_PYTHON_SCRIPT = (
    "#!/usr/bin/env python3\n"
    "# Runs as the Drone service on this Batocera machine.\n"
    "print(\"Hello from Stream Deck\")\n"
)


class ScriptStore(SharedScriptStore):
    def __init__(self, paths: StreamDeckPaths, runner: Optional[ProcessRunner] = None) -> None:
        super().__init__(paths, runner, env_prefix="DRONE_STREAMDECK_", log=lambda *a, **k: log_event(*a, **k))
