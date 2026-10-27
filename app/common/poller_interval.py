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
_DEFAULT_MINIMUM_SECONDS = 30.0


def _number(value: object) -> float | None:
    """Convert untrusted configuration input to a float without propagating errors."""
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError, AttributeError):
        return None
    # This is an import-time configuration boundary.  A hostile object's
    # conversion hook must not prevent the application from starting.
    except Exception:
        return None


def _minimum(value: object) -> float:
    """Return a valid polling floor, preserving the established 30-second default."""
    minimum = _number(value)
    if minimum is None or not math.isfinite(minimum) or not 0 <= minimum <= POLLER_INTERVAL_MAX_SECONDS:
        return _DEFAULT_MINIMUM_SECONDS
    return minimum


def _sleepable(value: float | None, minimum: float) -> float | None:
    """Return a finite, bounded duration floored at ``minimum`` for ``time.sleep``."""
    if value is None or not math.isfinite(value) or value > POLLER_INTERVAL_MAX_SECONDS:
        return None
    return max(minimum, value)


def parse_poller_interval(raw: str | None, default: float, minimum: float = 30.0) -> float:
    """Return ``raw`` as a sleepable interval in seconds, or ``default`` if it is unusable.

    ``None``/blank, non-numeric, non-finite (``inf``, ``nan``), or above
    ``POLLER_INTERVAL_MAX_SECONDS`` all fall back to ``default``. Usable values are
    clamped up to ``minimum``. Never raises: a bad ``raw`` type, ``float`` overflow,
    or unusable ``default`` still returns a sleepable number.
    """
    floor = _minimum(minimum)
    fallback = _sleepable(_number(default), floor) or _SLEEPABLE_FALLBACK_SECONDS
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
    return _sleepable(value, floor) or fallback


def poller_interval_from_env(name: str, default: float, minimum: float = 30.0) -> float:
    """Read interval env var ``name`` via :func:`parse_poller_interval`. Never raises."""
    try:
        return parse_poller_interval(os.environ.get(name), default, minimum)
    except Exception:
        fallback = _sleepable(_number(default), _minimum(minimum))
        return fallback or _SLEEPABLE_FALLBACK_SECONDS


def seconds_from_env(name: str, default: float, maximum: float = POLLER_INTERVAL_MAX_SECONDS) -> float:
    """Read a non-negative timeout in seconds from ``name``. Never raises.

    For request/command timeouts and grace periods, not pollers: no floor is applied.
    Blank, non-numeric, non-finite, negative, or above ``maximum`` values fall back to
    ``default``.
    """
    try:
        text = os.environ.get(name)
        if text is None or not text.strip():
            return default
        value = float(text.strip())
    except Exception:
        return default
    if not math.isfinite(value) or not 0 <= value <= maximum:
        return default
    return value


def int_from_env(name: str, default: int, minimum: int = 0, maximum: int = 1_000_000) -> int:
    """Read a bounded integer from ``name``. Never raises; unusable values use ``default``."""
    try:
        text = os.environ.get(name)
        if text is None or not text.strip():
            return default
        value = int(text.strip())
    except Exception:
        return default
    if not minimum <= value <= maximum:
        return default
    return value
