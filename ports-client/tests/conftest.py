"""Make both ``client`` (this app) and ``app`` (the Drone package, for the
integration test's real server harness) importable regardless of the cwd
pytest was invoked from.
"""

import sys
from pathlib import Path

_PORTS_CLIENT_ROOT = Path(__file__).resolve().parents[1]
_DRONE_ROOT = _PORTS_CLIENT_ROOT.parent

for _path in (_PORTS_CLIENT_ROOT, _DRONE_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
