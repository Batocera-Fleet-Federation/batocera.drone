"""Shared single-line peer display label sanitization and fallback helpers."""

from __future__ import annotations

from typing import Any, Optional


def _is_control_or_line_break(ch: str) -> bool:
    return ord(ch) < 32 or 0x7F <= ord(ch) <= 0x9F or ch in ("\u2028", "\u2029")


def _has_control_or_line_break(text: str) -> bool:
    return any(_is_control_or_line_break(ch) for ch in text)


def sanitize_single_line_text(value: Any) -> str:
    """Replace ASCII/C1 control characters and Unicode line breaks with spaces."""
    if value is None or isinstance(value, (dict, list, tuple, set, bool)):
        return ""
    return "".join(
        " " if _is_control_or_line_break(ch) else ch
        for ch in str(value)
    ).strip()


def clean_single_line_label(value: Any, *, max_length: Optional[int] = None) -> str:
    """Return a trimmed single-line display label, or "" if unsafe/blank.

    Rejects strings containing ASCII control characters (including CR, LF,
    tabs, vertical tabs, form feeds, and ESC), C1 control characters (including
    NEL ``\\x85``), or Unicode line/paragraph separators (``\\u2028``,
    ``\\u2029``) so peer-supplied names can never forge extra lines in plaintext
    digests or notifications.
    """
    if value is None or isinstance(value, (dict, list, tuple, set, bool)):
        return ""
    text = str(value).strip()
    if not text or _has_control_or_line_break(text):
        return ""
    if max_length is not None and len(text) > max_length:
        return ""
    return text


def resolve_peer_label(
    name: Any,
    hostname: Any = None,
    peer_id: Any = None,
    *,
    fallback: str = "a paired peer",
) -> str:
    """Resolve the safest single-line peer label from name -> hostname -> id -> fallback."""
    return (
        clean_single_line_label(name)
        or clean_single_line_label(hostname)
        or clean_single_line_label(peer_id)
        or fallback
    )
