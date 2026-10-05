"""Uploaded button images: validation and storage of originals.

Uploads are checked four ways before anything is stored: declared MIME type,
file extension, a structural parse of the actual bytes (PNG chunk CRCs, JPEG
frame header, WebP RIFF/VP8 header -- stdlib only, so it works before the
tooling exists), and size/dimension limits. When tooling is installed the
manager additionally decodes the file with Pillow in a subprocess. Originals
are stored under ``images/<32-hex-id>.<ext>`` and are never executed; device-
sized versions are rendered separately into ``rendered/`` by the worker.
"""

import struct
import time
import zlib
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

from .config import clean_text, hex_id, new_id
from .paths import StreamDeckPaths, atomic_write_bytes, atomic_write_json, read_json


MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_DIMENSION = 4096
EXTENSIONS = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "webp": "webp"}
MIME_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}


def _png(data: bytes) -> Tuple[int, int]:
    offset, width, height, saw_end = 8, 0, 0, False
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise ValueError("truncated PNG chunk")
        chunk = data[offset + 8:offset + 8 + length]
        if zlib.crc32(kind + chunk) & 0xFFFFFFFF != struct.unpack(">I", data[offset + 8 + length:end])[0]:
            raise ValueError("corrupt PNG (bad checksum)")
        if offset == 8:
            if kind != b"IHDR" or length != 13:
                raise ValueError("PNG must start with an IHDR chunk")
            width, height = struct.unpack(">II", chunk[:8])
        if kind == b"IEND":
            saw_end = True
            break
        offset = end
    if not saw_end:
        raise ValueError("incomplete PNG image")
    return width, height


_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg(data: bytes) -> Tuple[int, int]:
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            raise ValueError("invalid JPEG marker structure")
        marker = data[offset + 1]
        if marker == 0xFF:
            offset += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        if marker in (0xD9, 0xDA):
            break
        length = struct.unpack(">H", data[offset + 2:offset + 4])[0]
        if length < 2 or offset + 2 + length > len(data):
            raise ValueError("truncated JPEG segment")
        if marker in _SOF_MARKERS:
            if length < 7:
                raise ValueError("invalid JPEG frame header")
            height, width = struct.unpack(">HH", data[offset + 5:offset + 9])
            return width, height
        offset += 2 + length
    raise ValueError("JPEG has no frame header")


def _webp(data: bytes) -> Tuple[int, int]:
    declared = struct.unpack("<I", data[4:8])[0] + 8
    if declared > len(data) or declared < 30:
        raise ValueError("truncated WebP image")
    chunk = data[12:16]
    if chunk == b"VP8X":
        return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
    if chunk == b"VP8L":
        if data[20] != 0x2F:
            raise ValueError("invalid lossless WebP header")
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            raise ValueError("invalid lossy WebP header")
        return struct.unpack("<H", data[26:28])[0] & 0x3FFF, struct.unpack("<H", data[28:30])[0] & 0x3FFF
    raise ValueError("unsupported WebP bitstream")


def inspect_image(data: bytes) -> Tuple[str, int, int]:
    """(kind, width, height) for a structurally valid PNG/JPEG/WebP; else ValueError."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        kind, (width, height) = "png", _png(data)
    elif data.startswith(b"\xff\xd8\xff"):
        kind, (width, height) = "jpeg", _jpeg(data)
    elif len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind, (width, height) = "webp", _webp(data)
    else:
        raise ValueError("the file is not a PNG, JPEG, or WebP image")
    if not (1 <= width <= MAX_DIMENSION and 1 <= height <= MAX_DIMENSION):
        raise ValueError(f"image dimensions must be between 1 and {MAX_DIMENSION} pixels")
    return kind, width, height


class ImageStore:
    def __init__(self, paths: StreamDeckPaths, verifier: Optional[Callable[[Path], None]] = None) -> None:
        self.paths = paths
        self.verifier = verifier

    def _meta_path(self, image_id: str) -> Path:
        return self.paths.owned("images", f"{hex_id(image_id, 'image id')}.json")

    def save_upload(self, filename: str, data: bytes, declared_type: str = "") -> dict:
        if not data:
            raise ValueError("the uploaded image is empty")
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("images must be 5 MiB or smaller")
        extension = Path(str(filename or "")).suffix.lower().lstrip(".")
        expected = EXTENSIONS.get(extension)
        if expected is None:
            raise ValueError("only .png, .jpg, .jpeg, and .webp files are accepted")
        kind, width, height = inspect_image(data)
        if kind != expected:
            raise ValueError("the file extension does not match the image content")
        declared = str(declared_type or "").split(";")[0].strip().lower()
        if declared and declared not in ("application/octet-stream", MIME_TYPES[kind]):
            raise ValueError("the declared content type does not match the image content")
        image_id = new_id()
        self.paths.ensure(self.paths.images_dir)
        target = self.paths.owned("images", f"{image_id}.{'jpg' if kind == 'jpeg' else kind}")
        atomic_write_bytes(target, data)
        if self.verifier is not None:
            try:
                self.verifier(target)
            except Exception as error:  # noqa: BLE001
                target.unlink(missing_ok=True)
                raise ValueError(f"the image could not be decoded: {error}") from error
        meta = {
            "id": image_id, "type": kind, "content_type": MIME_TYPES[kind], "file": target.name,
            "width": width, "height": height, "size": len(data),
            "original_name": clean_text(Path(str(filename)).name, 120),
            "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        atomic_write_json(self._meta_path(image_id), meta)
        return meta

    def get(self, image_id: str) -> Tuple[Path, dict]:
        meta = read_json(self._meta_path(image_id), None)
        if not isinstance(meta, dict):
            raise KeyError("image not found")
        kind = meta.get("type")
        if kind not in MIME_TYPES:
            raise KeyError("image not found")
        path = self.paths.owned("images", f"{hex_id(image_id)}.{'jpg' if kind == 'jpeg' else kind}")
        if not path.is_file() or path.is_symlink():
            raise KeyError("image not found")
        return path, meta

    def path(self, image_id: str) -> Optional[Path]:
        try:
            return self.get(image_id)[0]
        except (KeyError, ValueError):
            return None

    def prune(self, referenced: Iterable[str], *, older_than_seconds: float = 3600.0) -> List[str]:
        """Delete unreferenced uploads older than the grace period (unsaved edits keep theirs)."""
        keep = set(referenced)
        removed = []
        cutoff = time.time() - older_than_seconds
        try:
            entries = list(self.paths.images_dir.glob("*.json"))
        except OSError:
            return removed
        for meta_path in entries:
            image_id = meta_path.stem
            try:
                hex_id(image_id)
                if image_id in keep or meta_path.stat().st_mtime > cutoff:
                    continue
                path, _meta = self.get(image_id)
                path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                removed.append(image_id)
            except (KeyError, ValueError, OSError):
                continue
        return removed
