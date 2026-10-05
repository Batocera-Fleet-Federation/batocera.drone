"""Key-image rendering with Pillow (imported lazily: worker process only).

Input is a *render spec* compiled by the Drone (``runtime_document``), so the
browser preview and the physical key are built from the same description:

    {"kind": "generated" | "image" | "blank",
     "text", "secondary_text", "symbol", "background", "text_color",
     "text_size", "align",                       # generated art
     "source": "/abs/path", "fit": "fill|fit|stretch",
     "fallback": {...generated spec...}}         # image art

Output is an RGB image at the device's real key size; the device adapter then
applies the model's native format/rotation (``PILHelper``). Rendered results
are cached as PNGs in ``rendered/`` keyed by spec + source identity + size, so
full-size artwork is resized once, not on every apply.
"""

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from .paths import atomic_write_bytes

RENDERER_VERSION = 2
MAX_CACHE_FILES = 600
FONT_CANDIDATES = (
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/emulationstation/resources/opensans_hebrew_condensed_regular.ttf",
    "DejaVuSans-Bold.ttf",
)
TEXT_SCALE = {"small": 0.16, "medium": 0.22, "large": 0.3}


class KeyRenderer:
    def __init__(self, cache_dir: Optional[Path] = None, allowed_roots: Tuple[Path, ...] = ()) -> None:
        from PIL import Image, ImageDraw, ImageFont  # noqa: WPS433 - private lib/ only

        self.Image, self.ImageDraw, self.ImageFont = Image, ImageDraw, ImageFont
        Image.MAX_IMAGE_PIXELS = 4096 * 4096 * 2
        self.cache_dir = cache_dir
        self.allowed_roots = tuple(Path(os.path.realpath(str(root))) for root in allowed_roots)
        self._fonts: Dict[int, Any] = {}

    # -- public -----------------------------------------------------------
    def render(self, spec: dict, size: Tuple[int, int]) -> Any:
        width, height = max(1, int(size[0])), max(1, int(size[1]))
        kind = spec.get("kind") or "blank"
        if kind == "image":
            source = self._allowed_source(spec.get("source"))
            if source is not None:
                try:
                    return self._cached(spec, (width, height), source, lambda: self._image(spec, source, (width, height)))
                except Exception:  # noqa: BLE001 - corrupt artwork falls back to generated art
                    pass
            spec = spec.get("fallback") or {"kind": "generated", "text": "?"}
            kind = spec.get("kind") or "generated"
        if kind == "generated":
            return self._cached(spec, (width, height), None, lambda: self._generated(spec, (width, height)))
        return self.Image.new("RGB", (width, height), "#000000")

    def write_preview(self, image: Any, path: Path) -> None:
        """Save exactly what was sent to a key, for the admin "as rendered" preview."""
        from io import BytesIO
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        try:
            atomic_write_bytes(path, buffer.getvalue())
        except OSError:
            pass

    # -- cache --------------------------------------------------------------
    def _allowed_source(self, raw: Any) -> Optional[Path]:
        if not raw:
            return None
        path = Path(os.path.realpath(str(raw)))
        if self.allowed_roots and not any(path == root or root in path.parents for root in self.allowed_roots):
            return None
        return path if path.is_file() else None

    def _cache_key(self, spec: dict, size: Tuple[int, int], source: Optional[Path]) -> str:
        identity: Dict[str, Any] = {"v": RENDERER_VERSION, "spec": spec, "size": list(size)}
        if source is not None:
            stat = source.stat()
            identity["source"] = [str(source), stat.st_size, stat.st_mtime_ns]
        return hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    def _cached(self, spec: dict, size: Tuple[int, int], source: Optional[Path], build: Callable[[], Any]) -> Any:
        if self.cache_dir is None:
            return build()
        target = self.cache_dir / f"{self._cache_key(spec, size, source)}.png"
        if target.is_file():
            try:
                with self.Image.open(target) as cached:
                    return cached.convert("RGB")
            except Exception:  # noqa: BLE001 - rebuild a damaged cache entry
                target.unlink(missing_ok=True)
        image = build()
        try:
            from io import BytesIO
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            atomic_write_bytes(target, buffer.getvalue())
            self._prune_cache()
        except OSError:
            pass
        return image

    def _prune_cache(self) -> None:
        try:
            files = sorted(self.cache_dir.glob("*.png"), key=lambda path: path.stat().st_mtime)
        except OSError:
            return
        for stale in files[: max(0, len(files) - MAX_CACHE_FILES)]:
            stale.unlink(missing_ok=True)

    # -- drawing ------------------------------------------------------------
    def _image(self, spec: dict, source: Path, size: Tuple[int, int]) -> Any:
        width, height = size
        with self.Image.open(source) as original:
            original.draft("RGB", (width * 2, height * 2))
            image = original.convert("RGBA")
        backdrop = self.Image.new("RGBA", image.size, spec.get("background") or "#000000")
        image = self.Image.alpha_composite(backdrop, image).convert("RGB")
        fit = spec.get("fit") or "fill"
        resample = getattr(self.Image, "Resampling", self.Image).LANCZOS
        if fit == "stretch":
            canvas = image.resize((width, height), resample)
            if spec.get("text"):
                self._caption(canvas, str(spec["text"]), spec.get("text_color") or "#ffffff")
            return canvas
        scale = (min if fit == "fit" else max)(width / image.width, height / image.height)
        resized = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), resample)
        canvas = self.Image.new("RGB", (width, height), spec.get("background") or "#000000")
        canvas.paste(resized, ((width - resized.width) // 2, (height - resized.height) // 2))
        if spec.get("text"):
            self._caption(canvas, str(spec["text"]), spec.get("text_color") or "#ffffff")
        return canvas

    def _font(self, size: int) -> Any:
        size = max(6, int(size))
        if size not in self._fonts:
            font = None
            for candidate in FONT_CANDIDATES:
                try:
                    font = self.ImageFont.truetype(candidate, size)
                    break
                except OSError:
                    continue
            if font is None:
                try:
                    font = self.ImageFont.load_default(size=size)
                except TypeError:
                    font = self.ImageFont.load_default()
            self._fonts[size] = font
        return self._fonts[size]

    def _fit_text(self, draw: Any, text: str, max_width: int, start_size: int) -> Tuple[Any, Tuple[int, int, int, int]]:
        size = start_size
        while True:
            font = self._font(size)
            box = draw.multiline_textbbox((0, 0), text, font=font, align="center", spacing=1)
            if box[2] - box[0] <= max_width or size <= 7:
                return font, box
            size -= 1

    @staticmethod
    def _wrap(text: str, limit: int = 10) -> str:
        words, lines, line = text.split(), [], ""
        for word in words:
            if line and len(line) + 1 + len(word) > limit:
                lines.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        if line:
            lines.append(line)
        return "\n".join(lines[:3])

    def _caption(self, canvas: Any, text: str, color: str) -> None:
        width, height = canvas.size
        draw = self.ImageDraw.Draw(canvas)
        band = max(10, height // 4)
        draw.rectangle((0, height - band, width, height), fill="#000000")
        font, box = self._fit_text(draw, text[:18], int(width * 0.92), int(band * 0.75))
        draw.text(((width - (box[2] - box[0])) / 2 - box[0], height - band + (band - (box[3] - box[1])) / 2 - box[1]),
                  text[:18], fill=color, font=font)

    def _generated(self, spec: dict, size: Tuple[int, int]) -> Any:
        width, height = size
        image = self.Image.new("RGB", (width, height), spec.get("background") or "#111827")
        draw = self.ImageDraw.Draw(image)
        color = spec.get("text_color") or "#ffffff"
        text = self._wrap(str(spec.get("text") or ""))
        secondary = str(spec.get("secondary_text") or "")[:24]
        symbol = spec.get("symbol") or ""
        top, bottom = 0.0, float(height)
        if symbol:
            icon_box = (width * 0.22, height * (0.08 if text else 0.18), width * 0.78, height * (0.56 if text else 0.82))
            if not text and not secondary:
                icon_box = (width * 0.18, height * 0.18, width * 0.82, height * 0.82)
            draw_symbol(draw, symbol, icon_box, color)
            top = icon_box[3] + height * 0.04
        if text:
            scale = TEXT_SCALE.get(spec.get("text_size") or "medium", 0.22)
            if symbol:
                scale = min(scale, 0.2)
            font, box = self._fit_text(draw, text, int(width * 0.9), int(height * scale))
            text_h = box[3] - box[1]
            sec_h = 0
            sec_font = None
            if secondary:
                sec_font, sec_box = self._fit_text(draw, secondary, int(width * 0.9), int(height * 0.13))
                sec_h = sec_box[3] - sec_box[1] + 2
            block = text_h + sec_h
            align = spec.get("align") or "middle"
            if symbol:
                y = top + max(0.0, (bottom - top - block) / 2)
            elif align == "top":
                y = height * 0.08
            elif align == "bottom":
                y = height * 0.92 - block
            else:
                y = (height - block) / 2
            draw.multiline_text(((width - (box[2] - box[0])) / 2 - box[0], y - box[1]), text, fill=color,
                                font=font, align="center", spacing=1)
            if secondary and sec_font is not None:
                sec_box = draw.textbbox((0, 0), secondary, font=sec_font)
                draw.text(((width - (sec_box[2] - sec_box[0])) / 2 - sec_box[0], y + text_h + 2 - sec_box[1]),
                          secondary, fill=color, font=sec_font)
        return image


def draw_symbol(draw: Any, symbol: str, box: Tuple[float, float, float, float], color: str) -> None:
    """Simple vector glyphs so built-ins have default art without any font support."""
    x0, y0, x1, y1 = box
    side = min(x1 - x0, y1 - y0)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    left, top, right, bottom = cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2
    stroke = max(2, int(side / 9))

    def p(fx: float, fy: float) -> Tuple[float, float]:
        return (left + side * fx, top + side * fy)

    def speaker() -> None:
        draw.polygon([p(0.05, 0.38), p(0.25, 0.38), p(0.5, 0.15), p(0.5, 0.85), p(0.25, 0.62), p(0.05, 0.62)], fill=color)

    def arrow_circle(clockwise: bool = True) -> None:
        draw.arc((left + stroke, top + stroke, right - stroke, bottom - stroke), 300 if clockwise else 60, 240 if clockwise else 0,
                 fill=color, width=stroke)
        tip_angle = math.radians(300 if clockwise else 60)
        radius = side / 2 - stroke
        tx, ty = cx + radius * math.cos(tip_angle), cy + radius * math.sin(tip_angle)
        size = side * 0.18
        draw.polygon([(tx - size, ty - size * 0.2), (tx + size * 0.6, ty - size * 0.9), (tx + size * 0.4, ty + size * 0.7)], fill=color)

    if symbol == "power":
        draw.arc((left + stroke, top + stroke, right - stroke, bottom - stroke), 300, 240, fill=color, width=stroke)
        draw.line([p(0.5, 0.02), p(0.5, 0.48)], fill=color, width=stroke)
    elif symbol in ("reboot", "restart"):
        arrow_circle()
        if symbol == "restart":
            draw.rectangle((p(0.36, 0.38), p(0.64, 0.62)), outline=color, width=max(1, stroke // 2))
    elif symbol == "exit":
        draw.rectangle((p(0.08, 0.1), p(0.55, 0.9)), outline=color, width=stroke)
        draw.line([p(0.35, 0.5), p(0.95, 0.5)], fill=color, width=stroke)
        draw.polygon([p(0.98, 0.5), p(0.78, 0.32), p(0.78, 0.68)], fill=color)
    elif symbol == "volume-up":
        speaker()
        draw.line([p(0.62, 0.5), p(0.98, 0.5)], fill=color, width=stroke)
        draw.line([p(0.8, 0.32), p(0.8, 0.68)], fill=color, width=stroke)
    elif symbol == "volume-down":
        speaker()
        draw.line([p(0.62, 0.5), p(0.98, 0.5)], fill=color, width=stroke)
    elif symbol == "mute":
        speaker()
        draw.line([p(0.62, 0.34), p(0.95, 0.66)], fill=color, width=stroke)
        draw.line([p(0.62, 0.66), p(0.95, 0.34)], fill=color, width=stroke)
    elif symbol == "pause":
        draw.rectangle((p(0.2, 0.15), p(0.4, 0.85)), fill=color)
        draw.rectangle((p(0.6, 0.15), p(0.8, 0.85)), fill=color)
    elif symbol in ("save", "load"):
        draw.rectangle((p(0.1, 0.1), p(0.9, 0.9)), outline=color, width=stroke)
        draw.rectangle((p(0.28, 0.1), p(0.72, 0.38)), outline=color, width=max(1, stroke // 2))
        if symbol == "save":
            draw.polygon([p(0.5, 0.82), p(0.3, 0.55), p(0.7, 0.55)], fill=color)
        else:
            draw.polygon([p(0.5, 0.5), p(0.3, 0.78), p(0.7, 0.78)], fill=color)
    elif symbol in ("play", "game"):
        if symbol == "game":
            draw.rounded_rectangle((p(0.05, 0.25), p(0.95, 0.75)), radius=side * 0.18, outline=color, width=stroke)
            draw.line([p(0.2, 0.5), p(0.4, 0.5)], fill=color, width=stroke)
            draw.line([p(0.3, 0.4), p(0.3, 0.6)], fill=color, width=stroke)
            draw.ellipse((p(0.62, 0.38), p(0.72, 0.48)), fill=color)
            draw.ellipse((p(0.72, 0.52), p(0.82, 0.62)), fill=color)
        else:
            draw.polygon([p(0.25, 0.12), p(0.85, 0.5), p(0.25, 0.88)], fill=color)
    elif symbol == "next":
        draw.polygon([p(0.2, 0.15), p(0.65, 0.5), p(0.2, 0.85)], fill=color)
        draw.rectangle((p(0.7, 0.15), p(0.82, 0.85)), fill=color)
    elif symbol == "previous":
        draw.polygon([p(0.8, 0.15), p(0.35, 0.5), p(0.8, 0.85)], fill=color)
        draw.rectangle((p(0.18, 0.15), p(0.3, 0.85)), fill=color)
    elif symbol == "profile":
        for fx in (0.1, 0.55):
            for fy in (0.1, 0.55):
                draw.rectangle((p(fx, fy), p(fx + 0.35, fy + 0.35)), outline=color, width=max(1, stroke // 2))
    elif symbol == "script":
        draw.rectangle((p(0.05, 0.15), p(0.95, 0.85)), outline=color, width=max(1, stroke // 2))
        draw.line([p(0.2, 0.35), p(0.4, 0.5), p(0.2, 0.65)], fill=color, width=stroke)
        draw.line([p(0.48, 0.68), p(0.78, 0.68)], fill=color, width=stroke)
    elif symbol == "star":
        points = []
        for index in range(10):
            angle = math.radians(-90 + index * 36)
            radius = 0.48 if index % 2 == 0 else 0.2
            points.append((cx + side * radius * math.cos(angle), cy + side * radius * math.sin(angle)))
        draw.polygon(points, fill=color)
