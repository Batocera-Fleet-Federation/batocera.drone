import json
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

from app.music.coverart_client import CoverArtClient, CoverArtUnavailableError


class FakeResponse:
    def __init__(self, payload: bytes, content_type: str = "application/json"):
        self._payload = payload
        self.headers = mock.Mock()
        self.headers.get_content_type = mock.Mock(return_value=content_type)

    def read(self, *_args):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FrontCoverUrlTests(unittest.TestCase):
    def test_finds_the_front_image_and_upgrades_http_to_https(self) -> None:
        # Real live bug caught manually testing against a real release
        # (Radiohead's "OK Computer"): Cover Art Archive's own JSON
        # reported a plain http:// image URL, which download_image's
        # https-only safety check then rejected outright -- silently
        # swallowed by the caller into "no art" for an album that actually
        # has real cover art on file. Fixed at the source here rather than
        # loosening that safety check.
        payload = json.dumps(
            {"images": [{"front": True, "image": "http://coverartarchive.org/release/rel-1/1.jpg"}]}
        ).encode("utf-8")
        client = CoverArtClient()
        with mock.patch("app.music.coverart_client.urlopen", return_value=FakeResponse(payload)):
            url = client.front_cover_url("rel-1")
        self.assertEqual(url, "https://coverartarchive.org/release/rel-1/1.jpg")

    def test_ignores_a_non_front_image(self) -> None:
        payload = json.dumps(
            {"images": [{"front": False, "back": True, "image": "https://coverartarchive.org/release/rel-1/back.jpg"}]}
        ).encode("utf-8")
        client = CoverArtClient()
        with mock.patch("app.music.coverart_client.urlopen", return_value=FakeResponse(payload)):
            url = client.front_cover_url("rel-1")
        self.assertIsNone(url)

    def test_no_images_at_all_returns_none(self) -> None:
        payload = json.dumps({"images": []}).encode("utf-8")
        client = CoverArtClient()
        with mock.patch("app.music.coverart_client.urlopen", return_value=FakeResponse(payload)):
            url = client.front_cover_url("rel-1")
        self.assertIsNone(url)

    def test_404_means_no_art_on_file_not_an_error(self) -> None:
        error = HTTPError("https://coverartarchive.org/release/rel-1", 404, "Not Found", None, None)
        client = CoverArtClient()
        with mock.patch("app.music.coverart_client.urlopen", side_effect=error):
            url = client.front_cover_url("rel-1")
        self.assertIsNone(url)

    def test_empty_release_mbid_returns_none_without_a_request(self) -> None:
        client = CoverArtClient()
        with mock.patch("app.music.coverart_client.urlopen") as urlopen_mock:
            url = client.front_cover_url("")
        self.assertIsNone(url)
        urlopen_mock.assert_not_called()

    def test_other_http_error_raises_unavailable(self) -> None:
        error = HTTPError("https://coverartarchive.org/release/rel-1", 500, "Server Error", None, None)
        client = CoverArtClient()
        with mock.patch("app.music.coverart_client.urlopen", side_effect=error):
            with self.assertRaises(CoverArtUnavailableError):
                client.front_cover_url("rel-1")

    def test_network_failure_raises_unavailable(self) -> None:
        client = CoverArtClient()
        with mock.patch("app.music.coverart_client.urlopen", side_effect=URLError("no route")):
            with self.assertRaises(CoverArtUnavailableError):
                client.front_cover_url("rel-1")


class DownloadImageTests(unittest.TestCase):
    def test_rejects_a_non_https_url(self) -> None:
        client = CoverArtClient()
        with self.assertRaises(ValueError):
            client.download_image("http://coverartarchive.org/release/rel-1/1.jpg")

    def test_downloads_real_image_bytes(self) -> None:
        client = CoverArtClient()
        with mock.patch("app.music.coverart_client.urlopen", return_value=FakeResponse(b"\xff\xd8\xff", "image/jpeg")):
            data, content_type = client.download_image("https://coverartarchive.org/release/rel-1/1.jpg")
        self.assertEqual(data, b"\xff\xd8\xff")
        self.assertEqual(content_type, "image/jpeg")

    def test_a_non_image_response_raises_value_error(self) -> None:
        client = CoverArtClient()
        with mock.patch("app.music.coverart_client.urlopen", return_value=FakeResponse(b"<html>", "text/html")):
            with self.assertRaises(ValueError):
                client.download_image("https://coverartarchive.org/release/rel-1/1.jpg")

    def test_http_error_raises_unavailable(self) -> None:
        error = HTTPError("https://coverartarchive.org/release/rel-1/1.jpg", 500, "Server Error", None, None)
        client = CoverArtClient()
        with mock.patch("app.music.coverart_client.urlopen", side_effect=error):
            with self.assertRaises(CoverArtUnavailableError):
                client.download_image("https://coverartarchive.org/release/rel-1/1.jpg")


if __name__ == "__main__":
    unittest.main()
