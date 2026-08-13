"""Shared GPU texture loading for bundled image assets (currently just the
logo). One place so every screen/the shell reuses the same cached texture
instead of re-decoding the PNG per call site.

Must only be called from a real draw frame (a live GL context) -- see
``ui/screens/about.py``'s note on why this can't run from ``on_enter()``:
uploading a texture without a live rendering backend doesn't raise a
catchable Python exception, it hard-crashes the process.
"""

from pathlib import Path
from typing import Optional, Tuple

from imgui_bundle import hello_imgui, imgui

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
_LOGO_CACHE_KEY = "logo"

_logo_load_attempted = False
_logo_texture_id: Optional[int] = None
_logo_size: Optional["imgui.ImVec2"] = None


def logo_texture() -> Optional[Tuple[int, "imgui.ImVec2"]]:
    """Returns (texture_id, size) for the bundled logo, loading it once on
    first call. Returns None (never raises) if the asset is missing or the
    upload fails -- callers should treat the logo as optional decoration."""
    global _logo_load_attempted, _logo_texture_id, _logo_size
    if not _logo_load_attempted:
        _logo_load_attempted = True
        try:
            data = (_ASSETS_DIR / "logo.png").read_bytes()
            image = hello_imgui.image_and_size_from_encoded_data(data, _LOGO_CACHE_KEY)
            _logo_texture_id = image.texture_id
            _logo_size = image.size
        except (OSError, RuntimeError):
            _logo_texture_id = None
            _logo_size = None
    if _logo_texture_id is None or _logo_size is None:
        return None
    return _logo_texture_id, _logo_size
