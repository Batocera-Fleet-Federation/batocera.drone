"""Issue #722: Ports client browse wrappers replace peer-scoped asset calls.

Quoted requirement: "New endpoint wrappers in `client/endpoints.py`
(`media_browse_sources`, `media_browse_list`). Remove
`peer_asset_summary`/`peer_roms`/`peer_movies` once nothing uses them."

Also: "GET /v1/api/media-browse/sources" and
"GET /v1/api/media-browse/{roms|movies|music}?q=&system=&drones=<ids>&exclude_local=1&cursor=&limit=".
The HTTP client already prefixes `/v1/api`, so the wrapper path is the
suffix after that prefix.
"""

from __future__ import annotations

import ast
import inspect
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
PORTS = ROOT / "ports-client"
if str(PORTS) not in sys.path:
    sys.path.insert(0, str(PORTS))

from client import endpoints  # noqa: E402


class _RecordingClient:
    def __init__(self) -> None:
        self.gets: list[str] = []

    def get(self, path: str, *, timeout: float | None = None):
        self.gets.append(path)
        return {"ok": True, "path": path, "timeout": timeout}


def _query(path: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(path).query, keep_blank_values=True)


def _route(path: str) -> str:
    return unquote(urlsplit(path).path)


class MediaBrowseClientUat(unittest.TestCase):
    def test_sources_wrapper_hits_media_browse_sources(self) -> None:
        # "GET /v1/api/media-browse/sources: paired Drones + `online`"
        client = _RecordingClient()
        wrapper = getattr(endpoints, "media_browse_sources", None)
        self.assertTrue(callable(wrapper), "media_browse_sources is missing")
        wrapper(client)
        self.assertEqual(len(client.gets), 1)
        self.assertEqual(_route(client.gets[0]), "/media-browse/sources")
        self.assertNotIn("/v1/api/v1/api", client.gets[0])

    def test_list_wrapper_encodes_kind_query_and_paging_fields(self) -> None:
        # "GET /v1/api/media-browse/{roms|movies|music}?q=&system=&drones=<ids>&exclude_local=1&cursor=&limit="
        client = _RecordingClient()
        wrapper = getattr(endpoints, "media_browse_list", None)
        self.assertTrue(callable(wrapper), "media_browse_list is missing")
        cursor = "a b/??!!"
        wrapper(
            client,
            "roms",
            q="zelda",
            system="",
            drones="r5-den,pi kids",
            exclude_local=True,
            cursor=cursor,
            limit=25,
        )
        self.assertEqual(_route(client.gets[0]), "/media-browse/roms")
        query = _query(client.gets[0])
        self.assertEqual(query.get("q"), ["zelda"])
        system = query.get("system", [""])
        self.assertIn(system, [["",], []])
        self.assertEqual(query.get("drones"), ["r5-den,pi kids"])
        self.assertIn(query.get("exclude_local"), [["1"], ["true"]])
        self.assertEqual(query.get("cursor"), [cursor])
        self.assertEqual(query.get("limit"), ["25"])

    def test_list_wrapper_covers_movies_and_music_and_empty_query(self) -> None:
        # "Movies & Shows and Music tabs search and download the same way."
        # "An empty query lists recent/all items, so browsing still works without typing."
        client = _RecordingClient()
        wrapper = getattr(endpoints, "media_browse_list", None)
        self.assertTrue(callable(wrapper), "media_browse_list is missing")
        for kind in ("movies", "music"):
            wrapper(client, kind, q="", system="", drones="", exclude_local=False, cursor="", limit=10)
        routes = [_route(path) for path in client.gets]
        self.assertEqual(routes, ["/media-browse/movies", "/media-browse/music"])
        for path in client.gets:
            query = _query(path)
            self.assertTrue(query.get("q", [""]) in ([], [""]))
            self.assertNotIn("1", query.get("exclude_local", ["0"]))

    def test_removed_peer_and_network_share_wrappers_are_gone(self) -> None:
        # "Remove `peer_asset_summary`/`peer_roms`/`peer_movies` once nothing uses them."
        # "the `network_shares`/`network_share_enable`/`network_share_disable` wrappers"
        for name in (
            "peer_asset_summary",
            "peer_roms",
            "peer_movies",
            "network_shares",
            "network_share_enable",
            "network_share_disable",
        ):
            self.assertFalse(hasattr(endpoints, name), name)

    def test_ports_client_no_longer_calls_removed_wrappers(self) -> None:
        # "Remove `peer_asset_summary`/`peer_roms`/`peer_movies` once nothing uses them."
        names: set[str] = set()
        for path in PORTS.rglob("*.py"):
            if "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    names.add(node.id)
                elif isinstance(node, ast.Attribute):
                    names.add(node.attr)
        for name in (
            "peer_asset_summary",
            "peer_roms",
            "peer_movies",
            "network_shares",
            "network_share_enable",
            "network_share_disable",
        ):
            self.assertNotIn(name, names)

    def test_list_signature_accepts_the_issue_query_fields(self) -> None:
        # The wrapper must accept the query fields named in the browse URL.
        wrapper = getattr(endpoints, "media_browse_list", None)
        self.assertTrue(callable(wrapper), "media_browse_list is missing")
        signature = inspect.signature(wrapper)
        parameters = set(signature.parameters)
        for name in ("q", "system", "drones", "exclude_local", "cursor", "limit"):
            self.assertIn(name, parameters)


if __name__ == "__main__":
    unittest.main()
