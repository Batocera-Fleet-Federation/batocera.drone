"""Compatibility re-export: the subprocess boundary now lives in ``integrations.shared.process``."""

try:
    from ..shared.process import (  # noqa: F401
        DEFAULT_OUTPUT_LIMIT, ProcessResult, ProcessRunner, _EXIT_DRAIN_SECONDS, _signal_group,
    )
except ImportError:  # pragma: no cover - flat execution
    from integrations.shared.process import (  # type: ignore  # noqa: F401
        DEFAULT_OUTPUT_LIMIT, ProcessResult, ProcessRunner, _EXIT_DRAIN_SECONDS, _signal_group,
    )
