"""Issue #722: Search Assets searches every online Drone without a peer or system first.

Quoted requirements:
- "Rename the tab from \"Request Assets\" to \"Search Assets\"."
- "One search box searches all online paired Drones. Games search across all systems by default."
- "Searching \"zelda\" on Games returns matches from all systems on all online paired Drones,
  each labeled with its system and source Drone, without picking a peer or system first."
- "Movies & Shows and Music tabs search and download the same way. Music lands in the music library."
- "Results load page by page (`cursor`) as the user scrolls to the end of the list.
  Show a per-Drone status line for any Drone that was `offline`/`timeout`/`unsupported`;
  it must never block the results."
- "An empty query lists recent/all items, so browsing still works without typing."
- "Download queues the copy from that row's Drone. Download All queues everything in the
  current results/filter."
- "\"Hide items I have\" maps to `exists_locally` and the non-overwrite behavior."
- "`defer_action` for every network call. Nothing may block the render loop."
"""

from __future__ import annotations

import inspect
import sys
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
PORTS = ROOT / "ports-client"
if str(PORTS) not in sys.path:
    sys.path.insert(0, str(PORTS))


def _ensure_imgui() -> None:
    try:
        import imgui_bundle  # noqa: F401
        return
    except ImportError:
        pass
    from unittest import mock

    imgui = mock.MagicMock()
    imgui.Key.gamepad_face_right = "gamepad_face_right"
    bundle = mock.MagicMock()
    bundle.imgui = imgui
    sys.modules["imgui_bundle"] = bundle


_ensure_imgui()

from client.errors import DroneApiError  # noqa: E402
import ui.screens.swarm as swarm  # noqa: E402
from ui.screens.swarm import SwarmScreen  # noqa: E402


class _BrowseClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict | None]] = []
        self.release = threading.Event()
        self.started = threading.Event()
        self.block = False

    def get(self, path: str, *, timeout: float | None = None):
        self.gets.append(path)
        if self.block:
            self.started.set()
            if not self.release.wait(1.0):
                raise DroneApiError("render thread blocked inside a network call")
        if self.fail:
            raise DroneApiError("browse unavailable")
        route = unquote(urlsplit(path).path)
        if route.endswith("/media-browse/sources"):
            return {
                "sources": [
                    {"drone_id": "self", "name": "This Drone", "is_local": True, "online": True},
                    {"drone_id": "r5", "name": "r5-den", "is_local": False, "online": True},
                    {"drone_id": "pi", "name": "pi-kids", "is_local": False, "online": True},
                    {"drone_id": "slow", "name": "slow-box", "is_local": False, "online": False},
                ]
            }
        query = parse_qs(urlsplit(path).query, keep_blank_values=True)
        kind = route.rstrip("/").rsplit("/", 1)[-1]
        cursor = (query.get("cursor") or [""])[0]
        q = (query.get("q") or [""])[0]
        if cursor:
            return {"items": [], "sources": [], "next_cursor": "", "has_more": False}
        if kind == "music":
            items = [
                {
                    "display_title": "Zelda's Lullaby",
                    "artist": "Koji Kondo",
                    "album": "Ocarina of Time",
                    "file_path": "Koji Kondo/Ocarina of Time/lullaby.mp3",
                    "file_size": 4096,
                    "drone_id": "r5",
                    "drone_name": "r5-den",
                    "exists_locally": False,
                }
            ]
        elif kind == "movies":
            items = [
                {
                    "display_title": "Twin Peaks S02E01",
                    "show_title": "Twin Peaks",
                    "season": 2,
                    "episode": 1,
                    "file_path": "Twin Peaks/Season 2/Twin Peaks S02E01.mkv",
                    "file_size": 2048,
                    "drone_id": "pi",
                    "drone_name": "pi-kids",
                    "exists_locally": False,
                }
            ]
        else:
            if q not in ("", "zelda"):
                items = []
            else:
                items = [
                    {
                        "display_title": "Zelda: Link's Awakening",
                        "name": "Zelda: Link's Awakening",
                        "system": "gb",
                        "file_path": "gb/Zelda.zip",
                        "file_size": 262144,
                        "drone_id": "r5",
                        "drone_name": "r5-den",
                        "exists_locally": False,
                    },
                    {
                        "display_title": "Zelda II",
                        "name": "Zelda II",
                        "system": "nes",
                        "file_path": "nes/Zelda II.zip",
                        "file_size": 131072,
                        "drone_id": "r5",
                        "drone_name": "r5-den",
                        "exists_locally": True,
                    },
                    {
                        "display_title": "Legend of Zelda",
                        "name": "Legend of Zelda",
                        "system": "nes",
                        "file_path": "nes/Legend of Zelda.zip",
                        "file_size": 196608,
                        "drone_id": "pi",
                        "drone_name": "pi-kids",
                        "exists_locally": False,
                    },
                ]
        return {
            "items": items,
            "sources": [
                {"drone_id": "r5", "name": "r5-den", "status": "ok"},
                {"drone_id": "pi", "name": "pi-kids", "status": "ok"},
                {"drone_id": "slow", "name": "slow-box", "status": "timeout"},
            ],
            "next_cursor": "page-2 !!",
            "has_more": True,
        }

    def post(self, path: str, body=None, *, timeout: float | None = None):
        self.posts.append((path, body if isinstance(body, dict) else {}))
        return {"jobs": [{"job_id": "job-1", "status": "queued"}], "queued_job_ids": ["job-1"]}


def _tabs() -> list[tuple[str, str]]:
    tabs = getattr(swarm, "_TABS", ())
    return [(str(key), str(label)) for key, label in tabs]


def _tab_id(label: str) -> str:
    for key, text in _tabs():
        if text == label:
            return key
    raise AssertionError(f"Swarm sub-tabs are { [label for _, label in _tabs()] }, missing {label!r}")


def _flush(screen: SwarmScreen) -> None:
    screen.wait_for_deferred_action(2)


def _open_search(screen: SwarmScreen) -> str:
    tab = _tab_id("Search Assets")
    if hasattr(screen, "_queue_tab"):
        screen._queue_tab(tab, "Search Assets")
        _flush(screen)
    elif hasattr(screen, "_select_tab"):
        screen._select_tab(tab)
        _flush(screen)
    else:
        raise AssertionError("Search Assets tab cannot be selected")
    if hasattr(screen, "_ensure_loaded") and getattr(screen, "tab", None) == tab:
        loaded = getattr(screen, "_loaded_tabs", None)
        if isinstance(loaded, set) and tab not in loaded:
            screen._ensure_loaded(tab)
            _flush(screen)
    return tab


def _reload(screen: SwarmScreen) -> None:
    tab = getattr(screen, "tab", None)
    loaded = getattr(screen, "_loaded_tabs", None)
    if isinstance(loaded, set):
        loaded.discard(tab)
    if hasattr(screen, "_queue_tab") and tab is not None:
        screen._queue_tab(tab, "Search Assets")
        _flush(screen)
    if isinstance(loaded, set):
        loaded.discard(tab)
    if hasattr(screen, "_ensure_loaded") and tab is not None:
        screen._ensure_loaded(tab)
        _flush(screen)
    for name in dir(screen):
        if name.startswith("__") or "draw" in name or any(token in name for token in ("clear", "reset", "leave", "poll")):
            continue
        if not any(token in name for token in ("search", "browse", "result")):
            continue
        function = getattr(screen, name)
        if not callable(function):
            continue
        try:
            parameters = list(inspect.signature(function).parameters)
        except (TypeError, ValueError):
            continue
        if parameters == ["self"] or parameters == []:
            function()
            _flush(screen)


def _assign_matching(screen: SwarmScreen, pattern: str, value) -> bool:
    import re

    found = False
    for name, current in list(vars(screen).items()):
        if not re.search(pattern, name):
            continue
        if isinstance(value, bool) and isinstance(current, bool):
            setattr(screen, name, value)
            found = True
        elif isinstance(value, str) and (isinstance(current, str) or current is None):
            setattr(screen, name, value)
            found = True
    return found


def _browse_gets(client: _BrowseClient) -> list[str]:
    return [path for path in client.gets if "/media-browse/" in path]


def _row_texts(screen: SwarmScreen) -> list[str]:
    texts: list[str] = []

    def walk(value) -> None:
        if isinstance(value, dict):
            texts.append(" ".join(str(item) for item in value.values()))
            for child in value.values():
                walk(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child)
        elif isinstance(value, str):
            texts.append(value)

    walk(vars(screen))
    return texts


def _kind_ids() -> dict[str, str]:
    required = {"Games", "Movies & Shows", "Music"}
    for value in vars(swarm).values():
        mapping: list[tuple[str, str]] = []
        if isinstance(value, dict):
            for key, label in value.items():
                if isinstance(key, str) and isinstance(label, str):
                    if label in required:
                        mapping.append((key, label))
                    elif key in required:
                        mapping.append((label, key))
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, (list, tuple)) and len(item) >= 2 and isinstance(item[0], str) and isinstance(item[1], str):
                    mapping.append((item[0], item[1]))
        labels = {label for _, label in mapping}
        if required <= labels:
            return {label: key for key, label in mapping}
    raise AssertionError("asset type tabs Games / Movies & Shows / Music are missing")


def _select_kind(screen: SwarmScreen, label: str) -> None:
    kind_id = _kind_ids()[label]
    found = False
    for name, value in list(vars(screen).items()):
        if value in _kind_ids().values() and "kind" in name or value in _kind_ids().values() and "type" in name:
            setattr(screen, name, kind_id)
            found = True
    if not found:
        for name, value in list(vars(screen).items()):
            if value in _kind_ids().values():
                setattr(screen, name, kind_id)
                found = True
    if not found:
        raise AssertionError(f"no asset-type state to select {label}")


class SearchAssetsUat(unittest.TestCase):
    def test_swarm_subtabs_are_search_assets_without_reference_roms(self) -> None:
        # "Swarm sub-tabs are now Overview / Tailnet / Local Network / Search Assets."
        # "The Swarm tab shows Search Assets, not Request Assets. Reference ROMs is gone from the Ports client."
        self.assertEqual(
            [label for _, label in _tabs()],
            ["Overview", "Tailnet", "Local Network", "Search Assets"],
        )
        self.assertFalse(hasattr(swarm, "_TAB_REFERENCE"))
        self.assertFalse(any(name.startswith("_draw_reference") for name in dir(swarm.SwarmScreen)))
        readme = (PORTS / "README.md").read_text(encoding="utf-8")
        self.assertIn("Search Assets", readme)
        docstring = inspect.getdoc(swarm) or ""
        self.assertNotIn("no Music", docstring)
        self.assertNotIn("Reference ROMs", docstring)

    def test_empty_games_query_searches_all_systems_on_browse_api(self) -> None:
        # "An empty query lists recent/all items, so browsing still works without typing."
        # "Games search across all systems by default."
        # "without picking a peer or system first."
        client = _BrowseClient()
        screen = SwarmScreen(client)
        _open_search(screen)
        browse = _browse_gets(client)
        rom_calls = [path for path in browse if "/media-browse/roms" in unquote(urlsplit(path).path)]
        self.assertTrue(rom_calls, f"games search did not call media-browse roms: {client.gets}")
        query = parse_qs(urlsplit(rom_calls[0]).query, keep_blank_values=True)
        self.assertTrue(query.get("q", [""]) in ([], [""]))
        self.assertTrue(query.get("system", [""]) in ([], [""]))
        self.assertFalse(any("/assets?type=" in path or "/assets?type=" in path for path in client.gets))
        texts = _row_texts(screen)
        self.assertTrue(any("Link" in text and "gb" in text and "r5-den" in text for text in texts), texts)
        self.assertTrue(any("Legend of Zelda" in text and "pi-kids" in text for text in texts), texts)
        self.assertTrue(any("nes" in text and "Zelda II" in text for text in texts), texts)

    def test_zelda_query_filters_the_same_all_drone_search(self) -> None:
        # "Searching \"zelda\" on Games returns matches from all systems on all online paired Drones"
        client = _BrowseClient()
        screen = SwarmScreen(client)
        _open_search(screen)
        self.assertTrue(_assign_matching(screen, r"query|search", "zelda"))
        _reload(screen)
        rom_calls = [
            path for path in _browse_gets(client)
            if unquote(urlsplit(path).path).rstrip("/").endswith("/media-browse/roms")
            and (parse_qs(urlsplit(path).query).get("q") or [""]) == ["zelda"]
        ]
        self.assertTrue(rom_calls, client.gets)
        query = parse_qs(urlsplit(rom_calls[-1]).query, keep_blank_values=True)
        self.assertTrue(query.get("system", [""]) in ([], [""]))

    def test_timeout_source_is_shown_and_does_not_drop_results(self) -> None:
        # "Show a per-Drone status line for any Drone that was `offline`/`timeout`/`unsupported`; it must never block the results."
        client = _BrowseClient()
        screen = SwarmScreen(client)
        _open_search(screen)
        blob = "\n".join(_row_texts(screen))
        self.assertIn("Link", blob)
        self.assertTrue("slow-box" in blob and "timeout" in blob.lower(), blob)

    def test_next_cursor_page_is_requested_when_the_list_continues(self) -> None:
        # "Results load page by page (`cursor`) as the user scrolls to the end of the list."
        client = _BrowseClient()
        screen = SwarmScreen(client)
        _open_search(screen)
        paged = False
        for name in dir(screen):
            if name.startswith("__") or "draw" in name:
                continue
            function = getattr(screen, name)
            if not callable(function):
                continue
            try:
                source = inspect.getsource(function)
            except (OSError, TypeError):
                continue
            if "cursor" not in source or "media_browse" not in source:
                continue
            try:
                parameters = list(inspect.signature(function).parameters)
            except (TypeError, ValueError):
                continue
            if parameters in (["self"], []):
                function()
                _flush(screen)
                paged = True
        self.assertTrue(paged, "Search results never request another cursor page")
        cursors = []
        for path in _browse_gets(client):
            cursors.extend(parse_qs(urlsplit(path).query, keep_blank_values=True).get("cursor") or [])
        self.assertIn("page-2 !!", cursors)

    def test_hide_items_i_have_sends_exclude_local_and_does_not_overwrite(self) -> None:
        # "\"Hide items I have\" maps to `exists_locally` and the non-overwrite behavior, like today's \"Ignore existing games\"."
        client = _BrowseClient()
        screen = SwarmScreen(client)
        _open_search(screen)
        self.assertTrue(_assign_matching(screen, r"hide|ignore_existing|exclude_local", True))
        _reload(screen)
        hidden = [
            path for path in _browse_gets(client)
            if (parse_qs(urlsplit(path).query).get("exclude_local") or [""])[0] in {"1", "true", "yes"}
        ]
        self.assertTrue(hidden, client.gets)
        self._download_all(screen)
        _flush(screen)
        self.assertTrue(client.posts, "Download All did not queue a copy")
        for path, body in client.posts:
            self.assertTrue(path.endswith("/sync") or path.endswith("/sync-bulk"), path)
            self.assertIsNot(body.get("overwrite_files"), True)

    def test_download_all_queues_every_source_drone(self) -> None:
        # "Download All queues everything in the current results/filter. If there's more than one source Drone, use the bulk endpoint per Drone, or loop over `sync`."
        client = _BrowseClient()
        screen = SwarmScreen(client)
        _open_search(screen)
        self._download_all(screen)
        _flush(screen)
        peer_ids = {str((body or {}).get("peer_id") or "") for _, body in client.posts}
        self.assertTrue({"r5", "pi"} <= peer_ids, client.posts)

    def test_music_search_and_download_uses_music_asset_type(self) -> None:
        # "Movies & Shows and Music tabs search and download the same way. Music lands in the music library."
        client = _BrowseClient()
        screen = SwarmScreen(client)
        _open_search(screen)
        _select_kind(screen, "Music")
        _reload(screen)
        music_calls = [path for path in _browse_gets(client) if unquote(urlsplit(path).path).rstrip("/").endswith("/media-browse/music")]
        self.assertTrue(music_calls, client.gets)
        texts = _row_texts(screen)
        self.assertTrue(any("Lullaby" in text and "Koji Kondo" in text and "r5-den" in text for text in texts), texts)
        self._download_one(screen)
        _flush(screen)
        music_posts = [body for _, body in client.posts if str(body.get("asset_type") or "") == "music"]
        self.assertTrue(music_posts, client.posts)
        self.assertEqual(music_posts[0].get("peer_id"), "r5")

    def test_movies_search_labels_show_and_source_drone(self) -> None:
        # "each result row shows the title, system (games), artist/album (music) or show/season (movies), the source Drone name"
        client = _BrowseClient()
        screen = SwarmScreen(client)
        _open_search(screen)
        _select_kind(screen, "Movies & Shows")
        _reload(screen)
        movie_calls = [path for path in _browse_gets(client) if "/media-browse/movies" in path]
        self.assertTrue(movie_calls, client.gets)
        texts = _row_texts(screen)
        self.assertTrue(any("Twin Peaks" in text and "pi-kids" in text for text in texts), texts)

    def test_opening_search_does_not_block_the_caller_on_the_network(self) -> None:
        # "`defer_action` for every network call. Nothing may block the render loop."
        client = _BrowseClient()
        client.block = True
        screen = SwarmScreen(client)
        tab = _tab_id("Search Assets")
        self.assertTrue(hasattr(screen, "_queue_tab"))
        screen._queue_tab(tab, "Search Assets")
        self.assertFalse(client.started.is_set(), "Search Assets performed network I/O inside the caller")
        screen.run_deferred_action()
        self.assertTrue(client.started.wait(1.0))
        client.release.set()
        _flush(screen)

    def test_system_and_drone_filters_narrow_the_browse_request(self) -> None:
        # "An optional System filter (All systems + the systems seen in results or the Drones' summaries) narrows it. An optional Drones filter narrows to specific peers."
        client = _BrowseClient()
        screen = SwarmScreen(client)
        _open_search(screen)
        self.assertTrue(_assign_matching(screen, r"system", "gb"))
        self.assertTrue(
            _assign_matching(screen, r"drones|drone.*(filter|ids|selected)|(?:filter|selected|ids).*drone", "r5")
        )
        _reload(screen)
        narrowed = []
        for path in _browse_gets(client):
            if not unquote(urlsplit(path).path).rstrip("/").endswith("/media-browse/roms"):
                continue
            query = parse_qs(urlsplit(path).query, keep_blank_values=True)
            if (query.get("system") or [""]) == ["gb"] and "r5" in (query.get("drones") or [""])[0]:
                narrowed.append(path)
        self.assertTrue(narrowed, client.gets)

    def _download_all(self, screen: SwarmScreen) -> None:
        matches = [name for name in dir(screen) if "download_all" in name.lower() and callable(getattr(screen, name))]
        self.assertTrue(matches, "Download All is not available")
        function = getattr(screen, matches[0])
        parameters = list(inspect.signature(function).parameters)
        if parameters in (["self"], []):
            function()
        else:
            raise AssertionError(f"{matches[0]} is not a no-argument Download All action")

    def _download_one(self, screen: SwarmScreen) -> None:
        item = {
            "display_title": "Zelda's Lullaby",
            "artist": "Koji Kondo",
            "album": "Ocarina of Time",
            "file_path": "Koji Kondo/Ocarina of Time/lullaby.mp3",
            "file_size": 4096,
            "drone_id": "r5",
            "drone_name": "r5-den",
            "exists_locally": False,
        }
        for name in ("_download_item", "_download_result", "_queue_download", "_request_item", "_do_request_item"):
            function = getattr(screen, name, None)
            if not callable(function):
                continue
            for args in ((item,), ("music", item, "Zelda's Lullaby"), ("music", item, "Zelda's Lullaby", "")):
                try:
                    function(*args)
                except TypeError:
                    continue
                return
        self.fail("Download on a result row is not available")


if __name__ == "__main__":
    unittest.main()
