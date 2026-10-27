"""Safe parsing for background-poller interval environment variables.

Interval env vars are operator input read at import time or at poller start. A bare
``float()`` accepts ``inf``/``nan``/``1e400`` and raises on ``abc``; the first lets
``time.sleep`` raise ``OverflowError`` (killing the poller before its first tick), the
second breaks importing the whole app. A value that cannot be used falls back to the
poller's default instead, and a usable value is never below ``minimum`` seconds.

Leaf module: stdlib only, no imports from the rest of the app.
"""

from __future__ import annotations

import math
import os

# One year. Keeps every accepted value far below the range time.sleep can represent
# (CPython overflows somewhere around 9.2e9 seconds).
POLLER_INTERVAL_MAX_SECONDS = 365.0 * 24 * 3600


def parse_poller_interval(raw: str | None, default: float, minimum: float = 30.0) -> float:
    """Return ``raw`` as a sleepable interval in seconds, or ``default`` if it is unusable.

    ``None``/blank, non-numeric, non-finite (``inf``, ``nan``), or above
    ``POLLER_INTERVAL_MAX_SECONDS`` all fall back to ``default``. Usable values are
    clamped up to ``minimum``.
    """
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value) or value > POLLER_INTERVAL_MAX_SECONDS:
        return default
    return max(minimum, value)


def poller_interval_from_env(name: str, default: float, minimum: float = 30.0) -> float:
    """Read interval env var ``name`` via :func:`parse_poller_interval`."""
    return parse_poller_interval(os.environ.get(name), default, minimum)
