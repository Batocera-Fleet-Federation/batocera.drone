"""RomRequestHandler multi-Drone media browsing, as a mixin.

Browse movies/shows, music and games across this Drone **and** every paired
peer from one UI.  Routes live under ``/v1/api/media-browse/...`` and are
session-gated like the local ``/movies`` and ``/music`` browsing routes (the
write route -- liking a track -- is a POST like ``/music/{key}/like``).

* Listing fans out to every source in parallel (``transfer/media_browse.py``)
  and returns one merged, cursor-paged list.  Every row carries ``drone_id`` /
  ``drone_name`` so the UI can badge it.  An offline peer degrades to a status
  entry in ``sources`` instead of failing the page.
* Playback and artwork of a peer's item are **proxied through this Drone**
  over the pinned mTLS client (``_peer_open_stream_for_peer``) with ``Range``
  forwarded for seeking.  Nothing is written to disk and the browser never
  talks to a peer.
* Copying an item here reuses ``POST /admin/local-network/sync`` -- the same
  path Swarm > Transfers uses -- so a game arrives with all of its artwork in
  the right system folder.
* Likes on a peer's tracks are recorded only on this Drone
  (``peer_music_likes``) and never sent to the peer.

All peer data is untrusted: keys are validated, paths are never taken from the
browser for peer fetches, and responses are size-capped.
"""

from __future__ import annotations

import re
import socket
import time
from typing import Dict, List, Optional
from urllib.error import HTTPError
from urllib.parse import quote, unquote

try:
    from ..storage import movies_store as _movies_store
    from ..storage import music_store as _music_store
    from ..transfer import local_network as _local_network
    from ..transfer import media_browse as _media_browse
    from ..transfer.peer_connectivity import (
        PeerIdentityHold,
        _peer_get_json_for_peer,
        _peer_open_stream_for_peer,
    )
except ImportError:  # pragma: no cover - direct script execution fallback
    from storage import movies_store as _movies_store  # type: ignore
    from storage import music_store as _music_store  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from transfer import media_browse as _media_browse  # type: ignore
    from transfer.peer_connectivity import (  # type: ignore
        PeerIdentityHold,
        _peer_get_json_for_peer,
        _peer_open_stream_for_peer,
    )

_ENTRY_KEY_RE = re.compile(r"^[0-9a-f]{1,64}$")
_DRONE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_ARTWORK_FIELDS = {
    "movies": {"poster", "backdrop"},
    "music": {"art", "artist"},
}
_STREAMABLE = {"movies", "music"}
# Browsing a library must feel quick: far shorter than a transfer's timeout.
BROWSE_PEER_TIMEOUT_SECONDS = 15.0
BROWSE_PEER_BUDGET_SECONDS = 20.0
MAX_ARTWORK_BYTES = 20 * 1024 * 1024
_PATH_CACHE_TTL_SECONDS = 300.0
_path_cache: Dict[tuple, tuple] = {}
# One process-wide cache of peers' inventory pages (see media_browse.SourceCache).
_source_cache = _media_browse.SourceCache()


class HandlersMediaBrowseMixin:
    # ------------------------------------------------------------ sources

    def _media_sources(self, only: Optional[set] = None) -> List["_media_browse.MediaSource"]:
        sources = [
            _media_browse.MediaSource(
                drone_id=str(self.settings.device_id),
                name=self._media_local_name(),
                is_local=True,
            )
        ]
        if _local_network.is_local_mode(self.settings):
            for peer in _local_network.paired_peers(self.settings):
                peer_id = str(peer.get("drone_id") or peer.get("device_id") or "").strip()
                if not peer_id or not _DRONE_ID_RE.match(peer_id):
                    continue
                name = str(peer.get("name") or peer.get("hostname") or peer_id)
                sources.append(_media_browse.MediaSource(drone_id=peer_id, name=name))
        if only:
            sources = [source for source in sources if source.drone_id in only]
        return _media_browse.order_sources(sources)

    def _media_local_name(self) -> str:
        try:
            return socket.gethostname() or str(self.settings.device_id)
        except Exception:  # noqa: BLE001
            return str(self.settings.device_id)

    def _media_peer(self, drone_id: str) -> Optional[dict]:
        if not _DRONE_ID_RE.match(str(drone_id or "")):
            return None
        if not _local_network.is_local_mode(self.settings):
            return None
        return _local_network.get_paired_peer(self.settings, drone_id)

    def _media_with_local_flags(self, kind: str, result):
        """``exists_locally`` depends on *this* Drone, so it is recomputed per
        request and never cached with the peer's page."""
        if kind == "roms" and isinstance(result, dict):
            result = {**result, "items": [dict(item) for item in (result.get("items") or []) if isinstance(item, dict)]}
            self._annotate_roms_exist_locally(result["items"])
        return result

    def _media_fetch(self, kind: str, params: dict, refresh: bool = False):
        """Build the per-source ``fetch(source, offset, limit)`` callable."""

        def fetch(source, offset: int, limit: int) -> dict:
            query_params = {key: [str(value)] for key, value in params.items() if value not in (None, "")}
            query_params["limit"] = [str(limit)]
            query_params["offset"] = [str(offset)]
            if source.is_local:
                return self._collect_peer_inventory(kind, query_params)
            peer = self._media_peer(source.drone_id)
            if peer is None:
                raise OSError("peer is no longer paired")
            cache_key = (source.drone_id, kind, params.get("q") or "", params.get("system") or "", offset, limit)
            if not refresh:
                hit = _source_cache.get(cache_key)
                if hit is not None:
                    return self._media_with_local_flags(kind, dict(hit[0]))
            try:
                result = self._peer_inventory_request(
                    peer, source.drone_id, kind, query_params,
                )
            except HTTPError as error:
                if error.code in (400, 404):
                    # The peer predates this asset type (or does not serve it).
                    raise _media_browse.SourceUnsupported() from error
                stale = _source_cache.get(cache_key, allow_stale=True)
                if stale is not None:
                    return self._media_with_local_flags(kind, {**stale[0], "stale": True, "age": stale[1]})
                raise
            except (socket.timeout, TimeoutError) as error:
                stale = _source_cache.get(cache_key, allow_stale=True)
                if stale is not None:
                    return self._media_with_local_flags(kind, {**stale[0], "stale": True, "age": stale[1]})
                raise _media_browse.SourceTimeout() from error
            except PeerIdentityHold:
                raise
            except Exception:
                # Unreachable / TLS / transient failure: fall back to the last good copy.
                stale = _source_cache.get(cache_key, allow_stale=True)
                if stale is not None:
                    return self._media_with_local_flags(kind, {**stale[0], "stale": True, "age": stale[1]})
                raise
            if isinstance(result, dict):
                _source_cache.put(cache_key, result)
            return self._media_with_local_flags(kind, dict(result) if isinstance(result, dict) else result)

        return fetch

    # --------------------------------------------------------------- list

    def _handle_media_browse_list(self, kind: str, query_params: dict) -> None:
        if kind not in _media_browse.MEDIA_TYPES:
            self._send_json(404, {"error": "unknown media type"})
            return
        q = str((query_params.get("q") or [""])[0]).strip()[:200]
        system = str((query_params.get("system") or [""])[0]).strip()[:128]
        cursor = str((query_params.get("cursor") or [""])[0])
        limit = _media_browse.clamp_limit((query_params.get("limit") or [None])[0])
        only = {
            part.strip() for part in str((query_params.get("drones") or [""])[0]).split(",") if part.strip()
        } or None
        sources = self._media_sources(only)
        params = {"q": q, "system": system if kind == "roms" else ""}
        refresh = str((query_params.get("refresh") or [""])[0]).strip().lower() in {"1", "true", "yes"}
        if refresh:
            for source in sources:
                if not source.is_local:
                    _source_cache.invalidate(source.drone_id)
        page = _media_browse.collect_page(
            sources,
            self._media_fetch(kind, params, refresh=refresh),
            limit=limit,
            cursor=cursor,
        )
        if kind == "music":
            self._media_overlay_likes(page["items"])
        self._send_json(200, {"media_type": kind, **page})

    def _media_overlay_likes(self, items: list) -> None:
        """``liked`` comes only from this Drone's own stores."""
        try:
            local_keys = _music_store.list_liked_music_keys(self.settings.music_root)
        except Exception:  # noqa: BLE001
            local_keys = set()
        peer_keys: Dict[str, set] = {}
        for item in items:
            key = str(item.get("entry_key") or "")
            if item.get("is_local"):
                item["liked"] = key in local_keys
                continue
            drone_id = str(item.get("drone_id") or "")
            if drone_id not in peer_keys:
                try:
                    peer_keys[drone_id] = _music_store.list_peer_music_liked_keys(self.settings.music_root, drone_id)
                except Exception:  # noqa: BLE001
                    peer_keys[drone_id] = set()
            item["liked"] = key in peer_keys[drone_id]

    # ------------------------------------------------------------- detail

    def _local_media_detail(self, kind: str, entry_key: str) -> Optional[dict]:
        """Detail payload for one of *this* Drone's movies/tracks."""
        if kind == "movies":
            movie = _movies_store.get_movie_by_key(self.settings.movies_root, entry_key)
            if not movie:
                return None
            movie.pop("absolute_path", None)
            metadata = _movies_store.get_movie_metadata(self.settings.movies_root, entry_key)
            movie["display_title"] = (metadata or {}).get("title") or movie.get("movie_name") or ""
            movie["metadata"] = metadata
            self._enrich_media_items("movies", [movie])
            return movie
        track = _music_store.get_music_by_key(self.settings.music_root, entry_key)
        if not track:
            return None
        track.pop("absolute_path", None)
        metadata = _music_store.get_music_metadata(self.settings.music_root, entry_key)
        self._enrich_media_items("music", [track])
        track["metadata"] = metadata
        track["liked"] = _music_store.is_music_liked(self.settings.music_root, entry_key)
        return track

    def _handle_media_browse_detail(self, kind: str, drone_id: str, entry_key: str) -> None:
        if kind not in _STREAMABLE or not _ENTRY_KEY_RE.match(entry_key or ""):
            self._send_json(404, {"error": "not found"})
            return
        if drone_id == str(self.settings.device_id):
            item = self._local_media_detail(kind, entry_key)
            name, is_local = self._media_local_name(), True
        else:
            peer = self._media_peer(drone_id)
            if peer is None:
                self._send_json(404, {"error": "unknown Drone"})
                return
            try:
                item, _ = _peer_get_json_for_peer(
                    peer, f"/v1/api/peer/media-detail/{kind}/{entry_key}", self.settings,
                    peer_id=drone_id, config={"network_mode": "local_network"},
                    timeout=BROWSE_PEER_TIMEOUT_SECONDS,
                    overall_deadline=time.monotonic() + BROWSE_PEER_BUDGET_SECONDS,
                )
            except PeerIdentityHold:
                self._send_json(503, {"error": "swarm networking is held pending Drone identity verification"})
                return
            except HTTPError as error:
                self._send_json(404 if error.code == 404 else 502, {"error": "Drone could not provide this item"})
                return
            except Exception as error:  # noqa: BLE001 - peer unreachable
                self._send_json(502, {"error": f"Drone unreachable: {str(error)[:120]}"})
                return
            name, is_local = str(peer.get("name") or peer.get("hostname") or drone_id), False
            if isinstance(item, dict) and kind == "music":
                try:
                    item["liked"] = entry_key in _music_store.list_peer_music_liked_keys(
                        self.settings.music_root, drone_id
                    )
                except Exception:  # noqa: BLE001
                    item["liked"] = False
        if not isinstance(item, dict) or not item:
            self._send_json(404, {"error": "not found"})
            return
        item.pop("absolute_path", None)
        item["drone_id"], item["drone_name"], item["is_local"] = drone_id, name, is_local
        self._send_json(200, item)

    def _handle_media_browse_also_on(self, kind: str, query_params: dict) -> None:
        """Other Drones holding the same title as the described item."""
        if kind not in _media_browse.MEDIA_TYPES:
            self._send_json(404, {"error": "unknown media type"})
            return

        def value(name: str) -> str:
            return str((query_params.get(name) or [""])[0]).strip()[:300]

        reference = {
            "fingerprint": value("fingerprint"),
            "display_title": value("title"),
            "artist": value("artist"),
            "system": value("system"),
            "year": value("year"),
        }
        exclude = value("exclude_drone")
        search = reference["fingerprint"] or reference["display_title"]
        if not search:
            self._send_json(200, {"media_type": kind, "copies": [], "sources": []})
            return
        sources = [s for s in self._media_sources() if s.drone_id != exclude]
        params = {"q": search, "system": reference["system"] if kind == "roms" else ""}
        page = _media_browse.collect_page(
            sources, self._media_fetch(kind, params), limit=50,
        )
        copies = [
            item for item in page["items"]
            if _media_browse.same_item(kind, reference, item)
        ]
        self._send_json(200, {"media_type": kind, "copies": copies, "sources": page["sources"]})

    # ------------------------------------------------------- stream / art

    def _media_peer_relative_path(self, peer: dict, kind: str, entry_key: str) -> str:
        cache_key = (str(peer.get("drone_id") or peer.get("device_id") or ""), kind, entry_key)
        cached = _path_cache.get(cache_key)
        now = time.monotonic()
        if cached and cached[1] > now:
            return cached[0]
        detail, _ = _peer_get_json_for_peer(
            peer, f"/v1/api/peer/media-detail/{kind}/{entry_key}", self.settings,
            peer_id=cache_key[0], config={"network_mode": "local_network"},
            timeout=BROWSE_PEER_TIMEOUT_SECONDS,
            overall_deadline=now + BROWSE_PEER_BUDGET_SECONDS,
        )
        rel = str((detail or {}).get("file_path") or "").replace("\\", "/").lstrip("/")
        if not rel or ".." in rel.split("/"):
            raise FileNotFoundError()
        if len(_path_cache) > 2048:
            _path_cache.clear()
        _path_cache[cache_key] = (rel, now + _PATH_CACHE_TTL_SECONDS)
        return rel

    def _handle_media_browse_stream(self, kind: str, drone_id: str, entry_key: str) -> None:
        """Play a movie/track: local file, or a peer's file proxied through here."""
        if kind not in _STREAMABLE or not _ENTRY_KEY_RE.match(entry_key or ""):
            self._send_json(404, {"error": "not found"})
            return
        if drone_id == str(self.settings.device_id):
            if kind == "movies":
                self._handle_movie_stream(entry_key)
            else:
                self._handle_music_stream(entry_key)
            return
        peer = self._media_peer(drone_id)
        if peer is None:
            self._send_json(404, {"error": "unknown Drone"})
            return
        try:
            rel = self._media_peer_relative_path(peer, kind, entry_key)
            endpoint = f"/v1/api/peer/{kind}/{quote(rel, safe='/')}"
            response, _address = _peer_open_stream_for_peer(
                peer, endpoint, self.settings, peer_id=drone_id,
                range_header=self.headers.get("Range"), timeout=BROWSE_PEER_TIMEOUT_SECONDS,
            )
        except PeerIdentityHold:
            self._send_json(503, {"error": "swarm networking is held pending Drone identity verification"})
            return
        except FileNotFoundError:
            self._send_json(404, {"error": "not found"})
            return
        except HTTPError as error:
            if error.code == 416:
                self.send_response(416)
                self.send_header("Content-Range", error.headers.get("Content-Range") or "bytes */0")
                self.send_header("Content-Length", "0")
                self._send_security_headers()
                self.end_headers()
                return
            self._send_json(404 if error.code == 404 else 502, {"error": "Drone could not provide this file"})
            return
        except Exception as error:  # noqa: BLE001 - peer unreachable
            self._send_json(502, {"error": f"Drone unreachable: {str(error)[:120]}"})
            return
        try:
            status = int(getattr(response, "status", 200) or 200)
            self.send_response(status if status in (200, 206) else 200)
            for header in ("Content-Type", "Content-Length", "Content-Range"):
                value = response.headers.get(header)
                if value:
                    self.send_header(header, value)
            self.send_header("Accept-Ranges", "bytes")
            self._send_security_headers(cache_control="no-store")
            self.end_headers()
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break  # viewer seeked or closed the player
        finally:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass

    def _handle_media_browse_artwork(self, kind: str, drone_id: str, entry_key: str, field: str) -> None:
        if kind not in _STREAMABLE or field not in _ARTWORK_FIELDS[kind] or not _ENTRY_KEY_RE.match(entry_key or ""):
            self._send_json(404, {"error": "not found"})
            return
        if drone_id == str(self.settings.device_id):
            if kind == "movies":
                self._handle_movie_artwork(entry_key, field)
            else:
                self._handle_music_artwork(entry_key, field)
            return
        self._proxy_peer_image(drone_id, f"/v1/api/peer/media-artwork/{kind}/{entry_key}/{field}")

    def _handle_media_browse_rom_artwork(self, drone_id: str, system: str, artwork_type: str, rom_path: str) -> None:
        """A peer game's box art / screenshot, proxied (local games already
        have their own artwork routes)."""
        if drone_id == str(self.settings.device_id):
            self._send_json(404, {"error": "use the local artwork route"})
            return
        system, artwork_type = unquote(system), unquote(artwork_type)
        if "/" in system or "/" in artwork_type or not system or not artwork_type:
            self._send_json(400, {"error": "invalid artwork request"})
            return
        rel = unquote(rom_path or "").replace("\\", "/").lstrip("/")
        if not rel or ".." in rel.split("/"):
            self._send_json(400, {"error": "invalid artwork request"})
            return
        self._proxy_peer_image(
            drone_id,
            f"/v1/api/peer/artwork/{quote(system, safe='')}/{quote(artwork_type, safe='')}/{quote(rel, safe='/')}",
        )

    def _proxy_peer_image(self, drone_id: str, endpoint: str) -> None:
        peer = self._media_peer(drone_id)
        if peer is None:
            self._send_json(404, {"error": "unknown Drone"})
            return
        try:
            response, _ = _peer_open_stream_for_peer(
                peer, endpoint, self.settings, peer_id=drone_id, timeout=BROWSE_PEER_TIMEOUT_SECONDS,
            )
        except PeerIdentityHold:
            self._send_json(503, {"error": "swarm networking is held pending Drone identity verification"})
            return
        except HTTPError as error:
            self._send_json(404 if error.code == 404 else 502, {"error": "artwork unavailable"})
            return
        except Exception:  # noqa: BLE001 - peer unreachable
            self._send_json(502, {"error": "Drone unreachable"})
            return
        try:
            content_type = str(response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not content_type.startswith("image/"):
                self._send_json(502, {"error": "unexpected artwork response"})
                return
            body = response.read(MAX_ARTWORK_BYTES + 1)
        finally:
            response.close()
        if len(body) > MAX_ARTWORK_BYTES:
            self._send_json(502, {"error": "artwork too large"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers(cache_control="private, max-age=300")
        self.end_headers()
        self.wfile.write(body)

    # -------------------------------------------------------------- likes

    def _handle_media_browse_like(self, payload: dict) -> None:
        """Like/unlike a track on any Drone.  Always recorded locally only."""
        payload = payload if isinstance(payload, dict) else {}
        drone_id = str(payload.get("drone_id") or "")
        entry_key = str(payload.get("entry_key") or "")
        liked = bool(payload.get("liked", True))
        if not _ENTRY_KEY_RE.match(entry_key):
            self._send_json(400, {"error": "invalid entry_key"})
            return
        if drone_id == str(self.settings.device_id):
            if not _music_store.get_music_by_key(self.settings.music_root, entry_key):
                self._send_json(404, {"error": "unknown track"})
                return
            _music_store.set_music_liked(self.settings.music_root, entry_key, liked)
        else:
            if self._media_peer(drone_id) is None:
                self._send_json(404, {"error": "unknown Drone"})
                return
            _music_store.set_peer_music_liked(self.settings.music_root, drone_id, entry_key, liked)
        self._send_json(200, {"drone_id": drone_id, "entry_key": entry_key, "liked": liked})
