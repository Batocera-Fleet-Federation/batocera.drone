import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.storage.music_store as music_store
from app.common.settings import Settings
from app.music import metadata_manager
from app.music.musicbrainz_client import MusicBrainzUnavailableError


def _build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "MOVIES_ROOT": str(root / "movies"),
        "MUSIC_ROOT": str(root / "music"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "music-metadata-manager-test",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


def _write_track(root: Path, rel: str, data: bytes = b"x") -> Path:
    path = root / "music" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


_ALBUM_RELEASE = {
    "release_mbid": "release-1",
    "title": "Sample Album",
    "artist": "Sample Artist",
    "artist_mbid": "artist-1",
    "date": "2001-05-01",
    "genres": ["Rock"],
    "release_group_mbid": "rg-1",
    "tracks": [
        {"disc_number": 1, "track_number": 1, "title": "Track One", "recording_mbid": "rec-1", "length_ms": 180000},
        {"disc_number": 1, "track_number": 2, "title": "Track Two", "recording_mbid": "rec-2", "length_ms": 200000},
    ],
}
class FakeMusicBrainzClient:
    def __init__(
        self, *, release_search_results=None, releases=None,
        raise_on_lookup=None, artist_lookups=None,
    ):
        self._release_search_results = release_search_results or []
        self._releases = releases or {}
        self._raise_on_lookup = raise_on_lookup or {}
        self._artist_lookups = artist_lookups or {}
        self.lookup_calls = []
        self.artist_lookup_calls = []

    def search_release(self, query, limit=10):
        return self._release_search_results

    def release_lookup(self, release_mbid):
        self.lookup_calls.append(release_mbid)
        if release_mbid in self._raise_on_lookup:
            raise self._raise_on_lookup[release_mbid]
        return self._releases[release_mbid]

    def artist_lookup(self, artist_mbid):
        self.artist_lookup_calls.append(artist_mbid)
        if artist_mbid in self._raise_on_lookup:
            raise self._raise_on_lookup[artist_mbid]
        return self._artist_lookups.get(artist_mbid, {"artist_mbid": artist_mbid, "image_commons_url": None})


class FakeCoverArtClient:
    def __init__(self, *, front_url="https://coverartarchive.org/release-1/front.jpg", raise_error=None):
        self._front_url = front_url
        self._raise_error = raise_error
        self.downloaded_urls = []

    def front_cover_url(self, release_mbid):
        if self._raise_error:
            raise self._raise_error
        return self._front_url

    def download_image(self, url):
        self.downloaded_urls.append(url)
        return (f"bytes-for-{url}".encode(), "image/jpeg")


class ApplyAlbumTests(unittest.TestCase):
    def test_applies_release_art_and_metadata_to_every_track_in_the_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Sample Artist/Sample Album/01 - Track One.mp3")
            _write_track(root, "Sample Artist/Sample Album/02 - Track Two.mp3")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_keys = [row["entry_key"] for row in music_store.list_music(settings.music_root)]

            client = FakeMusicBrainzClient(releases={"release-1": _ALBUM_RELEASE})
            cover_client = FakeCoverArtClient()
            result = metadata_manager.apply_album(settings, entry_keys, "release-1", client=client, cover_client=cover_client)

            self.assertEqual(result["updated"], 2)
            self.assertTrue(result["has_art"])
            self.assertEqual(sorted(result["entry_keys"]), sorted(entry_keys))
            # One release -> one shared download, not one per track.
            self.assertEqual(len(cover_client.downloaded_urls), 1)

            metas = [music_store.get_music_metadata(settings.music_root, key) for key in entry_keys]
            for metadata in metas:
                # Never overwrites a track's own filename-derived title.
                self.assertEqual(metadata["title"], "")
                self.assertEqual(metadata["provider"], "musicbrainz")
                self.assertEqual(metadata["provider_id"], "release-1")
                self.assertEqual(metadata["artist"], "Sample Artist")
                self.assertEqual(metadata["album"], "Sample Album")
                self.assertEqual(metadata["genres"], ["Rock"])
                self.assertTrue(metadata["art_relative_path"])
            self.assertEqual(metas[0]["art_relative_path"], metas[1]["art_relative_path"])

    def test_unknown_entry_keys_raise_music_not_found_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(metadata_manager.MusicNotFoundError):
                metadata_manager.apply_album(settings, ["deadbeef"], "release-1", client=FakeMusicBrainzClient())

    def test_missing_cover_art_does_not_fail_the_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Sample Artist/Sample Album/01 - Track One.mp3")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]

            client = FakeMusicBrainzClient(releases={"release-1": _ALBUM_RELEASE})
            cover_client = FakeCoverArtClient(front_url=None)
            result = metadata_manager.apply_album(settings, [entry_key], "release-1", client=client, cover_client=cover_client)
            self.assertFalse(result["has_art"])
            metadata = music_store.get_music_metadata(settings.music_root, entry_key)
            self.assertIsNone(metadata["art_relative_path"])


class ArtistArtTests(unittest.TestCase):
    def test_applies_artist_photo_when_musicbrainz_has_an_image_relation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Sample Artist/Sample Album/01 - Track One.mp3")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]

            client = FakeMusicBrainzClient(
                releases={"release-1": _ALBUM_RELEASE},
                artist_lookups={
                    "artist-1": {
                        "artist_mbid": "artist-1",
                        "image_commons_url": "https://commons.wikimedia.org/wiki/File:Sample_Artist.jpg",
                    },
                },
            )
            with mock.patch(
                "app.music.metadata_manager._wikimedia_client.resolve_image_url",
                return_value="https://upload.wikimedia.org/x/Sample_Artist.jpg",
            ) as resolve_mock:
                result = metadata_manager.apply_album(settings, [entry_key], "release-1", client=client, cover_client=FakeCoverArtClient())

            self.assertTrue(result["has_artist_art"])
            resolve_mock.assert_called_once_with("https://commons.wikimedia.org/wiki/File:Sample_Artist.jpg")
            metadata = music_store.get_music_metadata(settings.music_root, entry_key)
            self.assertTrue(metadata["artist_art_relative_path"])
            self.assertNotEqual(metadata["artist_art_relative_path"], metadata["art_relative_path"])
            self.assertEqual(client.artist_lookup_calls, ["artist-1"])

    def test_no_image_relation_leaves_artist_art_empty_without_calling_wikimedia(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Sample Artist/Sample Album/01 - Track One.mp3")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]

            client = FakeMusicBrainzClient(releases={"release-1": _ALBUM_RELEASE})  # no artist_lookups configured
            with mock.patch("app.music.metadata_manager._wikimedia_client.resolve_image_url") as resolve_mock:
                result = metadata_manager.apply_album(settings, [entry_key], "release-1", client=client, cover_client=FakeCoverArtClient())
            self.assertFalse(result["has_artist_art"])
            resolve_mock.assert_not_called()
            metadata = music_store.get_music_metadata(settings.music_root, entry_key)
            self.assertIsNone(metadata["artist_art_relative_path"])

    def test_artist_lookup_failure_does_not_fail_the_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Sample Artist/Sample Album/01 - Track One.mp3")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]

            client = FakeMusicBrainzClient(
                releases={"release-1": _ALBUM_RELEASE},
                raise_on_lookup={"artist-1": MusicBrainzUnavailableError("boom")},
            )
            result = metadata_manager.apply_album(settings, [entry_key], "release-1", client=client, cover_client=FakeCoverArtClient())
            self.assertFalse(result["has_artist_art"])
            self.assertTrue(result["has_art"])  # album art still succeeded regardless

    def test_unresolvable_commons_url_leaves_artist_art_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Sample Artist/Sample Album/01 - Track One.mp3")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]

            client = FakeMusicBrainzClient(
                releases={"release-1": _ALBUM_RELEASE},
                artist_lookups={"artist-1": {"artist_mbid": "artist-1", "image_commons_url": "https://commons.wikimedia.org/wiki/File:Gone.jpg"}},
            )
            with mock.patch("app.music.metadata_manager._wikimedia_client.resolve_image_url", return_value=None):
                result = metadata_manager.apply_album(settings, [entry_key], "release-1", client=client, cover_client=FakeCoverArtClient())
            self.assertFalse(result["has_artist_art"])


class DeleteTests(unittest.TestCase):
    def test_delete_metadata_is_idempotent_and_removes_artwork(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Sample Artist/Sample Album/01 - Track One.mp3")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            metadata_manager.apply_album(
                settings, [entry_key], "release-1",
                client=FakeMusicBrainzClient(releases={"release-1": _ALBUM_RELEASE}),
                cover_client=FakeCoverArtClient(),
            )
            metadata = music_store.get_music_metadata(settings.music_root, entry_key)
            art_path = Path(settings.music_root) / metadata["art_relative_path"]
            self.assertTrue(art_path.exists())

            result = metadata_manager.delete_metadata(settings, entry_key)
            self.assertEqual(result, {"deleted": True})
            self.assertFalse(art_path.exists())
            self.assertIsNone(music_store.get_music_metadata(settings.music_root, entry_key))
            self.assertEqual(metadata_manager.delete_metadata(settings, entry_key), {"deleted": False})

    def test_delete_music_removes_file_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = _write_track(root, "Sample Artist/Sample Album/01 - Track One.mp3")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            result = metadata_manager.delete_music(settings, entry_key)
            self.assertTrue(result["deleted"])
            self.assertFalse(path.exists())


class DeleteAlbumMetadataTests(unittest.TestCase):
    def test_removes_metadata_for_every_track_and_unlinks_the_shared_art_file_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Sample Artist/Sample Album/01 - Track One.mp3")
            _write_track(root, "Sample Artist/Sample Album/02 - Track Two.mp3")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_keys = [row["entry_key"] for row in music_store.list_music(settings.music_root)]
            metadata_manager.apply_album(
                settings, entry_keys, "release-1",
                client=FakeMusicBrainzClient(releases={"release-1": _ALBUM_RELEASE}),
                cover_client=FakeCoverArtClient(),
            )
            metadata = music_store.get_music_metadata(settings.music_root, entry_keys[0])
            art_path = Path(settings.music_root) / metadata["art_relative_path"]
            self.assertTrue(art_path.exists())

            result = metadata_manager.delete_album_metadata(settings, entry_keys)
            self.assertEqual(result, {"deleted": 2})
            self.assertFalse(art_path.exists())
            for entry_key in entry_keys:
                self.assertIsNone(music_store.get_music_metadata(settings.music_root, entry_key))

    def test_unknown_entry_keys_are_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            result = metadata_manager.delete_album_metadata(settings, ["deadbeef"])
            self.assertEqual(result, {"deleted": 0})


class SearchAlbumDefaultQueryTests(unittest.TestCase):
    def test_release_only_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            client = FakeMusicBrainzClient(release_search_results=[{"release_mbid": "release-1", "title": "Sample Album"}])
            outcome = metadata_manager.search_album_default_query(settings, "Sample Artist", "Sample Album", client=client)
            self.assertEqual(outcome["results"][0]["kind"], "release")

    def test_no_album_returns_no_results_without_calling_musicbrainz(self):
        # search_candidates(artist, "", "") yields no candidates at all when
        # there's no album to search by -- confirms the album page's search
        # card never makes a pointless call for the "Singles" pseudo-group.
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            client = FakeMusicBrainzClient(release_search_results=[{"release_mbid": "should-not-be-used"}])
            outcome = metadata_manager.search_album_default_query(settings, "Solo Singer", "", client=client)
            self.assertEqual(outcome["results"], [])

    def test_no_results_anywhere_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            outcome = metadata_manager.search_album_default_query(settings, "Nobody", "Nothing", client=FakeMusicBrainzClient())
            self.assertEqual(outcome["results"], [])


if __name__ == "__main__":
    unittest.main()
