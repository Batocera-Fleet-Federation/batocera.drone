import json
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

from app.music import wikimedia_client


class FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self, *_args):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class CommonsFileTitleTests(unittest.TestCase):
    def test_extracts_the_file_title_from_a_wiki_path(self) -> None:
        title = wikimedia_client._commons_file_title("https://commons.wikimedia.org/wiki/File:Sample_Artist.jpg")
        self.assertEqual(title, "File:Sample_Artist.jpg")

    def test_non_wikimedia_host_returns_none(self) -> None:
        self.assertIsNone(wikimedia_client._commons_file_title("https://example.com/wiki/File:X.jpg"))

    def test_missing_wiki_path_returns_none(self) -> None:
        self.assertIsNone(wikimedia_client._commons_file_title("https://commons.wikimedia.org/some/other/path"))

    def test_a_non_file_page_returns_none(self) -> None:
        self.assertIsNone(wikimedia_client._commons_file_title("https://commons.wikimedia.org/wiki/Category:Musicians"))

    def test_empty_url_returns_none(self) -> None:
        self.assertIsNone(wikimedia_client._commons_file_title(""))


class ResolveImageUrlTests(unittest.TestCase):
    def test_resolves_the_real_upload_url(self) -> None:
        payload = json.dumps(
            {
                "query": {
                    "pages": {
                        "123": {
                            "imageinfo": [{"url": "https://upload.wikimedia.org/wikipedia/commons/x/Sample_Artist.jpg"}],
                        },
                    },
                },
            }
        ).encode("utf-8")
        with mock.patch("app.music.wikimedia_client.urlopen", return_value=FakeResponse(payload)) as urlopen_mock:
            result = wikimedia_client.resolve_image_url("https://commons.wikimedia.org/wiki/File:Sample_Artist.jpg")
        self.assertEqual(result, "https://upload.wikimedia.org/wikipedia/commons/x/Sample_Artist.jpg")
        self.assertIn("File%3ASample_Artist.jpg", urlopen_mock.call_args[0][0].full_url)

    def test_unrecognized_url_never_makes_a_request(self) -> None:
        with mock.patch("app.music.wikimedia_client.urlopen") as urlopen_mock:
            result = wikimedia_client.resolve_image_url("https://example.com/not-commons.jpg")
        self.assertIsNone(result)
        urlopen_mock.assert_not_called()

    def test_missing_page_returns_none(self) -> None:
        payload = json.dumps({"query": {"pages": {"-1": {"missing": ""}}}}).encode("utf-8")
        with mock.patch("app.music.wikimedia_client.urlopen", return_value=FakeResponse(payload)):
            result = wikimedia_client.resolve_image_url("https://commons.wikimedia.org/wiki/File:Gone.jpg")
        self.assertIsNone(result)

    def test_network_failure_returns_none_not_a_crash(self) -> None:
        with mock.patch("app.music.wikimedia_client.urlopen", side_effect=URLError("no route")):
            result = wikimedia_client.resolve_image_url("https://commons.wikimedia.org/wiki/File:X.jpg")
        self.assertIsNone(result)

    def test_http_error_returns_none_not_a_crash(self) -> None:
        error = HTTPError("https://commons.wikimedia.org/w/api.php", 500, "Server Error", None, None)
        with mock.patch("app.music.wikimedia_client.urlopen", side_effect=error):
            result = wikimedia_client.resolve_image_url("https://commons.wikimedia.org/wiki/File:X.jpg")
        self.assertIsNone(result)

    def test_malformed_json_returns_none(self) -> None:
        with mock.patch("app.music.wikimedia_client.urlopen", return_value=FakeResponse(b"not json")):
            result = wikimedia_client.resolve_image_url("https://commons.wikimedia.org/wiki/File:X.jpg")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
