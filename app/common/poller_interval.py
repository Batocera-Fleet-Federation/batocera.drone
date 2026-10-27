"""Safe parsing for background-poller interval environment variables.

Interval env vars are operator input read at import time or at poller start. A bare
``float()`` accepts ``inf``/``nan``/``1e400`` and raises on ``abc``; the first lets
``time.sleep`` raise ``OverflowError`` (killing the poller before its first tick), the
second breaks importing the whole app. A value that cannot be used falls back to the
poller's default instead, and a usable value is never below ``minimum`` seconds.

This module must not raise: SMTP and tailnet bind their intervals at import, so any
exception here is ``import app.drone_api`` failing.

Leaf module: stdlib only, no imports from the rest of the app.
"""

from __future__ import annotations

import math
import os

# One year. Keeps every accepted value far below the range time.sleep can represent
# (CPython overflows somewhere around 9.2e9 seconds).
POLLER_INTERVAL_MAX_SECONDS = 365.0 * 24 * 3600
# Last-resort sleep if even ``default`` is unusable (should not happen for callers).
_SLEEPABLE_FALLBACK_SECONDS = 300.0


def _sleepable(value: float, minimum: float) -> float | None:
    """Return ``value`` floored at ``minimum`` if it is a portable ``time.sleep`` duration."""
    try:
        if not math.isfinite(value) or value > POLLER_INTERVAL_MAX_SECONDS:
            return None
        if not math.isfinite(minimum) or minimum < 0 or minimum > POLLER_INTERVAL_MAX_SECONDS:
            minimum = 30.0
        return max(minimum, value)
    except (TypeError, ValueError, OverflowError):
        return None


def parse_poller_interval(raw: str | None, default: float, minimum: float = 30.0) -> float:
    """Return ``raw`` as a sleepable interval in seconds, or ``default`` if it is unusable.

    ``None``/blank, non-numeric, non-finite (``inf``, ``nan``), or above
    ``POLLER_INTERVAL_MAX_SECONDS`` all fall back to ``default``. Usable values are
    clamped up to ``minimum``. Never raises: a bad ``raw`` type, ``float`` overflow,
    or unusable ``default`` still returns a sleepable number.
    """
    try:
        fallback = _sleepable(float(default), float(minimum)) or _SLEEPABLE_FALLBACK_SECONDS
    except (TypeError, ValueError, OverflowError):
        fallback = _SLEEPABLE_FALLBACK_SECONDS
    try:
        if raw is None:
            return fallback
        text = str(raw).strip()
        if not text:
            return fallback
        value = float(text)
    except (TypeError, ValueError, OverflowError, AttributeError):
        return fallback
    except Exception:
        return fallback
    return _sleepable(value, minimum) or fallback


def poller_interval_from_env(name: str, default: float, minimum: float = 30.0) -> float:
    """Read interval env var ``name`` via :func:`parse_poller_interval`. Never raises."""
    try:
        return parse_poller_interval(os.environ.get(name), default, minimum)
    except Exception:
        try:
            fallback = _sleepable(float(default), float(minimum))
        except (TypeError, ValueError, OverflowError):
            fallback = None
        return fallback or _SLEEPABLE_FALLBACK_SECONDS
