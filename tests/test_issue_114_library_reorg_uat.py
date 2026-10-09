"""User-level acceptance tests for issue #114: artwork survives a library reorganization.

Runs the same sequence the poller does -- scan/sync movies and music, scrape,
move files around, sync again -- and then asks the real artwork route for the
images. Covers the acceptance criteria: moved movies and shows keep their
posters (200), moved albums keep their covers (200), orphan rows are reported
as a remainder count, and a transient DB open failure during the music
artwork route answers 503 rather than 500.
"""

import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.storage.movies_store as movies_store
import app.storage.music_store as music_store
from app.drone_api import Settings
from app.web import handlers_movies, handlers_music


def _build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "MOVIES_ROOT": str(root / "movies"),
        "SHOWS_ROOT": str(root / "shows"),
        "MUSIC_ROOT": str(root / "music"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "issue-114-uat",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class _ArtworkResponder:
    """Records what the artwork routes would send, for the real mixin methods to call."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.headers = {}
        self.wfile = io.BytesIO()
        self.response_status = None
        self.json_response = None

    def _stream_cached_image(self, path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError()
        self.response_status = 200
        self.wfile.write(path.read_bytes())

    def _send_json(self, status_code, payload, cache_key=None, extra_headers=None) -> None:
        self.json_response = (status_code, payload)


class _MovieArtworkResponder(handlers_movies.HandlersMoviesMixin, _ArtworkResponder):
    pass


class _MusicArtworkResponder(handlers_music.HandlersMusicMixin, _ArtworkResponder):
    pass


class LibraryReorganizationUatTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = _build_settings(self.root)
        self.movies_root = self.settings.movies_root
        self.shows_root = self.settings.shows_root
        self.music_root = self.settings.music_root
        for directory in (self.movies_root, self.shows_root, self.music_root):
            Path(directory).mkdir(parents=True, exist_ok=True)
        self._db_env = os.environ.get("DRONE_STATE_DATABASE_FILE")
        os.environ["DRONE_STATE_DATABASE_FILE"] = str(self.root / "state.sqlite3")

    def tearDown(self):
        if self._db_env is None:
            os.environ.pop("DRONE_STATE_DATABASE_FILE", None)
        else:
            os.environ["DRONE_STATE_DATABASE_FILE"] = self._db_env
        self._tmp.cleanup()

    def _write(self, path: Path, data: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def _movie_key(self, file_path: str) -> str:
        return next(item["entry_key"] for item in movies_store.list_movies(self.movies_root) if item["file_path"] == file_path)

    def _track_key(self, file_path: str) -> str:
        return next(item["entry_key"] for item in music_store.list_music(self.music_root) if item["file_path"] == file_path)

    def _sync_all(self) -> dict:
        movies = movies_store.sync_movies_cache(self.movies_root, self.shows_root)
        music = music_store.sync_music_cache(self.music_root)
        return {"movies": movies, "music": music}

    def test_moved_movies_and_shows_keep_their_posters_and_report_orphan_remainder(self):
        # Legacy root-level movie with a scraper sidecar, plus a show folder.
        self._write(self.movies_root / "Vacation.mp4", b"vacation")
        self._write(self.movies_root / "images" / "Vacation-tmdb-poster.jpg", b"vacation-poster")
        self._write(self.shows_root / "Firefly (2002)" / "Season 01" / "Firefly - S01E01.mkv", b"episode")
        self._write(self.shows_root / "Firefly (2002)" / "Season 01" / "images" / "Firefly - S01E01-tmdb-poster.jpg", b"ep-poster")
        self._sync_all()

        movie_key = self._movie_key("Vacation.mp4")
        episode_key = self._movie_key("Shows/Firefly (2002)/Season 01/Firefly - S01E01.mkv")
        movies_store.save_movie_metadata(
            self.movies_root, movie_key, provider="tmdb", provider_id="1", title="Vacation",
            poster_relative_path="images/Vacation-tmdb-poster.jpg", backdrop_relative_path=None, extra={},
        )
        movies_store.save_movie_metadata(
            self.movies_root, episode_key, provider="tmdb_tv", provider_id="2", title="Serenity",
            poster_relative_path=None, backdrop_relative_path=None, extra={},
        )

        # Reorganize: move the movie into a genre folder (with its art), rename the show folder.
        (self.movies_root / "comedy").mkdir()
        (self.movies_root / "Vacation.mp4").rename(self.movies_root / "comedy" / "Vacation.mp4")
        (self.movies_root / "images").rename(self.movies_root / "comedy" / "images")
        (self.shows_root / "Firefly (2002)").rename(self.shows_root / "Firefly")
        result = self._sync_all()["movies"]

        self.assertEqual(result["metadata_orphaned"], 2)
        self.assertEqual(result["metadata_rekeyed"], 2)
        self.assertEqual(result["metadata_unmatched"], 0)

        new_movie_key = self._movie_key("comedy/Vacation.mp4")
        responder = _MovieArtworkResponder(self.settings)
        responder._handle_movie_artwork(new_movie_key, "poster")
        self.assertEqual(responder.response_status, 200)
        self.assertEqual(responder.wfile.getvalue(), b"vacation-poster")

        new_episode_key = self._movie_key("Shows/Firefly/Season 01/Firefly - S01E01.mkv")
        responder = _MovieArtworkResponder(self.settings)
        responder._handle_movie_artwork(new_episode_key, "poster")
        self.assertEqual(responder.response_status, 200)
        self.assertEqual(responder.wfile.getvalue(), b"ep-poster")

    def test_moved_album_keeps_its_cover_after_resync(self):
        track = self._write(self.music_root / "Artist" / "Album" / "01 Song.mp3", b"audio")
        self._write(track.parent / "images" / "album-cover.jpg", b"cover")
        self._sync_all()
        old_key = self._track_key("Artist/Album/01 Song.mp3")
        music_store.save_music_metadata(
            self.music_root, old_key, provider="musicbrainz", provider_id="1", title="Song",
            art_relative_path="Artist/Album/images/album-cover.jpg", artist_art_relative_path=None, extra={},
        )

        (self.music_root / "Artist" / "Album").rename(self.music_root / "Artist" / "Album (Reissue)")
        result = self._sync_all()["music"]

        new_key = self._track_key("Artist/Album (Reissue)/01 Song.mp3")
        self.assertEqual(result["metadata_rekeyed"], 1)
        responder = _MusicArtworkResponder(self.settings)
        responder._handle_music_artwork(new_key, "art")
        self.assertEqual(responder.response_status, 200)
        self.assertEqual(responder.wfile.getvalue(), b"cover")

    def test_transient_music_db_failure_during_poll_answers_503_then_recovers(self):
        track = self._write(self.music_root / "Artist" / "Album" / "01 Song.mp3", b"audio")
        self._write(track.parent / "images" / "album-cover.jpg", b"cover")
        self._sync_all()
        entry_key = self._track_key("Artist/Album/01 Song.mp3")

        failure = sqlite3.OperationalError("unable to open database file")
        responder = _MusicArtworkResponder(self.settings)
        with mock.patch.object(music_store, "get_music_metadata", side_effect=failure):
            responder._handle_music_artwork(entry_key, "art")
        self.assertEqual(responder.json_response[0], 503)

        retry = _MusicArtworkResponder(self.settings)
        retry._handle_music_artwork(entry_key, "art")
        self.assertEqual(retry.response_status, 200)


if __name__ == "__main__":
    unittest.main()
