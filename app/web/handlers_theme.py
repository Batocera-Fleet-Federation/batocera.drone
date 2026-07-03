"""RomRequestHandler theme-metadata helper, as a mixin.

Extracted from ``drone_api.py``. Builds the resolved Batocera-theme metadata dict:
the selected theme name (env / batocera.conf / es_settings precedence) plus the
best-effort css/background/logo asset URLs discovered under the theme dir. Consumed
by the ``/theme/meta`` handler in ``handlers_content.py`` via ``self._build_theme_meta()``;
composed onto ``RomRequestHandler``.
"""

from pathlib import Path
from typing import List, Optional, Tuple

try:
    from ..device.device_control import (
        _parse_batocera_theme_name,
        _parse_es_theme_name,
        _resolve_es_settings_file,
        _resolve_theme_dir,
    )
    from .route_config import api_url
except ImportError:  # pragma: no cover - direct script execution fallback
    from device.device_control import (  # type: ignore
        _parse_batocera_theme_name,
        _parse_es_theme_name,
        _resolve_es_settings_file,
        _resolve_theme_dir,
    )
    from web.route_config import api_url  # type: ignore


class ThemeMetaMixin:
    def _build_theme_meta(self) -> dict:
        explicit = self.settings.batocera_theme_name
        from_batocera_conf = _parse_batocera_theme_name(self.settings.batocera_conf_file)
        resolved_es_settings_file = _resolve_es_settings_file(self.settings)
        from_es_settings = _parse_es_theme_name(resolved_es_settings_file) if resolved_es_settings_file else None
        selected = explicit or from_batocera_conf or from_es_settings
        theme_dir = _resolve_theme_dir(self.settings)
        if not theme_dir:
            return {
                "enabled": False,
                "selected_theme_name": selected,
                "theme_sources": {
                    "env": explicit,
                    "batocera_conf": from_batocera_conf,
                    "es_settings": from_es_settings,
                },
                "themes_root": str(self.settings.themes_root),
                "es_settings_file": str(resolved_es_settings_file) if resolved_es_settings_file else None,
            }

        css_candidates = ["theme.css", "style.css", "theme/theme.css", "theme/style.css", "_inc/theme.css", "_inc/style.css"]
        bg_name_candidates = ["background", "fond", "bg", "backdrop", "wallpaper"]
        logo_name_candidates = ["logo", "brand", "title", "system-logo"]

        def first_existing(candidates: List[str]) -> Optional[str]:
            for rel in candidates:
                target = (theme_dir / rel).resolve()
                if target.exists() and target.is_file() and theme_dir in target.parents:
                    return rel
            return None

        def first_match_recursive(name_fragments: List[str], allowed_suffixes: Tuple[str, ...]) -> Optional[str]:
            # Keep this bounded for large theme trees.
            checked = 0
            for path in theme_dir.rglob("*"):
                if checked > 5000:
                    break
                checked += 1
                if not path.is_file():
                    continue
                suffix = path.suffix.lower()
                if suffix not in allowed_suffixes:
                    continue
                name_lower = path.stem.lower()
                if any(fragment in name_lower for fragment in name_fragments):
                    try:
                        return path.relative_to(theme_dir).as_posix()
                    except Exception:
                        continue
            return None

        css_file = first_existing(css_candidates)
        if not css_file:
            css_file = first_match_recursive(["theme", "style"], (".css",))

        bg_file = first_existing(
            [
                "art/background.png",
                "art/background.jpg",
                "art/fond.png",
                "art/fond.jpg",
                "background.png",
                "background.jpg",
            ]
        )
        if not bg_file:
            bg_file = first_match_recursive(bg_name_candidates, (".png", ".jpg", ".jpeg", ".webp"))

        logo_file = first_existing(["art/logo.png", "art/logo.svg", "logo.png", "logo.svg"])
        if not logo_file:
            logo_file = first_match_recursive(logo_name_candidates, (".png", ".jpg", ".jpeg", ".webp", ".svg"))

        css_url = api_url(f"/theme/assets/{css_file}") if css_file else None
        if self.settings.use_fake_data and css_url:
            css_url = None
        background_url = self._fake_theme_asset_url(bg_file) if (self.settings.use_fake_data and bg_file) else (api_url(f"/theme/assets/{bg_file}") if bg_file else None)
        logo_url = self._fake_theme_asset_url(logo_file) if (self.settings.use_fake_data and logo_file) else (api_url(f"/theme/assets/{logo_file}") if logo_file else None)

        return {
            "enabled": True,
            "theme_name": theme_dir.name,
            "theme_dir": str(theme_dir),
            "selected_theme_name": selected,
            "theme_sources": {
                "env": explicit,
                "batocera_conf": from_batocera_conf,
                "es_settings": from_es_settings,
            },
            "themes_root": str(self.settings.themes_root),
            "es_settings_file": str(resolved_es_settings_file) if resolved_es_settings_file else None,
            "api": {
                "theme_assets_base": api_url("/theme/assets/"),
                "system_theme_meta": api_url("/theme/system/{system}"),
            },
            "ui": {
                "css_url": css_url,
                "background_url": background_url,
                "logo_url": logo_url,
            },
            "css_url": css_url,
            "background_url": background_url,
            "logo_url": logo_url,
            "resolved_files": {
                "css": css_file,
                "background": bg_file,
                "logo": logo_file,
            },
        }
