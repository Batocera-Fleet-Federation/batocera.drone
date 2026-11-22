"""The ``assets`` record of a backup: which games and media the source Drone had.

A backup archive holds configuration, never the games themselves. Next to the
configuration every (non-snapshot) backup records a *wish list* of the assets the
source Drone held, so the Drone that restores it can ask the swarm for whatever is
missing (``backups/asset_restore.py``).

Storage (version 1). The list can be large (hundreds of thousands of ROMs), and the
manifest is also served to peers (``PEER_MANIFEST_MAX_BYTES``) and copied into a sidecar,
so the entries do **not** live in the manifest. They are one gzip-JSON archive member,
``assets.json.gz``, written between the data members and ``manifest.json``. The manifest
only carries a small ``assets`` summary (counts, sizes, ``truncated``, the member's
SHA-256 and size); ``archive.verify_archive`` requires the member to match that summary
exactly. The member is not a file of the backup (it is not in ``files``), so the
"unlisted member" rules for ``data/`` members are unchanged. A local copy of the same
bytes is kept as ``<backup_id>.assets.json.gz`` beside the manifest sidecar so planning
never has to read the archive.

Decoded content::

    {"version": 1, "truncated": false,
     "roms":   [{"system", "path", "type": "file"|"folder", "unit"?, "marker"?, "size",
                 "fp"?, "name"?, "media"?: [gamelist media fields]}],
     "bios":   [{"path", "size", "md5"?}],
     "movies": [{"path", "size", "fp"?}],
     "music":  [{"path", "size", "fp"?}]}

Everything here is **untrusted** once it came from a peer: ``sanitize`` keeps only
entries with plain system tokens, safe relative paths and well-formed hex digests, caps
every list, and the fingerprints are only ever *matching hints* -- content is verified
by the transfer path (signed inventory + full SHA-256 / BIOS MD5), never by this file.

Older backups have no ``assets`` summary; ``legacy_from_gamelists`` rebuilds a path-only
ROM list from the gamelists they archived (``xml.parsers.expat`` rejecting any DTD/entity).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import zlib
import xml.etree.ElementTree as ET
import xml.parsers.expat as expat
from typing import Any, Dict, Iterable, List, Optional

try:
    from ..roms.gamelist import ARTWORK_FIELDS
    from ..storage import movies_store as _movies_store
    from ..storage import music_store as _music_store
    from ..storage import rom_metadata_store as _rom_store
except ImportError:  # pragma: no cover - flat execution
    from roms.gamelist import ARTWORK_FIELDS  # type: ignore
    from storage import movies_store as _movies_store  # type: ignore
    from storage import music_store as _music_store  # type: ignore
    from storage import rom_metadata_store as _rom_store  # type: ignore

ASSETS_VERSION = 1
ASSETS_MEMBER = "assets.json.gz"
KINDS = ("roms", "bios", "movies", "music")
MAX_ENTRIES_PER_KIND = 200_000
# Bounds on the member (compressed) and its decoded JSON, so a hostile archive cannot
# make a restoring Drone allocate without limit.
MAX_MEMBER_BYTES = 96 * 1024 * 1024
MAX_JSON_BYTES = 384 * 1024 * 1024
MAX_GAMELIST_BYTES = 64 * 1024 * 1024
MAX_GAMELIST_ENTRIES = MAX_ENTRIES_PER_KIND

SHA256 = re.compile(r"^[0-9a-f]{64}$")
DIGEST = re.compile(r"^[0-9a-f]{32}$")  # sample-fp-v1 and BIOS MD5 are both 32 hex
SYSTEM_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_ROM_TYPES = ("file", "folder")


def clean_system(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and SYSTEM_TOKEN.match(value) and value not in (".", "..") else None


def clean_relative(value: Any) -> Optional[str]:
    """A normalized relative POSIX path (no absolute, ``..``, empty parts, controls), or None."""
    if not isinstance(value, str) or not value or len(value) > 1024 or "\\" in value:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value) or value.startswith("/"):
        return None
    parts = value.split("/")
    if len(parts) > 32 or any(part in ("", ".", "..") for part in parts):
        return None
    if any(part.startswith(".drone-") for part in parts) or re.match(r"^[A-Za-z]:", parts[0]):
        return None
    return value


def _int(value: Any) -> int:
    return value if type(value) is int and 0 <= value < 1 << 50 else 0


def _digest(value: Any) -> Optional[str]:
    return value.lower() if isinstance(value, str) and DIGEST.match(value.lower()) else None


def _name(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    return "".join(ch for ch in text if ord(ch) >= 32 and ord(ch) != 127)[:200]


# ------------------------------------------------------------------- sanitizing

def _clean_rom(raw: Any) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    system, path = clean_system(raw.get("system")), clean_relative(raw.get("path"))
    if system is None or path is None:
        return None
    kind = raw.get("type") if raw.get("type") in _ROM_TYPES else "file"
    entry: Dict[str, Any] = {"system": system, "path": path, "type": kind, "size": _int(raw.get("size"))}
    if kind == "folder":
        unit, marker = clean_relative(raw.get("unit")), clean_relative(raw.get("marker"))
        if unit:
            entry["unit"] = unit
        if marker:
            entry["marker"] = marker
    digest = _digest(raw.get("fp"))
    if digest:
        entry["fp"] = digest
    name = _name(raw.get("name"))
    if name:
        entry["name"] = name
    media = raw.get("media")
    if isinstance(media, list):
        fields = [field for field in ARTWORK_FIELDS if field in media]
        if fields:
            entry["media"] = fields
    return entry


def _clean_flat(raw: Any, digest_key: str) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    path = clean_relative(raw.get("path"))
    if path is None:
        return None
    entry: Dict[str, Any] = {"path": path, "size": _int(raw.get("size"))}
    digest = _digest(raw.get(digest_key))
    if digest:
        entry[digest_key] = digest
    return entry


def sanitize(raw: Any) -> dict:
    """A clean copy of an (untrusted) assets document; malformed entries are dropped and
    every list is capped. Raises ``ValueError`` only when the document itself is unusable."""
    if not isinstance(raw, dict) or raw.get("version") != ASSETS_VERSION:
        raise ValueError("Unsupported asset record.")
    truncated = raw.get("truncated") is True
    out: Dict[str, Any] = {"version": ASSETS_VERSION}
    seen_any_overflow = False
    cleaners = {"roms": _clean_rom, "bios": lambda item: _clean_flat(item, "md5"),
                "movies": lambda item: _clean_flat(item, "fp"), "music": lambda item: _clean_flat(item, "fp")}
    for kind in KINDS:
        values = raw.get(kind)
        values = values if isinstance(values, list) else []
        if len(values) > MAX_ENTRIES_PER_KIND:
            seen_any_overflow = True
            values = values[:MAX_ENTRIES_PER_KIND]
        clean, seen = [], set()
        for item in values:
            entry = cleaners[kind](item)
            if entry is None:
                continue
            identity = (entry.get("system", ""), entry["path"].lower())
            if identity in seen:
                continue
            seen.add(identity)
            clean.append(entry)
        out[kind] = clean
    out["truncated"] = truncated or seen_any_overflow
    return out


# ------------------------------------------------------------ encode / decode

def encode(assets: dict) -> bytes:
    payload = json.dumps(assets, separators=(",", ":")).encode("utf-8")
    return gzip.compress(payload, compresslevel=6, mtime=0)


def decode(blob: bytes) -> dict:
    """Decode and sanitize a gzip-JSON assets member (bounded; raises ``ValueError``)."""
    if not isinstance(blob, (bytes, bytearray)) or len(blob) > MAX_MEMBER_BYTES:
        raise ValueError("The asset record is too large.")
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        payload = inflater.decompress(bytes(blob), MAX_JSON_BYTES + 1)
        if inflater.unconsumed_tail or len(payload) > MAX_JSON_BYTES:
            raise ValueError("The asset record is too large.")
        if not inflater.eof:
            raise ValueError("The asset record is damaged.")
        return sanitize(json.loads(payload.decode("utf-8")))
    except (zlib.error, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("The asset record is damaged.") from error


def package(assets: dict) -> tuple:
    """``(blob, summary)`` for a sanitized assets document."""
    blob = encode(assets)
    return blob, summarize(assets, blob)


def summarize(assets: dict, blob: bytes) -> dict:
    counts = {kind: len(assets.get(kind) or []) for kind in KINDS}
    sizes = {kind: sum(int(item.get("size") or 0) for item in assets.get(kind) or []) for kind in KINDS}
    return {
        "version": ASSETS_VERSION,
        "member": ASSETS_MEMBER,
        "sha256": hashlib.sha256(blob).hexdigest(),
        "size": len(blob),
        "counts": counts,
        "bytes": sizes,
        "truncated": bool(assets.get("truncated")),
        "media_roms": sum(1 for item in assets.get("roms") or [] if item.get("media")),
    }


def clean_summary(raw: Any) -> Optional[dict]:
    """The manifest's ``assets`` summary, sanitized; ``None`` when absent or not understood
    (an older backup, or one from a newer Drone -- the game list fallback applies)."""
    if not isinstance(raw, dict) or raw.get("version") != ASSETS_VERSION:
        return None
    sha, size = raw.get("sha256"), raw.get("size")
    if not isinstance(sha, str) or not SHA256.match(sha) or type(size) is not int or not 0 < size <= MAX_MEMBER_BYTES:
        return None
    counts = raw.get("counts") if isinstance(raw.get("counts"), dict) else {}
    byte_totals = raw.get("bytes") if isinstance(raw.get("bytes"), dict) else {}
    return {
        "version": ASSETS_VERSION,
        "member": ASSETS_MEMBER,
        "sha256": sha,
        "size": size,
        "counts": {kind: min(_int(counts.get(kind)), MAX_ENTRIES_PER_KIND) for kind in KINDS},
        "bytes": {kind: _int(byte_totals.get(kind)) for kind in KINDS},
        "truncated": raw.get("truncated") is True,
        "media_roms": min(_int(raw.get("media_roms")), MAX_ENTRIES_PER_KIND),
    }


def check_member(summary: Optional[dict], member_size: Optional[int], member_sha256: Optional[str]) -> None:
    """Archive verification: the member and the manifest summary must agree exactly."""
    if summary is None and member_size is None:
        return
    if summary is None or member_size is None or summary["size"] != member_size or summary["sha256"] != member_sha256:
        raise ValueError("The backup's asset record does not match its manifest.")


# ----------------------------------------------------------------- collecting

def collect(settings: Any) -> dict:
    """The asset record of this Drone, from its local caches (nothing is hashed or read from disk
    beyond the cache databases). A cache that is not ready yet contributes nothing."""
    assets: Dict[str, Any] = {"version": ASSETS_VERSION, "roms": [], "bios": [], "movies": [], "music": [],
                              "truncated": False}
    truncated = False
    page = _rom_store.list_rom_asset_rows(settings, limit=MAX_ENTRIES_PER_KIND)
    if page is not None:
        rows = page["rows"]
        if len(rows) > MAX_ENTRIES_PER_KIND:
            rows, truncated = rows[:MAX_ENTRIES_PER_KIND], True
        for system, path, size, entry_type, fingerprint, name, extra_json in rows:
            try:
                extra = json.loads(extra_json or "{}")
            except ValueError:
                extra = {}
            extra = extra if isinstance(extra, dict) else {}
            entry: Dict[str, Any] = {"system": system, "path": path, "type": "folder" if entry_type == "folder" else "file",
                                     "size": size, "fp": fingerprint, "name": name}
            if entry["type"] == "folder":
                entry["unit"] = extra.get("transfer_unit_path")
                entry["marker"] = extra.get("marker_relative_path")
            if (system, path) in page["has_entry"]:
                missing = page["missing"].get((system, path), set())
                entry["media"] = [field for field in ARTWORK_FIELDS if field not in missing]
            assets["roms"].append(entry)
    bios = _rom_store.list_bios_asset_rows(settings)
    for path, size, md5 in (bios or [])[:MAX_ENTRIES_PER_KIND]:
        assets["bios"].append({"path": path, "size": size, "md5": md5})
    truncated = truncated or len(bios or []) > MAX_ENTRIES_PER_KIND
    for kind, loader, root, digest_keys in (
        ("movies", _movies_store.list_movies, getattr(settings, "movies_root", None), ("movies_fingerprint", "fingerprint")),
        ("music", _music_store.list_music, getattr(settings, "music_root", None), ("music_fingerprint", "fingerprint")),
    ):
        if root is None:
            continue
        try:
            rows = loader(root)
        except Exception:  # noqa: BLE001 - a missing media cache never blocks a backup
            rows = []
        truncated = truncated or len(rows) > MAX_ENTRIES_PER_KIND
        for row in rows[:MAX_ENTRIES_PER_KIND]:
            fingerprint = next((row.get(key) for key in digest_keys if row.get(key)), None)
            assets[kind].append({"path": row.get("file_path"), "size": row.get("file_size"), "fp": fingerprint})
    assets["truncated"] = truncated
    return sanitize(assets)


# ---------------------------------------------------------------- legacy lists

def _parse_xml_no_dtd(data: bytes) -> ET.Element:
    """Parse XML bytes with expat while forbidding any DTD, DOCTYPE, or ENTITY construct.

    Rejecting via expat handlers rather than scanning raw bytes applies uniformly across
    every XML encoding expat supports (UTF-8, UTF-16 with BOM, ISO-8859-1, etc.).
    """
    parser = expat.ParserCreate(None, "}")
    target = ET.TreeBuilder()

    def _forbid(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("DTD declarations are not allowed in gamelist.xml")

    parser.StartDoctypeDeclHandler = _forbid
    parser.EndDoctypeDeclHandler = _forbid
    parser.EntityDeclHandler = _forbid
    parser.UnparsedEntityDeclHandler = _forbid
    parser.NotationDeclHandler = _forbid
    parser.ExternalEntityRefHandler = _forbid
    parser.ElementDeclHandler = _forbid
    parser.AttlistDeclHandler = _forbid
    parser.SkippedEntityHandler = _forbid
    parser.buffer_text = 1

    def _start(tag: str, attrs: Dict[str, str]) -> None:
        if "}" in tag:
            tag = "{" + tag
        if attrs:
            attrs = {("{" + k if "}" in k else k): v for k, v in attrs.items()}
        target.start(tag, attrs)

    def _end(tag: str) -> None:
        if "}" in tag:
            tag = "{" + tag
        target.end(tag)

    parser.StartElementHandler = _start
    parser.EndElementHandler = _end
    parser.CharacterDataHandler = target.data
    parser.Parse(data, True)
    return target.close()


def legacy_from_gamelists(members: Iterable) -> dict:
    """A path-only ROM list from archived ``roms/<system>/gamelist.xml`` files.

    ``members`` yields ``(tar member, file object)`` like ``archive.iter_members``. Files are
    read with a size cap, any DTD/DOCTYPE/ENTITY declaration rejects the whole file in any
    encoding, and parse errors skip the file.
    """
    roms: List[dict] = []
    truncated = False
    for member, handle in members:
        name = str(getattr(member, "name", ""))
        parts = name.split("/")
        if handle is None or len(parts) != 4 or parts[:2] != ["data", "roms"] or parts[3] != "gamelist.xml":
            continue
        system = clean_system(parts[2])
        if system is None or int(getattr(member, "size", 0) or 0) > MAX_GAMELIST_BYTES:
            continue
        data = handle.read(MAX_GAMELIST_BYTES + 1)
        if len(data) > MAX_GAMELIST_BYTES:
            continue
        try:
            root = _parse_xml_no_dtd(data)
        except (expat.ExpatError, ET.ParseError, ValueError):
            continue
        for game in root.findall("game"):
            path_text = (game.findtext("path") or "").strip().replace("\\", "/")
            while path_text.startswith("./"):
                path_text = path_text[2:]
            path = clean_relative(path_text)
            if path is None:
                continue
            if len(roms) >= MAX_GAMELIST_ENTRIES:
                truncated = True
                break
            entry: Dict[str, Any] = {"system": system, "path": path, "type": "file", "size": 0,
                                     "name": _name(game.findtext("name") or "")}
            media = [field for field in ARTWORK_FIELDS if (game.findtext(field) or "").strip()]
            if media:
                entry["media"] = media
            roms.append(entry)
    assets = sanitize({"version": ASSETS_VERSION, "roms": roms, "bios": [], "movies": [], "music": [],
                       "truncated": truncated})
    assets["legacy"] = True
    return assets
