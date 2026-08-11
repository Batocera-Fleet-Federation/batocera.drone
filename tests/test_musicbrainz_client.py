import json
import unittest
from unittest import mock
from urllib.error import HTTPError

from app.music.musicbrainz_client import (
    MusicBrainzClient,
    MusicBrainzNotFoundError,
    MusicBrainzUnavailableError,
)


class FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self, *_args):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class MusicBrainzClient503RetryTests(unittest.TestCase):
    """Mirrors test_tmdb_client.py's TmdbClient429RetryTests -- same retry
    shape, same reason: a bulk scrape can trip transient rate-limiting, and
    a 503/429 needs to be retried with backoff rather than aborting the
    whole run. _throttle is mocked out in every test here (it's a real
    ~1req/sec module-level sleep, unrelated to what these tests exercise)
    so only the retry-backoff sleep is under test."""

    def _rate_limited(self, retry_after=None):
        headers = {"Retry-After": retry_after} if retry_after is not None else {}
        return HTTPError("https://musicbrainz.org/ws/2/release/", 503, "Service Unavailable", headers, None)

    def test_succeeds_after_one_retry(self) -> None:
        payload = json.dumps({"releases": []}).encode("utf-8")
        client = MusicBrainzClient()
        with mock.patch("app.music.musicbrainz_client._throttle"):
            with mock.patch("app.music.musicbrainz_client.urlopen", side_effect=[self._rate_limited(), FakeResponse(payload)]):
                with mock.patch("app.music.musicbrainz_client.time.sleep") as sleep_mock:
                    results = client.search_release("halloween")
        self.assertEqual(results, [])
        sleep_mock.assert_called_once()

    def test_honors_retry_after_header(self) -> None:
        payload = json.dumps({"releases": []}).encode("utf-8")
        client = MusicBrainzClient()
        with mock.patch("app.music.musicbrainz_client._throttle"):
            with mock.patch("app.music.musicbrainz_client.urlopen", side_effect=[self._rate_limited(retry_after="5"), FakeResponse(payload)]):
                with mock.patch("app.music.musicbrainz_client.time.sleep") as sleep_mock:
                    client.search_release("halloween")
        sleep_mock.assert_called_once_with(5.0)

    def test_retry_after_is_capped_at_the_maximum_delay(self) -> None:
        # Real live bug: an uncapped Retry-After left a bulk-scrape
        # background thread blocked in this exact sleep for 5+ hours, deaf
        # to Stop the whole time (see MUSICBRAINZ_MAX_RETRY_DELAY_SECONDS).
        payload = json.dumps({"releases": []}).encode("utf-8")
        client = MusicBrainzClient()
        with mock.patch("app.music.musicbrainz_client._throttle"):
            with mock.patch("app.music.musicbrainz_client.urlopen", side_effect=[self._rate_limited(retry_after="7200"), FakeResponse(payload)]):
                with mock.patch("app.music.musicbrainz_client.time.sleep") as sleep_mock:
                    client.search_release("halloween")
        sleep_mock.assert_called_once_with(60.0)

    def test_falls_back_to_backoff_when_retry_after_is_not_a_plain_number(self) -> None:
        payload = json.dumps({"releases": []}).encode("utf-8")
        client = MusicBrainzClient()
        with mock.patch("app.music.musicbrainz_client._throttle"):
            with mock.patch("app.music.musicbrainz_client.urlopen", side_effect=[self._rate_limited(retry_after="not-a-number"), FakeResponse(payload)]):
                with mock.patch("app.music.musicbrainz_client.time.sleep") as sleep_mock:
                    client.search_release("halloween")
        sleep_mock.assert_called_once_with(1.0)

    def test_exhausting_retries_raises_musicbrainz_unavailable_not_a_crash(self) -> None:
        client = MusicBrainzClient()
        with mock.patch("app.music.musicbrainz_client._throttle"):
            with mock.patch("app.music.musicbrainz_client.urlopen", side_effect=self._rate_limited()):
                with mock.patch("app.music.musicbrainz_client.time.sleep") as sleep_mock:
                    with self.assertRaises(MusicBrainzUnavailableError):
                        client.search_release("halloween")
        self.assertEqual(sleep_mock.call_count, 3)

    def test_404_never_retries_and_raises_not_found(self) -> None:
        client = MusicBrainzClient()
        error = HTTPError("https://musicbrainz.org/ws/2/release/deadbeef", 404, "Not Found", None, None)
        with mock.patch("app.music.musicbrainz_client._throttle"):
            with mock.patch("app.music.musicbrainz_client.urlopen", side_effect=error):
                with mock.patch("app.music.musicbrainz_client.time.sleep") as sleep_mock:
                    with self.assertRaises(MusicBrainzNotFoundError):
                        client.release_lookup("deadbeef")
        sleep_mock.assert_not_called()


class ReleaseLookupArtistMbidTests(unittest.TestCase):
    """release_lookup's artist_mbid field feeds _apply_release_to_group's
    best-effort artist-photo fetch (see metadata_manager.py) -- exercised
    here independent of that, since it's this client's own JSON-shape
    knowledge (artist-credit[0].artist.id)."""

    def test_extracts_the_primary_artist_mbid_from_artist_credit(self) -> None:
        payload = json.dumps(
            {
                "id": "release-1",
                "title": "Sample Album",
                "artist-credit": [{"name": "Sample Artist", "artist": {"id": "artist-1", "name": "Sample Artist"}}],
                "date": "2001-05-01",
                "genres": [],
                "release-group": {"id": "rg-1"},
                "media": [],
            }
        ).encode("utf-8")
        client = MusicBrainzClient()
        with mock.patch("app.music.musicbrainz_client._throttle"):
            with mock.patch("app.music.musicbrainz_client.urlopen", return_value=FakeResponse(payload)):
                result = client.release_lookup("release-1")
        self.assertEqual(result["artist_mbid"], "artist-1")

    def test_missing_artist_credit_is_an_empty_string_not_a_crash(self) -> None:
        payload = json.dumps({"id": "release-1", "title": "No Artist", "media": []}).encode("utf-8")
        client = MusicBrainzClient()
        with mock.patch("app.music.musicbrainz_client._throttle"):
            with mock.patch("app.music.musicbrainz_client.urlopen", return_value=FakeResponse(payload)):
                result = client.release_lookup("release-1")
        self.assertEqual(result["artist_mbid"], "")


class ArtistLookupTests(unittest.TestCase):
    """artist_lookup's sole purpose is finding a MusicBrainz "image" url
    relation (typically a Wikimedia Commons file page) -- see
    music/wikimedia_client.py for turning that into a real image."""

    def test_finds_an_image_relation_pointing_at_a_commons_file_page(self) -> None:
        payload = json.dumps(
            {
                "id": "artist-1",
                "relations": [
                    {"type": "official homepage", "url": {"resource": "https://example.com"}},
                    {"type": "image", "url": {"resource": "https://commons.wikimedia.org/wiki/File:Sample_Artist.jpg"}},
                ],
            }
        ).encode("utf-8")
        client = MusicBrainzClient()
        with mock.patch("app.music.musicbrainz_client._throttle"):
            with mock.patch("app.music.musicbrainz_client.urlopen", return_value=FakeResponse(payload)):
                result = client.artist_lookup("artist-1")
        self.assertEqual(result["image_commons_url"], "https://commons.wikimedia.org/wiki/File:Sample_Artist.jpg")

    def test_no_image_relation_returns_none_not_an_error(self) -> None:
        payload = json.dumps(
            {"id": "artist-1", "relations": [{"type": "official homepage", "url": {"resource": "https://example.com"}}]}
        ).encode("utf-8")
        client = MusicBrainzClient()
        with mock.patch("app.music.musicbrainz_client._throttle"):
            with mock.patch("app.music.musicbrainz_client.urlopen", return_value=FakeResponse(payload)):
                result = client.artist_lookup("artist-1")
        self.assertIsNone(result["image_commons_url"])

    def test_no_relations_at_all_returns_none(self) -> None:
        payload = json.dumps({"id": "artist-1"}).encode("utf-8")
        client = MusicBrainzClient()
        with mock.patch("app.music.musicbrainz_client._throttle"):
            with mock.patch("app.music.musicbrainz_client.urlopen", return_value=FakeResponse(payload)):
                result = client.artist_lookup("artist-1")
        self.assertIsNone(result["image_commons_url"])

    def test_empty_artist_mbid_raises_value_error(self) -> None:
        client = MusicBrainzClient()
        with self.assertRaises(ValueError):
            client.artist_lookup("")

    def test_404_raises_not_found(self) -> None:
        client = MusicBrainzClient()
        error = HTTPError("https://musicbrainz.org/ws/2/artist/deadbeef", 404, "Not Found", None, None)
        with mock.patch("app.music.musicbrainz_client._throttle"):
            with mock.patch("app.music.musicbrainz_client.urlopen", side_effect=error):
                with mock.patch("app.music.musicbrainz_client.time.sleep") as sleep_mock:
                    with self.assertRaises(MusicBrainzNotFoundError):
                        client.artist_lookup("deadbeef")
        sleep_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
