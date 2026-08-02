import json
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

from app.movies.tmdb_client import TmdbClient, TmdbUnavailableError, tmdb_image_url


class FakeResponse:
    def __init__(self, payload: bytes, content_type: str = "application/json"):
        self._payload = payload
        self.headers = mock.Mock()
        self.headers.get_content_type.return_value = content_type

    def read(self, *_args):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class TmdbImageUrlTests(unittest.TestCase):
    def test_builds_absolute_url(self) -> None:
        self.assertEqual(tmdb_image_url("/abc123.jpg", "w500"), "https://image.tmdb.org/t/p/w500/abc123.jpg")

    def test_none_path_returns_none(self) -> None:
        self.assertIsNone(tmdb_image_url(None, "w500"))
        self.assertIsNone(tmdb_image_url("", "w500"))


class TmdbClientConstructionTests(unittest.TestCase):
    def test_requires_an_api_key(self) -> None:
        with self.assertRaises(TmdbUnavailableError):
            TmdbClient("")
        with self.assertRaises(TmdbUnavailableError):
            TmdbClient("   ")


class TmdbClientSearchTests(unittest.TestCase):
    def test_empty_query_returns_no_results_without_a_request(self) -> None:
        client = TmdbClient("key")
        with mock.patch("app.movies.tmdb_client.urlopen", side_effect=AssertionError("must not call TMDb")):
            self.assertEqual(client.search(""), [])

    def test_parses_results(self) -> None:
        payload = json.dumps(
            {
                "results": [
                    {
                        "id": 603,
                        "title": "The Matrix",
                        "release_date": "1999-03-30",
                        "overview": "A hacker discovers reality is a simulation.",
                        "poster_path": "/poster.jpg",
                    },
                    {"id": 604, "title": "The Matrix Reloaded"},
                    "not-a-dict",
                ]
            }
        ).encode("utf-8")
        client = TmdbClient("key")
        with mock.patch("app.movies.tmdb_client.urlopen", return_value=FakeResponse(payload)):
            results = client.search("the matrix")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["tmdb_id"], 603)
        self.assertEqual(results[0]["title"], "The Matrix")
        self.assertEqual(results[0]["release_date"], "1999-03-30")
        self.assertEqual(results[0]["thumbnail_url"], "https://image.tmdb.org/t/p/w154/poster.jpg")
        self.assertIsNone(results[1]["thumbnail_url"])

    def test_401_raises_tmdb_unavailable_with_a_clear_reason(self) -> None:
        client = TmdbClient("bad-key")
        error = HTTPError("https://api.themoviedb.org/3/search/movie", 401, "Unauthorized", None, None)
        with mock.patch("app.movies.tmdb_client.urlopen", side_effect=error):
            with self.assertRaisesRegex(TmdbUnavailableError, "rejected"):
                client.search("matrix")

    def test_network_error_raises_tmdb_unavailable(self) -> None:
        client = TmdbClient("key")
        with mock.patch("app.movies.tmdb_client.urlopen", side_effect=URLError("no network")):
            with self.assertRaises(TmdbUnavailableError):
                client.search("matrix")


class TmdbClientDetailsTests(unittest.TestCase):
    def test_parses_details_genres_and_cast(self) -> None:
        payload = json.dumps(
            {
                "id": 603,
                "title": "The Matrix",
                "overview": "A hacker discovers reality is a simulation.",
                "tagline": "Welcome to the Real World.",
                "genres": [{"id": 1, "name": "Action"}, {"id": 2, "name": "Science Fiction"}],
                "release_date": "1999-03-30",
                "vote_average": 8.2,
                "runtime": 136,
                "poster_path": "/poster.jpg",
                "backdrop_path": "/backdrop.jpg",
                "credits": {
                    "cast": [
                        {"name": "Keanu Reeves", "character": "Neo"},
                        {"name": "Laurence Fishburne", "character": "Morpheus"},
                        {"name": ""},  # no name -- must be skipped
                    ]
                },
            }
        ).encode("utf-8")
        client = TmdbClient("key")
        with mock.patch("app.movies.tmdb_client.urlopen", return_value=FakeResponse(payload)):
            details = client.details(603)
        self.assertEqual(details["title"], "The Matrix")
        self.assertEqual(details["genres"], ["Action", "Science Fiction"])
        self.assertEqual(details["cast"], [{"name": "Keanu Reeves", "character": "Neo"}, {"name": "Laurence Fishburne", "character": "Morpheus"}])
        self.assertEqual(details["poster_url"], "https://image.tmdb.org/t/p/w500/poster.jpg")
        self.assertEqual(details["backdrop_url"], "https://image.tmdb.org/t/p/w1280/backdrop.jpg")
        self.assertEqual(details["runtime_minutes"], 136)

    def test_caps_cast_at_20(self) -> None:
        payload = json.dumps(
            {
                "id": 603,
                "title": "Big Cast Movie",
                "credits": {"cast": [{"name": f"Actor {i}", "character": f"Role {i}"} for i in range(50)]},
            }
        ).encode("utf-8")
        client = TmdbClient("key")
        with mock.patch("app.movies.tmdb_client.urlopen", return_value=FakeResponse(payload)):
            details = client.details(603)
        self.assertEqual(len(details["cast"]), 20)

    def test_missing_id_raises_value_error(self) -> None:
        client = TmdbClient("key")
        with self.assertRaises(ValueError):
            client.details("")

    def test_404_raises_tmdb_unavailable(self) -> None:
        client = TmdbClient("key")
        error = HTTPError("https://api.themoviedb.org/3/movie/999999999", 404, "Not Found", None, None)
        with mock.patch("app.movies.tmdb_client.urlopen", side_effect=error):
            with self.assertRaises(TmdbUnavailableError):
                client.details(999999999)


class TmdbClientDownloadImageTests(unittest.TestCase):
    def test_rejects_non_tmdb_hosts(self) -> None:
        client = TmdbClient("key")
        with self.assertRaises(ValueError):
            client.download_image("https://evil.example.com/poster.jpg")

    def test_rejects_non_https(self) -> None:
        client = TmdbClient("key")
        with self.assertRaises(ValueError):
            client.download_image("http://image.tmdb.org/t/p/w500/poster.jpg")

    def test_downloads_a_valid_image(self) -> None:
        client = TmdbClient("key")
        with mock.patch("app.movies.tmdb_client.urlopen", return_value=FakeResponse(b"fake-jpeg-bytes", "image/jpeg")):
            data, content_type = client.download_image("https://image.tmdb.org/t/p/w500/poster.jpg")
        self.assertEqual(data, b"fake-jpeg-bytes")
        self.assertEqual(content_type, "image/jpeg")

    def test_rejects_non_image_content_type(self) -> None:
        client = TmdbClient("key")
        with mock.patch("app.movies.tmdb_client.urlopen", return_value=FakeResponse(b"<html>oops</html>", "text/html")):
            with self.assertRaises(ValueError):
                client.download_image("https://image.tmdb.org/t/p/w500/poster.jpg")


if __name__ == "__main__":
    unittest.main()
