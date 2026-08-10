import os
import tempfile
import unittest
from pathlib import Path

import app.storage.music_store as music_store


class MusicStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.userdata = Path(self._tmp.name)
        self.music_root = self.userdata / "music"
        self.music_root.mkdir(parents=True)
        # Keep the SQLite cache inside the temp dir.
        self._db_env = os.environ.get("DRONE_STATE_DATABASE_FILE")
        os.environ["DRONE_STATE_DATABASE_FILE"] = str(self.userdata / "system" / "drone-app" / "cache.sqlite3")

    def tearDown(self):
        if self._db_env is None:
            os.environ.pop("DRONE_STATE_DATABASE_FILE", None)
        else:
            os.environ["DRONE_STATE_DATABASE_FILE"] = self._db_env
        self._tmp.cleanup()

    def _write(self, rel, data=b"music-data"):
        path = self.music_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_scan_has_no_system_dimension(self):
        self._write("Artist/Song.mp3")
        entries = music_store.scan_music(self.music_root)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.file_path, "Artist/Song.mp3")
        self.assertEqual(entry.track_name, "Song.mp3")
        self.assertFalse(hasattr(entry, "system"))
        self.assertTrue(entry.fingerprint)

    def test_scan_traverses_artist_album_disc_nesting(self):
        self._write("Solo Artist/Single.mp3")
        self._write("Band/Album One/01 - Track.mp3")
        self._write("Band/Double Album/CD1/01 - Track.mp3")
        self._write("Band/Double Album/CD2/01 - Track.mp3")
        entries = music_store.scan_music(self.music_root)
        self.assertEqual(
            sorted(entry.file_path for entry in entries),
            [
                "Band/Album One/01 - Track.mp3",
                "Band/Double Album/CD1/01 - Track.mp3",
                "Band/Double Album/CD2/01 - Track.mp3",
                "Solo Artist/Single.mp3",
            ],
        )
        music_store.sync_music_cache(self.music_root)
        listed_paths = sorted(item["file_path"] for item in music_store.list_music(self.music_root))
        self.assertEqual(listed_paths, sorted(entry.file_path for entry in entries))

    def test_scan_ignores_non_audio_files(self):
        # A scraper cover-art image, a .cue sidecar, and a stray .db file must
        # never be treated as a track -- only recognized audio containers count.
        self._write("Artist/Album/Track.mp3")
        self._write("Artist/Album/cover.jpg")
        self._write("Artist/Album/Album.cue")
        self._write("Artist/Album/thumbs.db")
        entries = music_store.scan_music(self.music_root)
        self.assertEqual([entry.file_path for entry in entries], ["Artist/Album/Track.mp3"])

    def test_scan_recognizes_common_audio_containers(self):
        for name in ("A.mp3", "B.flac", "C.ogg", "D.opus", "E.m4a", "F.wav", "G.wma", "H.aac", "I.aiff", "J.ape"):
            self._write(f"Artist/{name}")
        entries = music_store.scan_music(self.music_root)
        self.assertEqual(len(entries), 10)

    def test_scan_extension_match_is_case_insensitive(self):
        self._write("Artist/Track.MP3")
        entries = music_store.scan_music(self.music_root)
        self.assertEqual([entry.file_path for entry in entries], ["Artist/Track.MP3"])

    def test_fingerprint_matches_rom_repository_algorithm(self):
        path = self._write("Artist/Track.mp3", b"x" * 1000)
        from app.drone_api import RomRepository

        self.assertEqual(music_store.build_music_fingerprint(path), RomRepository.build_fingerprint(path))

    def test_sync_reports_created_updated_deleted(self):
        self._write("Artist/A.mp3", b"one")
        self._write("Artist/B.mp3", b"two")
        first = music_store.sync_music_cache(self.music_root)
        self.assertEqual(first["created"], 2)
        self.assertEqual(first["updated"], 0)
        self.assertEqual(first["total"], 2)

        second = music_store.sync_music_cache(self.music_root)
        self.assertEqual((second["created"], second["updated"], second["deleted"]), (0, 0, 0))

        self._write("Artist/A.mp3", b"one-changed-and-longer")
        (self.music_root / "Artist" / "B.mp3").unlink()
        third = music_store.sync_music_cache(self.music_root)
        self.assertEqual(third["updated"], 1)
        self.assertEqual(third["deleted"], 1)

    def test_pending_changes_queue_and_clear(self):
        self._write("Artist/A.mp3")
        music_store.sync_music_cache(self.music_root)
        pending = music_store.read_pending_changes(self.music_root)
        self.assertEqual(len(pending["music"]), 1)
        self.assertEqual(pending["music"][0]["file_path"], "Artist/A.mp3")

        music_store.clear_pending_changes(self.music_root)
        self.assertEqual(music_store.read_pending_changes(self.music_root), {"music": [], "deleted": []})

        (self.music_root / "Artist" / "A.mp3").unlink()
        music_store.sync_music_cache(self.music_root)
        deleted = music_store.read_pending_changes(self.music_root)
        self.assertEqual(len(deleted["deleted"]), 1)
        self.assertEqual(deleted["deleted"][0]["file_path"], "Artist/A.mp3")

    def test_thumbprint_changes_with_content_and_is_stable(self):
        self._write("Artist/A.mp3", b"one")
        music_store.sync_music_cache(self.music_root)
        tp1 = music_store.stored_thumbprint(self.music_root)
        music_store.sync_music_cache(self.music_root)
        self.assertEqual(tp1, music_store.stored_thumbprint(self.music_root))
        self._write("Artist/A.mp3", b"one-changed-and-longer")
        music_store.sync_music_cache(self.music_root)
        self.assertNotEqual(tp1, music_store.stored_thumbprint(self.music_root))

    def test_list_music_returns_upload_ready_payload(self):
        self._write("Artist/A.mp3")
        self._write("Artist/B.mp3")
        music_store.sync_music_cache(self.music_root)
        items = music_store.list_music(self.music_root)
        self.assertEqual(len(items), 2)
        self.assertNotIn("system", items[0])
        self.assertIn("music_fingerprint", items[0])

    def test_list_music_page_filters_by_query_and_paginates(self):
        self._write("Artist/Alpha.mp3")
        self._write("Artist/Beta.mp3")
        self._write("Artist/Gamma.mp3")
        music_store.sync_music_cache(self.music_root)

        page = music_store.list_music_page(self.music_root, limit=2, offset=0)
        self.assertEqual(page["total"], 3)
        self.assertEqual(len(page["items"]), 2)

        filtered = music_store.list_music_page(self.music_root, query="beta")
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["items"][0]["track_name"], "Beta.mp3")

    def test_list_music_page_includes_entry_key(self):
        self._write("Artist/Alpha.mp3")
        music_store.sync_music_cache(self.music_root)
        page = music_store.list_music_page(self.music_root)
        self.assertTrue(page["items"][0]["entry_key"])

    def test_get_music_by_key_returns_matching_row(self):
        self._write("Artist/Alpha.mp3", b"alpha-bytes")
        music_store.sync_music_cache(self.music_root)
        listed = music_store.list_music(self.music_root)[0]
        row = music_store.get_music_by_key(self.music_root, listed["entry_key"])
        self.assertIsNotNone(row)
        self.assertEqual(row["file_path"], "Artist/Alpha.mp3")
        self.assertEqual(row["entry_key"], listed["entry_key"])

    def test_get_music_by_key_returns_none_for_unknown_key(self):
        self._write("Artist/Alpha.mp3")
        music_store.sync_music_cache(self.music_root)
        self.assertIsNone(music_store.get_music_by_key(self.music_root, "not-a-real-key"))

    def test_delete_music_file_removes_file_and_cache_entry(self):
        path = self._write("Artist/Alpha.mp3", b"alpha-bytes")
        music_store.sync_music_cache(self.music_root)
        listed = music_store.list_music(self.music_root)[0]
        entry_key = listed["entry_key"]

        result = music_store.delete_music_file(self.music_root, entry_key)

        self.assertEqual(result, {"deleted": True, "file_path": "Artist/Alpha.mp3"})
        self.assertFalse(path.exists())
        self.assertIsNone(music_store.get_music_by_key(self.music_root, entry_key))
        self.assertEqual(music_store.list_music(self.music_root), [])

    def test_delete_music_file_is_a_no_op_for_an_unknown_key(self):
        self._write("Artist/Alpha.mp3")
        music_store.sync_music_cache(self.music_root)
        result = music_store.delete_music_file(self.music_root, "not-a-real-key")
        self.assertEqual(result, {"deleted": False})
        self.assertEqual(len(music_store.list_music(self.music_root)), 1)

    def test_delete_music_file_rejects_a_path_traversal_file_path(self):
        with music_store._open(self.music_root) as connection:
            connection.execute(
                "INSERT INTO music_cache_entries (entry_key, file_path, track_name, absolute_path, file_size, modified_time, fingerprint) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("evil", "../outside.mp3", "outside.mp3", "", 1, 1, "fp"),
            )
            connection.commit()
        result = music_store.delete_music_file(self.music_root, "evil")
        self.assertEqual(result, {"deleted": False})


class MusicMetadataStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.userdata = Path(self._tmp.name)
        self.music_root = self.userdata / "music"
        self.music_root.mkdir(parents=True)
        self._db_env = os.environ.get("DRONE_STATE_DATABASE_FILE")
        os.environ["DRONE_STATE_DATABASE_FILE"] = str(self.userdata / "system" / "drone-app" / "cache.sqlite3")

    def tearDown(self):
        if self._db_env is None:
            os.environ.pop("DRONE_STATE_DATABASE_FILE", None)
        else:
            os.environ["DRONE_STATE_DATABASE_FILE"] = self._db_env
        self._tmp.cleanup()

    def test_get_music_metadata_returns_none_when_never_scraped(self):
        self.assertIsNone(music_store.get_music_metadata(self.music_root, "deadbeef"))

    def test_save_and_get_round_trips_all_fields(self):
        saved = music_store.save_music_metadata(
            self.music_root,
            "deadbeef",
            provider="musicbrainz",
            provider_id="recording-mbid-1",
            title="Sample Track",
            art_relative_path="Band/Album/images/album-mb-art.jpg",
            artist_art_relative_path=None,
            extra={
                "artist": "Sample Band",
                "album": "Sample Album",
                "genres": ["Rock", "Indie"],
                "release_date": "2001-05-01",
                "track_number": 3,
                "disc_number": 1,
            },
        )
        self.assertEqual(saved["title"], "Sample Track")
        self.assertEqual(saved["provider"], "musicbrainz")
        self.assertEqual(saved["provider_id"], "recording-mbid-1")
        self.assertEqual(saved["genres"], ["Rock", "Indie"])
        self.assertEqual(saved["track_number"], 3)
        self.assertTrue(saved["scraped_at"])

        fetched = music_store.get_music_metadata(self.music_root, "deadbeef")
        self.assertEqual(fetched, saved)

    def test_save_upserts_on_re_scrape(self):
        music_store.save_music_metadata(
            self.music_root, "deadbeef", provider="musicbrainz", provider_id="1", title="Wrong Track",
            art_relative_path=None, artist_art_relative_path=None, extra={},
        )
        music_store.save_music_metadata(
            self.music_root, "deadbeef", provider="musicbrainz", provider_id="2", title="Right Track",
            art_relative_path=None, artist_art_relative_path=None, extra={},
        )
        fetched = music_store.get_music_metadata(self.music_root, "deadbeef")
        self.assertEqual(fetched["title"], "Right Track")

    def test_delete_music_metadata_is_idempotent(self):
        self.assertIsNone(music_store.delete_music_metadata(self.music_root, "deadbeef"))
        music_store.save_music_metadata(
            self.music_root, "deadbeef", provider="musicbrainz", provider_id="1", title="Track",
            art_relative_path=None, artist_art_relative_path=None, extra={},
        )
        deleted = music_store.delete_music_metadata(self.music_root, "deadbeef")
        self.assertEqual(deleted["title"], "Track")
        self.assertIsNone(music_store.get_music_metadata(self.music_root, "deadbeef"))
        self.assertIsNone(music_store.delete_music_metadata(self.music_root, "deadbeef"))

    def test_list_music_genres_reads_from_extra_json(self):
        music_store.save_music_metadata(
            self.music_root, "key1", provider="musicbrainz", provider_id="1", title="Track 1",
            art_relative_path=None, artist_art_relative_path=None, extra={"genres": ["Jazz"]},
        )
        music_store.save_music_metadata(
            self.music_root, "key2", provider="musicbrainz", provider_id="2", title="Track 2",
            art_relative_path=None, artist_art_relative_path=None, extra={},
        )
        genres = music_store.list_music_genres(self.music_root)
        self.assertEqual(genres, {"key1": ["Jazz"]})

    def test_list_music_display_titles_only_includes_scraped_tracks(self):
        music_store.save_music_metadata(
            self.music_root, "key1", provider="musicbrainz", provider_id="1", title="Scraped Title",
            art_relative_path=None, artist_art_relative_path=None, extra={},
        )
        titles = music_store.list_music_display_titles(self.music_root)
        self.assertEqual(titles, {"key1": "Scraped Title"})


class FindLocalCoverImageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.userdata = Path(self._tmp.name)
        self.music_root = self.userdata / "music"
        self.music_root.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel, data=b"img-bytes"):
        path = self.music_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_no_image_present_returns_none(self):
        track = self._write("Band/Album/Track.mp3")
        self.assertIsNone(music_store.find_local_cover_image(track, self.music_root))

    def test_finds_cover_jpg_beside_the_track(self):
        track = self._write("Band/Album/Track.mp3")
        cover = self._write("Band/Album/cover.jpg")
        self.assertEqual(music_store.find_local_cover_image(track, self.music_root), cover)

    def test_matches_case_insensitively(self):
        track = self._write("Band/Album/Track.mp3")
        cover = self._write("Band/Album/Cover.JPG")
        self.assertEqual(music_store.find_local_cover_image(track, self.music_root), cover)

    def test_recognizes_every_conventional_basename(self):
        for basename in ("cover", "folder", "album", "front", "art"):
            with self.subTest(basename=basename):
                sub_root = self.music_root / basename
                track_dir = sub_root / "Band" / "Album"
                track_dir.mkdir(parents=True)
                track = track_dir / "Track.mp3"
                track.write_bytes(b"x")
                image = track_dir / f"{basename}.png"
                image.write_bytes(b"img")
                self.assertEqual(music_store.find_local_cover_image(track, sub_root), image)

    def test_ignores_an_unrecognized_image_filename(self):
        # A folder can contain unrelated images (liner notes scan, a random
        # screenshot) -- only the conventional basenames count as a match.
        track = self._write("Band/Album/Track.mp3")
        self._write("Band/Album/random-photo.jpg")
        self.assertIsNone(music_store.find_local_cover_image(track, self.music_root))

    def test_falls_back_to_the_artist_folder_one_level_up(self):
        track = self._write("Band/Album/Track.mp3")
        cover = self._write("Band/cover.jpg")
        self.assertEqual(music_store.find_local_cover_image(track, self.music_root), cover)

    def test_prefers_the_track_s_own_folder_over_the_parent(self):
        track = self._write("Band/Album/Track.mp3")
        own_cover = self._write("Band/Album/cover.jpg")
        self._write("Band/cover.jpg")
        self.assertEqual(music_store.find_local_cover_image(track, self.music_root), own_cover)

    def test_does_not_search_above_the_artist_folder_for_a_singles_track(self):
        # Artist/Song.mp3 (no album folder) -- the track's own folder IS the
        # artist folder AND music_root's direct child, so there's no
        # "one level up" to search that wouldn't just be music_root itself.
        track = self._write("Solo Artist/Song.mp3")
        self._write("cover.jpg")  # sitting directly at music_root
        self.assertIsNone(music_store.find_local_cover_image(track, self.music_root))

    def test_non_matching_suffix_is_ignored(self):
        track = self._write("Band/Album/Track.mp3")
        self._write("Band/Album/cover.gif")
        self.assertIsNone(music_store.find_local_cover_image(track, self.music_root))


class MusicLikesStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.userdata = Path(self._tmp.name)
        self.music_root = self.userdata / "music"
        self.music_root.mkdir(parents=True)
        self._db_env = os.environ.get("DRONE_STATE_DATABASE_FILE")
        os.environ["DRONE_STATE_DATABASE_FILE"] = str(self.userdata / "system" / "drone-app" / "cache.sqlite3")

    def tearDown(self):
        if self._db_env is None:
            os.environ.pop("DRONE_STATE_DATABASE_FILE", None)
        else:
            os.environ["DRONE_STATE_DATABASE_FILE"] = self._db_env
        self._tmp.cleanup()

    def test_a_track_is_unliked_by_default(self):
        self.assertFalse(music_store.is_music_liked(self.music_root, "deadbeef"))
        self.assertEqual(music_store.list_liked_music_keys(self.music_root), set())

    def test_liking_and_unliking_round_trips(self):
        music_store.set_music_liked(self.music_root, "deadbeef", True)
        self.assertTrue(music_store.is_music_liked(self.music_root, "deadbeef"))
        self.assertEqual(music_store.list_liked_music_keys(self.music_root), {"deadbeef"})

        music_store.set_music_liked(self.music_root, "deadbeef", False)
        self.assertFalse(music_store.is_music_liked(self.music_root, "deadbeef"))
        self.assertEqual(music_store.list_liked_music_keys(self.music_root), set())

    def test_liking_an_already_liked_track_is_a_no_op(self):
        music_store.set_music_liked(self.music_root, "deadbeef", True)
        music_store.set_music_liked(self.music_root, "deadbeef", True)
        self.assertEqual(music_store.list_liked_music_keys(self.music_root), {"deadbeef"})

    def test_unliking_a_never_liked_track_is_a_no_op(self):
        music_store.set_music_liked(self.music_root, "deadbeef", False)
        self.assertFalse(music_store.is_music_liked(self.music_root, "deadbeef"))

    def test_list_liked_music_keys_only_includes_liked_tracks(self):
        music_store.set_music_liked(self.music_root, "key1", True)
        music_store.set_music_liked(self.music_root, "key2", False)
        self.assertEqual(music_store.list_liked_music_keys(self.music_root), {"key1"})

    def test_liking_is_independent_of_scraped_metadata(self):
        # A track can be liked whether or not it's ever been scraped -- the
        # two tables are unrelated.
        music_store.set_music_liked(self.music_root, "deadbeef", True)
        self.assertIsNone(music_store.get_music_metadata(self.music_root, "deadbeef"))
        self.assertTrue(music_store.is_music_liked(self.music_root, "deadbeef"))


if __name__ == "__main__":
    unittest.main()
