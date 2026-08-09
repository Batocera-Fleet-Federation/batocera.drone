import json
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

from app.movies.tmdb_client import (
    TmdbClient,
    TmdbNotFoundError,
    TmdbUnavailableError,
    _pick_best_trailer_key,
    parse_tmdb_movie_id,
    tmdb_image_url,
)


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


class PickBestTrailerKeyTests(unittest.TestCase):
    """_pick_best_trailer_key extracts a YouTube video id (for iframe
    embedding on the details page, see drone.js's renderMovieDetailShell)
    out of TMDb's raw videos append_to_response payload -- never a
    downloadable file, just the id YouTube's own embed URL needs."""

    def test_no_payload_returns_none(self) -> None:
        self.assertIsNone(_pick_best_trailer_key(None))
        self.assertIsNone(_pick_best_trailer_key({}))
        self.assertIsNone(_pick_best_trailer_key({"results": []}))

    def test_picks_the_official_trailer(self) -> None:
        payload = {
            "results": [
                {"key": "teaser-key", "site": "YouTube", "type": "Teaser", "official": True, "published_at": "2020-01-01"},
                {"key": "trailer-key", "site": "YouTube", "type": "Trailer", "official": True, "published_at": "2020-02-01"},
            ]
        }
        self.assertEqual(_pick_best_trailer_key(payload), "trailer-key")

    def test_falls_back_to_teaser_when_no_trailer_exists(self) -> None:
        payload = {"results": [{"key": "teaser-key", "site": "YouTube", "type": "Teaser", "official": True, "published_at": "2020-01-01"}]}
        self.assertEqual(_pick_best_trailer_key(payload), "teaser-key")

    def test_official_beats_unofficial_of_the_same_type(self) -> None:
        payload = {
            "results": [
                {"key": "fan-upload", "site": "YouTube", "type": "Trailer", "official": False, "published_at": "2019-01-01"},
                {"key": "official-upload", "site": "YouTube", "type": "Trailer", "official": True, "published_at": "2020-01-01"},
            ]
        }
        self.assertEqual(_pick_best_trailer_key(payload), "official-upload")

    def test_ignores_non_youtube_sites(self) -> None:
        payload = {"results": [{"key": "vimeo-key", "site": "Vimeo", "type": "Trailer", "official": True, "published_at": "2020-01-01"}]}
        self.assertIsNone(_pick_best_trailer_key(payload))

    def test_ignores_unrecognized_video_types(self) -> None:
        # e.g. "Clip", "Featurette", "Behind the Scenes" -- not something a
        # details page would want auto-embedded as "the trailer".
        payload = {"results": [{"key": "clip-key", "site": "YouTube", "type": "Clip", "official": True, "published_at": "2020-01-01"}]}
        self.assertIsNone(_pick_best_trailer_key(payload))

    def test_ignores_entries_with_no_key(self) -> None:
        payload = {"results": [{"key": "", "site": "YouTube", "type": "Trailer", "official": True}]}
        self.assertIsNone(_pick_best_trailer_key(payload))


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


class TmdbClient429RetryTests(unittest.TestCase):
    """A 429 used to be indistinguishable from a revoked API key -- both
    raised TmdbUnavailableError immediately, which metadata_manager's bulk
    job treats as "stop the whole run, fail everything left." A bulk scrape
    of a large library can easily trip transient rate-limiting (the search
    ladder alone issues up to 4 requests per movie), so a 429 needs to be
    retried with backoff instead of aborting the run outright."""

    def _rate_limited(self, retry_after=None):
        headers = {"Retry-After": retry_after} if retry_after is not None else {}
        return HTTPError("https://api.themoviedb.org/3/search/movie", 429, "Too Many Requests", headers, None)

    def test_succeeds_after_one_retry(self) -> None:
        payload = json.dumps({"results": []}).encode("utf-8")
        client = TmdbClient("key")
        with mock.patch("app.movies.tmdb_client.urlopen", side_effect=[self._rate_limited(), FakeResponse(payload)]):
            with mock.patch("app.movies.tmdb_client.time.sleep") as sleep_mock:
                results = client.search("halloween")
        self.assertEqual(results, [])
        sleep_mock.assert_called_once()

    def test_honors_retry_after_header(self) -> None:
        payload = json.dumps({"results": []}).encode("utf-8")
        client = TmdbClient("key")
        with mock.patch("app.movies.tmdb_client.urlopen", side_effect=[self._rate_limited(retry_after="5"), FakeResponse(payload)]):
            with mock.patch("app.movies.tmdb_client.time.sleep") as sleep_mock:
                client.search("halloween")
        sleep_mock.assert_called_once_with(5.0)

    def test_falls_back_to_backoff_when_retry_after_is_not_a_plain_number(self) -> None:
        payload = json.dumps({"results": []}).encode("utf-8")
        client = TmdbClient("key")
        with mock.patch("app.movies.tmdb_client.urlopen", side_effect=[self._rate_limited(retry_after="not-a-number"), FakeResponse(payload)]):
            with mock.patch("app.movies.tmdb_client.time.sleep") as sleep_mock:
                client.search("halloween")
        sleep_mock.assert_called_once_with(1.0)

    def test_exhausting_retries_raises_tmdb_unavailable_not_a_crash(self) -> None:
        client = TmdbClient("key")
        with mock.patch("app.movies.tmdb_client.urlopen", side_effect=self._rate_limited()):
            with mock.patch("app.movies.tmdb_client.time.sleep") as sleep_mock:
                with self.assertRaises(TmdbUnavailableError):
                    client.search("halloween")
        self.assertEqual(sleep_mock.call_count, 3)

    def test_401_never_retries(self) -> None:
        client = TmdbClient("bad-key")
        error = HTTPError("https://api.themoviedb.org/3/search/movie", 401, "Unauthorized", None, None)
        with mock.patch("app.movies.tmdb_client.urlopen", side_effect=error):
            with mock.patch("app.movies.tmdb_client.time.sleep") as sleep_mock:
                with self.assertRaises(TmdbUnavailableError):
                    client.search("matrix")
        sleep_mock.assert_not_called()

    def test_year_is_passed_as_primary_release_year_filter(self) -> None:
        payload = json.dumps({"results": []}).encode("utf-8")
        client = TmdbClient("key")
        with mock.patch("app.movies.tmdb_client.urlopen", return_value=FakeResponse(payload)) as urlopen:
            client.search("halloween", year="1978")
        requested_url = urlopen.call_args[0][0].full_url
        self.assertIn("primary_release_year=1978", requested_url)

    def test_no_year_omits_the_filter(self) -> None:
        payload = json.dumps({"results": []}).encode("utf-8")
        client = TmdbClient("key")
        with mock.patch("app.movies.tmdb_client.urlopen", return_value=FakeResponse(payload)) as urlopen:
            client.search("halloween")
        requested_url = urlopen.call_args[0][0].full_url
        self.assertNotIn("primary_release_year", requested_url)


class TmdbClientSearchTvTests(unittest.TestCase):
    def test_empty_query_returns_no_results_without_a_request(self) -> None:
        client = TmdbClient("key")
        with mock.patch("app.movies.tmdb_client.urlopen", side_effect=AssertionError("must not call TMDb")):
            self.assertEqual(client.search_tv(""), [])

    def test_parses_tv_results_using_name_fields(self) -> None:
        payload = json.dumps(
            {
                "results": [
                    {
                        "id": 1405,
                        "name": "Dexter",
                        "first_air_date": "2006-10-01",
                        "overview": "A blood spatter analyst.",
                        "poster_path": "/dexter.jpg",
                    }
                ]
            }
        ).encode("utf-8")
        client = TmdbClient("key")
        with mock.patch("app.movies.tmdb_client.urlopen", return_value=FakeResponse(payload)):
            results = client.search_tv("dexter")
        self.assertEqual(results[0]["tmdb_id"], 1405)
        self.assertEqual(results[0]["title"], "Dexter")
        self.assertEqual(results[0]["release_date"], "2006-10-01")

    def test_year_is_passed_as_first_air_date_year_filter(self) -> None:
        payload = json.dumps({"results": []}).encode("utf-8")
        client = TmdbClient("key")
        with mock.patch("app.movies.tmdb_client.urlopen", return_value=FakeResponse(payload)) as urlopen:
            client.search_tv("dexter", year="2006")
        requested_url = urlopen.call_args[0][0].full_url
        self.assertIn("first_air_date_year=2006", requested_url)


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

    def test_includes_youtube_trailer_key_from_videos_append_to_response(self) -> None:
        payload = json.dumps(
            {
                "id": 603,
                "title": "The Matrix",
                "videos": {"results": [{"key": "m8e-FF8MsqU", "site": "YouTube", "type": "Trailer", "official": True, "published_at": "1999-01-01"}]},
            }
        ).encode("utf-8")
        client = TmdbClient("key")
        with mock.patch("app.movies.tmdb_client.urlopen", return_value=FakeResponse(payload)) as urlopen:
            details = client.details(603)
        self.assertEqual(details["youtube_trailer_key"], "m8e-FF8MsqU")
        # One HTTP round trip covers cast/crew and videos both -- no separate
        # /credits or /videos call.
        self.assertIn("append_to_response=credits%2Cvideos", urlopen.call_args[0][0].full_url)

    def test_no_videos_in_payload_yields_none_trailer_key(self) -> None:
        payload = json.dumps({"id": 603, "title": "The Matrix"}).encode("utf-8")
        client = TmdbClient("key")
        with mock.patch("app.movies.tmdb_client.urlopen", return_value=FakeResponse(payload)):
            details = client.details(603)
        self.assertIsNone(details["youtube_trailer_key"])

    def test_404_raises_tmdb_not_found(self) -> None:
        # TmdbNotFoundError subclasses TmdbUnavailableError -- a caller that
        # only catches the broader type (e.g. the per-movie manual scrape's
        # HTTP 502 mapping) is unaffected; only the bulk job's per-candidate
        # loop needs to tell the two apart (see test_movies_metadata_manager.py).
        client = TmdbClient("key")
        error = HTTPError("https://api.themoviedb.org/3/movie/999999999", 404, "Not Found", None, None)
        with mock.patch("app.movies.tmdb_client.urlopen", side_effect=error):
            with self.assertRaises(TmdbNotFoundError):
                client.details(999999999)


class TmdbClientTvDetailsTests(unittest.TestCase):
    def test_parses_show_details_using_name_fields(self) -> None:
        payload = json.dumps(
            {
                "id": 1405,
                "name": "Dexter",
                "overview": "A blood spatter analyst.",
                "tagline": "",
                "genres": [{"id": 1, "name": "Drama"}],
                "first_air_date": "2006-10-01",
                "vote_average": 8.0,
                "poster_path": "/dexter.jpg",
                "backdrop_path": "/dexter-bg.jpg",
                "credits": {"cast": [{"name": "Michael C. Hall", "character": "Dexter Morgan"}]},
            }
        ).encode("utf-8")
        client = TmdbClient("key")
        with mock.patch("app.movies.tmdb_client.urlopen", return_value=FakeResponse(payload)):
            details = client.tv_details(1405)
        self.assertEqual(details["title"], "Dexter")
        self.assertEqual(details["release_date"], "2006-10-01")
        self.assertEqual(details["genres"], ["Drama"])
        self.assertEqual(details["poster_url"], "https://image.tmdb.org/t/p/w500/dexter.jpg")

    def test_missing_id_raises_value_error(self) -> None:
        client = TmdbClient("key")
        with self.assertRaises(ValueError):
            client.tv_details("")

    def test_includes_youtube_trailer_key_from_videos_append_to_response(self) -> None:
        payload = json.dumps(
            {
                "id": 1405,
                "name": "Dexter",
                "videos": {"results": [{"key": "dexter-trailer", "site": "YouTube", "type": "Trailer", "official": True, "published_at": "2006-01-01"}]},
            }
        ).encode("utf-8")
        client = TmdbClient("key")
        with mock.patch("app.movies.tmdb_client.urlopen", return_value=FakeResponse(payload)):
            details = client.tv_details(1405)
        self.assertEqual(details["youtube_trailer_key"], "dexter-trailer")


class TmdbClientTvSeasonDetailsTests(unittest.TestCase):
    def test_parses_season_details_including_its_own_poster(self) -> None:
        payload = json.dumps(
            {
                "name": "Season 1",
                "overview": "Dexter's first season.",
                "air_date": "2006-10-01",
                "poster_path": "/dexter-s1.jpg",
            }
        ).encode("utf-8")
        client = TmdbClient("key")
        with mock.patch("app.movies.tmdb_client.urlopen", return_value=FakeResponse(payload)):
            details = client.tv_season_details(1405, 1)
        self.assertEqual(details["title"], "Season 1")
        self.assertEqual(details["overview"], "Dexter's first season.")
        self.assertEqual(details["poster_url"], "https://image.tmdb.org/t/p/w500/dexter-s1.jpg")
        # No backdrop_url key at all -- TMDb seasons have no backdrop of their own.
        self.assertNotIn("backdrop_url", details)

    def test_missing_ids_raise_value_error(self) -> None:
        client = TmdbClient("key")
        with self.assertRaises(ValueError):
            client.tv_season_details("", 1)
        with self.assertRaises(ValueError):
            client.tv_season_details(1405, "")

    def test_season_zero_is_not_treated_as_missing(self) -> None:
        # Regression: season_number=0 is a real, common case (Plex/Kodi/
        # Jellyfin's "Season 0" convention for standalone TV specials), but
        # Python's `season_number or ""` idiom treats the falsy int 0 the
        # same as an empty/missing value, raising a spurious ValueError
        # before ever reaching TMDb. Confirmed live: three real "Forensic
        # Files Season 00" episodes failed a bulk scrape with exactly this
        # error ("tv_id and season_number are required").
        payload = json.dumps({"name": "Specials", "overview": "", "air_date": None, "poster_path": None}).encode("utf-8")
        client = TmdbClient("key")
        with mock.patch("app.movies.tmdb_client.urlopen", return_value=FakeResponse(payload)) as urlopen:
            details = client.tv_season_details(1405, 0)
        self.assertEqual(details["title"], "Specials")
        self.assertIn("/tv/1405/season/0", urlopen.call_args[0][0].full_url)

    def test_404_raises_tmdb_not_found(self) -> None:
        client = TmdbClient("key")
        error = HTTPError("https://api.themoviedb.org/3/tv/1405/season/99", 404, "Not Found", None, None)
        with mock.patch("app.movies.tmdb_client.urlopen", side_effect=error):
            with self.assertRaises(TmdbNotFoundError):
                client.tv_season_details(1405, 99)


class TmdbClientTvEpisodeDetailsTests(unittest.TestCase):
    def test_parses_episode_details(self) -> None:
        payload = json.dumps(
            {
                "name": "Dexter",
                "overview": "Pilot episode.",
                "air_date": "2006-10-01",
                "vote_average": 7.5,
                "still_path": "/still.jpg",
            }
        ).encode("utf-8")
        client = TmdbClient("key")
        with mock.patch("app.movies.tmdb_client.urlopen", return_value=FakeResponse(payload)):
            details = client.tv_episode_details(1405, 1, 1)
        self.assertEqual(details["title"], "Dexter")
        self.assertEqual(details["air_date"], "2006-10-01")
        self.assertEqual(details["still_url"], "https://image.tmdb.org/t/p/w1280/still.jpg")

    def test_missing_ids_raise_value_error(self) -> None:
        client = TmdbClient("key")
        with self.assertRaises(ValueError):
            client.tv_episode_details("", 1, 1)
        with self.assertRaises(ValueError):
            client.tv_episode_details(1405, "", 1)

    def test_season_and_episode_zero_are_not_treated_as_missing(self) -> None:
        # Same regression as tv_season_details -- season_number=0 (and, for
        # consistency, episode_number=0) must not be mistaken for a missing
        # parameter just because 0 is falsy in Python.
        payload = json.dumps(
            {"name": "Special", "overview": "", "air_date": None, "vote_average": None, "still_path": None}
        ).encode("utf-8")
        client = TmdbClient("key")
        with mock.patch("app.movies.tmdb_client.urlopen", return_value=FakeResponse(payload)) as urlopen:
            details = client.tv_episode_details(1405, 0, 0)
        self.assertEqual(details["title"], "Special")
        self.assertIn("/tv/1405/season/0/episode/0", urlopen.call_args[0][0].full_url)

    def test_404_raises_tmdb_not_found(self) -> None:
        client = TmdbClient("key")
        error = HTTPError("https://api.themoviedb.org/3/tv/1405/season/99/episode/1", 404, "Not Found", None, None)
        with mock.patch("app.movies.tmdb_client.urlopen", side_effect=error):
            with self.assertRaises(TmdbNotFoundError):
                client.tv_episode_details(1405, 99, 1)


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


class ParseTmdbMovieIdTests(unittest.TestCase):
    """Regression coverage for the direct-lookup escape hatch: a human pastes
    a themoviedb.org movie URL (or bare id) after finding the right page on
    TMDb's own site, because search doesn't reliably surface it (e.g. a movie
    catalogued under an AKA TMDb's search only weakly matches -- the real
    live case this was built for: "Hell of the Dead" not finding "Night of
    the Zombies" / TMDb id 21380, whose page is
    https://www.themoviedb.org/movie/21380-virus)."""

    def test_accepts_a_bare_numeric_id(self) -> None:
        self.assertEqual(parse_tmdb_movie_id("21380"), "21380")

    def test_accepts_the_exact_url_a_user_pasted(self) -> None:
        self.assertEqual(
            parse_tmdb_movie_id("https://www.themoviedb.org/movie/21380-virus?language=da-DK"), "21380"
        )

    def test_accepts_a_url_with_no_query_string(self) -> None:
        self.assertEqual(parse_tmdb_movie_id("https://www.themoviedb.org/movie/21380-virus"), "21380")

    def test_accepts_a_url_without_a_slug(self) -> None:
        self.assertEqual(parse_tmdb_movie_id("https://www.themoviedb.org/movie/21380"), "21380")

    def test_accepts_a_url_without_www_or_scheme(self) -> None:
        self.assertEqual(parse_tmdb_movie_id("themoviedb.org/movie/21380-virus"), "21380")

    def test_is_case_insensitive(self) -> None:
        self.assertEqual(parse_tmdb_movie_id("HTTPS://WWW.THEMOVIEDB.ORG/MOVIE/21380-VIRUS"), "21380")

    def test_does_not_blindly_concatenate_digits_from_the_slug(self) -> None:
        # A movie literally titled "2012" would otherwise have its slug's
        # digits wrongly appended to the real id by a naive "strip every
        # non-digit character" approach (12345 + 2012 = 123452012).
        self.assertEqual(parse_tmdb_movie_id("https://www.themoviedb.org/movie/12345-2012"), "12345")

    def test_rejects_an_empty_reference(self) -> None:
        with self.assertRaises(ValueError):
            parse_tmdb_movie_id("")
        with self.assertRaises(ValueError):
            parse_tmdb_movie_id(None)

    def test_rejects_a_non_numeric_non_url_string(self) -> None:
        with self.assertRaises(ValueError):
            parse_tmdb_movie_id("Hell of the Dead")

    def test_rejects_a_string_with_embedded_digits_that_is_not_a_bare_id(self) -> None:
        with self.assertRaises(ValueError):
            parse_tmdb_movie_id("movie 123")

    def test_rejects_a_tv_url(self) -> None:
        # Deliberately narrow to /movie/ -- a /tv/ URL pasted by mistake
        # should fail loudly rather than resolving to an unrelated id.
        with self.assertRaises(ValueError):
            parse_tmdb_movie_id("https://www.themoviedb.org/tv/1396-breaking-bad")


if __name__ == "__main__":
    unittest.main()
