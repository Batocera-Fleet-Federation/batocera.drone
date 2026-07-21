"""Readable formatting for outbound HTTP errors.

Extracted from the retired Overmind client. Pure stdlib, no settings/network
dependency, so it can be imported from anywhere (roms/, device/, transfer/, web/)
without creating a cycle.
"""

from urllib.error import HTTPError, URLError


def _format_http_error(error: BaseException) -> str:
    if isinstance(error, HTTPError):
        detail = ""
        try:
            raw = error.read()
            detail = raw.decode("utf-8", errors="replace").strip() if raw else ""
        except Exception:
            detail = ""
        if len(detail) > 500:
            detail = detail[:500] + "..."
        suffix = f" body={detail}" if detail else ""
        return f"HTTPError status={error.code} reason={error.reason or error.msg or 'unknown'} url={error.geturl()}{suffix}"
    if isinstance(error, URLError):
        reason = getattr(error, "reason", None)
        return f"URLError reason={reason!r}" if reason else f"URLError {error!r}"
    message = str(error).strip()
    if message:
        return f"{error.__class__.__name__}: {message}"
    return repr(error)
