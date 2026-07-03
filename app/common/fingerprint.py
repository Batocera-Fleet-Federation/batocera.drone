"""Content fingerprinting for cross-drone file identity (``sample-fp-v1``).

Extracted from ``drone_api.py`` (and de-duplicated against ``saves_store.py``) so
the sampled-hash algorithm lives in exactly one place. ROM and save files share
this identity, so the same bytes yield the same fingerprint whether a file is
scanned as a ROM or as a save. BIOS files instead use a full-file MD5
(:func:`build_md5`) for exact No-Intro / MAME matching.

``RomRepository`` keeps thin ``build_*`` static-method wrappers around these
functions so its existing call sites — and the tests that monkeypatch
``RomRepository.build_fingerprint`` — keep working.

Pure stdlib, no Drone-internal dependencies.
"""

import hashlib
import os
from pathlib import Path
from typing import Tuple

FINGERPRINT_ALGORITHM = "sample-fp-v1"
# A sampled hash (size + fixed head/middle/tail windows) replaces a full-file
# hash so disc images are not read end to end. Constant cost per file.
FINGERPRINT_SAMPLE_BYTES = max(4096, int(os.environ.get("ROM_METADATA_FINGERPRINT_SAMPLE_BYTES", str(64 * 1024))))
# Files at or below this size are fingerprinted whole (exact); larger files use
# the three sample windows. Keep >= 3x the sample size so windows never overlap.
FINGERPRINT_SMALL_FILE_BYTES = max(3 * FINGERPRINT_SAMPLE_BYTES, int(os.environ.get("ROM_METADATA_FINGERPRINT_SMALL_FILE_BYTES", str(3 * FINGERPRINT_SAMPLE_BYTES))))


def build_unique_id(path: Path) -> str:
    resolved = path.resolve()
    stat = resolved.stat()
    raw = f"{resolved}|{stat.st_size}|{int(stat.st_mtime)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_fingerprint(path: Path) -> str:
    """Content fingerprint for cross-drone file identity (``sample-fp-v1``).

    Hashes the file size plus up to three fixed 64 KB windows (head, middle,
    tail). Files at or below the small-file threshold are hashed whole, so
    small files are exact. Cost is constant regardless of file size, which is
    what lets us fingerprint multi-GB disc images without reading them end to
    end. Folding the size into the digest means two files of different size can
    never collide. This is the imohash approach used by file-sync tools: it is
    not a cryptographic hash, but for "is this the same file on another drone?"
    the collision probability is negligible. Deterministic across drones."""
    size = int(path.stat().st_size)
    digest = hashlib.md5()
    digest.update(size.to_bytes(8, "little"))
    with path.open("rb") as handle:
        if size <= FINGERPRINT_SMALL_FILE_BYTES:
            digest.update(handle.read())
        else:
            digest.update(handle.read(FINGERPRINT_SAMPLE_BYTES))
            handle.seek(max(0, size // 2 - FINGERPRINT_SAMPLE_BYTES // 2))
            digest.update(handle.read(FINGERPRINT_SAMPLE_BYTES))
            handle.seek(size - FINGERPRINT_SAMPLE_BYTES)
            digest.update(handle.read(FINGERPRINT_SAMPLE_BYTES))
    return digest.hexdigest()


def build_md5(path: Path) -> str:
    """Full-file MD5 — used for BIOS identity.

    BIOS files must be matched exactly against known-good dumps (No-Intro / MAME
    BIOS sets), so unlike ROMs they use a true content MD5 rather than the sampled
    fingerprint. BIOS files are small, so reading them whole is cheap."""
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_directory_stats(path: Path) -> Tuple[int, int]:
    total_size = 0
    latest_mtime = int(path.stat().st_mtime)
    for child in path.rglob("*"):
        if not child.is_file():
            continue
        stat = child.stat()
        total_size += int(stat.st_size)
        latest_mtime = max(latest_mtime, int(stat.st_mtime))
    return total_size, latest_mtime
