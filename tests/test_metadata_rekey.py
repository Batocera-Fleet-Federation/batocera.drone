"""Orphan metadata re-keying and scraper-sidecar recovery (issue #114).

Covers the pure planner in ``storage/metadata_rekey.py`` and the movie/music
sync integration: legacy root-level layout, a renamed show folder, sibling
stem collisions for sidecar art, and an ambiguous fingerprint match left
unmigrated. Handler-level behavior lives in test_movies_handlers.py and
test_music_handlers.py; the end-to-end reorganization path is in
test_issue_114_library_reorg_uat.py.
"""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import app.storage.movies_store as movies_store
import app.storage.music_store as music_store
from app.storage.metadata_rekey import OrphanClaim, RekeyPlan, plan_rekeys, rekey_orphan_metadata


def _claim(fingerprint, size=10, provider="tmdb", provider_id="1", title="Title", scraped_at="2026-01-01T00:00:00"):
    return OrphanClaim(fingerprint, size, provider, provider_id, title, scraped_at)


class PlanRekeysTest(unittest.TestCase):
    def test_unique_identity_match_moves_the_orphan(self):
        plan = plan_rekeys({"old": _claim("fp1")}, [("new", "fp1", 10)], occupied=set())
        self.assertEqual(plan.moves, {"old": "new"})
        self.assertEqual(plan.superseded, [])

    def test_multiple_live_candidates_are_ambiguous_and_left_alone(self):
        live = [("copy-a", "fp1", 10), ("copy-b", "fp1", 10)]
        plan = plan_rekeys({"old": _claim("fp1")}, live, occupied=set())
        self.assertEqual(plan, RekeyPlan(moves={}, superseded=[]))

    def test_no_candidate_leaves_the_orphan_alone(self):
        self.assertEqual(plan_rekeys({"old": _claim("fp1")}, [("new", "fp2", 10)], occupied=set()).moves, {})

    def test_size_must_match_as_well_as_fingerprint(self):
        self.assertEqual(plan_rekeys({"old": _claim("fp1")}, [("new", "fp1", 11)], occupied=set()).moves, {})

    def test_target_that_already_has_a_real_row_is_not_overwritten(self):
        plan = plan_rekeys({"old": _claim("fp1")}, [("new", "fp1", 10)], occupied={"new"})
        self.assertEqual(plan, RekeyPlan(moves={}, superseded=[]))

    def test_collision_moves_one_winner_and_supersedes_the_rest(self):
        orphans = {
            "old-tmdb": _claim("fp1", provider="tmdb", scraped_at="2026-03-01T00:00:00"),
            "old-tv": _claim("fp1", provider="tmdb_tv", scraped_at="2026-01-01T00:00:00"),
        }
        plan = plan_rekeys(orphans, [("new", "fp1", 10)], occupied=set())
        # tmdb_tv wins even though the tmdb row is more recent.
        self.assertEqual(plan.moves, {"old-tv": "new"})
        self.assertEqual(plan.superseded, ["old-tmdb"])

    def test_collision_falls_back_to_most_recent_scrape(self):
        orphans = {
            "older": _claim("fp1", provider="tmdb", scraped_at="2025-01-01T00:00:00"),
            "newer": _claim("fp1", provider="tmdb", scraped_at="2026-01-01T00:00:00"),
        }
        plan = plan_rekeys(orphans, [("new", "fp1", 10)], occupied=set())
        self.assertEqual(plan.moves, {"newer": "new"})
        self.assertEqual(plan.superseded, ["older"])

    def test_collision_never_shares_the_target(self):
        orphans = {f"old-{i}": _claim("fp1", scraped_at=f"2026-01-0{i}T00:00:00") for i in range(3)}
        plan = plan_rekeys(orphans, [("new", "fp1", 10)], occupied=set())
        self.assertEqual(list(plan.moves.values()), ["new"])
        self.assertEqual(len(plan.superseded), 2)

    def test_real_orphan_beats_a_placeholder_orphan_on_collision(self):
        orphans = {
            "placeholder": _claim("fp1", provider="local", provider_id="", title="", scraped_at="2026-06-01T00:00:00"),
            "real": _claim("fp1", provider="tmdb", scraped_at="2025-01-01T00:00:00"),
        }
        plan = plan_rekeys(orphans, [("new", "fp1", 10)], occupied=set())
        self.assertEqual(plan.moves, {"real": "new"})
        self.assertEqual(plan.superseded, ["placeholder"])

    def test_orphan_without_a_fingerprint_is_never_matched(self):
        self.assertEqual(plan_rekeys({"old": _claim("")}, [("new", "", 10)], occupied=set()).moves, {})


class RekeyTableTest(unittest.TestCase):
    """Runs ``rekey_orphan_metadata`` against minimal tables to cover the
    placeholder-target and superseded-duplicate cases directly."""

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript(
            """
            CREATE TABLE live (entry_key TEXT PRIMARY KEY, fingerprint TEXT, file_size INTEGER);
            CREATE TABLE deleted (entry_key TEXT PRIMARY KEY, fingerprint TEXT, file_size INTEGER);
            CREATE TABLE meta (entry_key TEXT PRIMARY KEY, provider TEXT NOT NULL, provider_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '', scraped_at TEXT NOT NULL, extra_json TEXT NOT NULL DEFAULT '{}');
            """
        )

    def tearDown(self):
        self.connection.close()

    def _rekey(self):
        return rekey_orphan_metadata(
            self.connection, cache_table="live", deleted_table="deleted", metadata_table="meta"
        )

    def _meta(self, key, provider, provider_id="1", title="T", scraped_at="2026-01-01T00:00:00"):
        self.connection.execute("INSERT INTO meta (entry_key, provider, provider_id, title, scraped_at) VALUES (?, ?, ?, ?, ?)",
                                (key, provider, provider_id, title, scraped_at))

    def _archive(self, key, fingerprint="fp1", size=10):
        self.connection.execute("INSERT INTO deleted VALUES (?, ?, ?)", (key, fingerprint, size))

    def _live(self, key, fingerprint="fp1", size=10):
        self.connection.execute("INSERT INTO live VALUES (?, ?, ?)", (key, fingerprint, size))

    def _meta_keys(self):
        return {row[0]: (row[1], row[2]) for row in self.connection.execute("SELECT entry_key, provider, title FROM meta")}

    def test_placeholder_on_the_target_is_replaced_by_the_real_orphan(self):
        self._archive("old")
        self._meta("old", "tmdb", title="Real Title")
        self._live("new")
        self._meta("new", "local", provider_id="", title="")

        result = self._rekey()

        self.assertEqual(result, {"orphaned": 1, "rekeyed": 1, "superseded": 0, "unmatched": 0})
        self.assertEqual(self._meta_keys(), {"new": ("tmdb", "Real Title")})

    def test_real_row_on_the_target_is_never_replaced(self):
        self._archive("old")
        self._meta("old", "tmdb", title="Orphan Title")
        self._live("new")
        self._meta("new", "tmdb", title="Live Title")

        result = self._rekey()

        self.assertEqual(result, {"orphaned": 1, "rekeyed": 0, "superseded": 0, "unmatched": 1})
        self.assertEqual(self._meta_keys(), {"old": ("tmdb", "Orphan Title"), "new": ("tmdb", "Live Title")})

    def test_local_row_with_a_title_counts_as_real(self):
        self._archive("old")
        self._meta("old", "tmdb")
        self._live("new")
        self._meta("new", "local", provider_id="", title="Has Title")

        self.assertEqual(self._rekey()["rekeyed"], 0)

    def test_duplicate_tmdb_rows_collapse_to_one_moved_row(self):
        self._archive("old-tmdb")
        self._archive("old-tv")
        self._meta("old-tmdb", "tmdb", scraped_at="2026-03-01T00:00:00")
        self._meta("old-tv", "tmdb_tv", provider_id="1-s1e1", scraped_at="2026-01-01T00:00:00")
        self._live("new")

        result = self._rekey()

        self.assertEqual(result, {"orphaned": 2, "rekeyed": 1, "superseded": 1, "unmatched": 0})
        self.assertEqual(self._meta_keys(), {"new": ("tmdb_tv", "T")})

    def test_rekey_is_idempotent_after_superseding(self):
        self._archive("old-a")
        self._archive("old-b")
        self._meta("old-a", "tmdb", scraped_at="2026-02-01T00:00:00")
        self._meta("old-b", "tmdb", scraped_at="2026-01-01T00:00:00")
        self._live("new")
        self._rekey()

        second = self._rekey()

        self.assertEqual(second, {"orphaned": 0, "rekeyed": 0, "superseded": 0, "unmatched": 0})
        self.assertEqual(self._meta_keys(), {"new": ("tmdb", "T")})


class _TempLibraryMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.userdata = Path(self._tmp.name)
        self.movies_root = self.userdata / "movies"
        self.movies_root.mkdir(parents=True)
        self.shows_root = self.userdata / "shows"
        self.shows_root.mkdir(parents=True)
        self._db_env = os.environ.get("DRONE_STATE_DATABASE_FILE")
        os.environ["DRONE_STATE_DATABASE_FILE"] = str(self.userdata / "system" / "drone-app" / "cache.sqlite3")

    def tearDown(self):
        if self._db_env is None:
            os.environ.pop("DRONE_STATE_DATABASE_FILE", None)
        else:
            os.environ["DRONE_STATE_DATABASE_FILE"] = self._db_env
        self._tmp.cleanup()

    def _write(self, root: Path, rel: str, data: bytes = b"movie-data") -> Path:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


class MovieRekeyIntegrationTest(_TempLibraryMixin, unittest.TestCase):
    def _scrape(self, rel: str, title: str = "Scraped") -> str:
        key = movies_store.list_movies(self.movies_root)
        entry_key = next(item["entry_key"] for item in key if item["file_path"] == rel)
        movies_store.save_movie_metadata(
            self.movies_root, entry_key, provider="tmdb", provider_id="1", title=title,
            poster_relative_path=None, backdrop_relative_path=None, extra={},
        )
        return entry_key

    def test_legacy_root_level_layout_orphans_are_rekeyed_after_reorganizing(self):
        self._write(self.movies_root, "Vacation.mp4", b"vacation-bytes")
        movies_store.sync_movies_cache(self.movies_root)
        old_key = self._scrape("Vacation.mp4", title="Vacation")

        (self.movies_root / "comedy").mkdir()
        (self.movies_root / "Vacation.mp4").rename(self.movies_root / "comedy" / "Vacation.mp4")
        result = movies_store.sync_movies_cache(self.movies_root)

        new_key = movies_store.list_movies(self.movies_root)[0]["entry_key"]
        self.assertNotEqual(old_key, new_key)
        self.assertIsNone(movies_store.get_movie_metadata(self.movies_root, old_key))
        self.assertEqual(movies_store.get_movie_metadata(self.movies_root, new_key)["title"], "Vacation")
        self.assertEqual(result["metadata_orphaned"], 1)
        self.assertEqual(result["metadata_rekeyed"], 1)
        self.assertEqual(result["metadata_unmatched"], 0)

    def test_renamed_show_folder_rekeys_its_episode_metadata(self):
        episode = self._write(self.shows_root, "Firefly (2002)/Season 01/Firefly - S01E01.mkv", b"episode")
        movies_store.sync_movies_cache(self.movies_root, self.shows_root)
        old_key = movies_store.list_movies(self.movies_root)[0]["entry_key"]
        movies_store.save_movie_metadata(
            self.movies_root, old_key, provider="tmdb_tv", provider_id="1-s1e1", title="Serenity",
            poster_relative_path=None, backdrop_relative_path=None, extra={},
        )

        (self.shows_root / "Firefly (2002)").rename(self.shows_root / "Firefly")
        self.assertFalse(episode.exists())
        result = movies_store.sync_movies_cache(self.movies_root, self.shows_root)

        new_key = movies_store.list_movies(self.movies_root)[0]["entry_key"]
        self.assertEqual(movies_store.list_movies(self.movies_root)[0]["file_path"], "Shows/Firefly/Season 01/Firefly - S01E01.mkv")
        self.assertEqual(movies_store.get_movie_metadata(self.movies_root, new_key)["title"], "Serenity")
        self.assertEqual(result["metadata_rekeyed"], 1)

    def test_ambiguous_fingerprint_match_is_left_unmigrated(self):
        self._write(self.movies_root, "Movie.mp4", b"same-bytes")
        movies_store.sync_movies_cache(self.movies_root)
        old_key = self._scrape("Movie.mp4", title="Movie")

        # Original moved away, and two identical copies now exist under new paths.
        (self.movies_root / "Movie.mp4").unlink()
        self._write(self.movies_root, "a/Movie.mp4", b"same-bytes")
        self._write(self.movies_root, "b/Movie.mp4", b"same-bytes")
        result = movies_store.sync_movies_cache(self.movies_root)

        # The orphan row stays put for review rather than attaching to a copy.
        self.assertIsNotNone(movies_store.get_movie_metadata(self.movies_root, old_key))
        self.assertEqual(result["metadata_orphaned"], 1)
        self.assertEqual(result["metadata_rekeyed"], 0)
        self.assertEqual(result["metadata_unmatched"], 1)
        for row in movies_store.list_movies(self.movies_root):
            self.assertIsNone(movies_store.get_movie_metadata(self.movies_root, row["entry_key"]))

    def test_rekey_is_idempotent_across_repeated_syncs(self):
        self._write(self.movies_root, "Vacation.mp4", b"vacation-bytes")
        movies_store.sync_movies_cache(self.movies_root)
        self._scrape("Vacation.mp4")
        self._write(self.movies_root, "new/Vacation.mp4", b"vacation-bytes")
        (self.movies_root / "Vacation.mp4").unlink()

        first = movies_store.sync_movies_cache(self.movies_root)
        second = movies_store.sync_movies_cache(self.movies_root)

        self.assertEqual(first["metadata_rekeyed"], 1)
        self.assertEqual(second["metadata_orphaned"], 0)
        self.assertEqual(second["metadata_rekeyed"], 0)


class MovieSidecarArtworkTest(_TempLibraryMixin, unittest.TestCase):
    def _poster_for(self, rel: str):
        movies_store.sync_movies_cache(self.movies_root, self.shows_root)
        entry_key = next(item["entry_key"] for item in movies_store.list_movies(self.movies_root) if item["file_path"] == rel)
        metadata = movies_store.get_movie_metadata(self.movies_root, entry_key)
        return (metadata or {}).get("poster_relative_path")

    def test_exact_stem_sidecar_is_recovered_as_poster(self):
        self._write(self.movies_root, "horror/The Shining.mp4")
        self._write(self.movies_root, "horror/images/The Shining-tmdb-poster.jpg", b"poster")
        self.assertEqual(self._poster_for("horror/The Shining.mp4"), "horror/images/The Shining-tmdb-poster.jpg")

    def test_normalized_stem_sidecar_is_recovered_when_unambiguous(self):
        self._write(self.movies_root, "Show.S01E02.mkv")
        self._write(self.movies_root, "images/Show S01E02-tmdb-poster.jpg", b"poster")
        self.assertEqual(self._poster_for("Show.S01E02.mkv"), "images/Show S01E02-tmdb-poster.jpg")

    def test_sibling_stem_collision_never_shares_a_sidecar(self):
        # Both siblings normalize to "show 01", so neither may claim the one sidecar.
        self._write(self.shows_root, "Show/Season 01/Show 01.mkv")
        self._write(self.shows_root, "Show/Season 01/Show-01.mkv")
        self._write(self.shows_root, "Show/Season 01/images/Show 01-tmdb-poster.jpg", b"poster")

        movies_store.sync_movies_cache(self.movies_root, self.shows_root)

        for row in movies_store.list_movies(self.movies_root):
            metadata = movies_store.get_movie_metadata(self.movies_root, row["entry_key"])
            self.assertIsNone(metadata)

    def test_sidecar_art_is_served_when_no_metadata_row_exists(self):
        # Art that lands after the last sync has no recovered row yet; the
        # route falls back to reading it straight off disk.
        self._write(self.movies_root, "Vacation.mp4")
        movies_store.sync_movies_cache(self.movies_root)
        self._write(self.movies_root, "images/Vacation-tmdb-poster.jpg", b"poster")
        entry_key = movies_store.list_movies(self.movies_root)[0]["entry_key"]

        self.assertIsNone(movies_store.get_movie_metadata(self.movies_root, entry_key))
        found = movies_store.find_local_artwork(self.movies_root, entry_key, self.shows_root, "poster")
        self.assertEqual(found, (self.movies_root / "images" / "Vacation-tmdb-poster.jpg").resolve())
        self.assertIsNone(movies_store.find_local_artwork(self.movies_root, entry_key, self.shows_root, "backdrop"))


class MusicRekeyIntegrationTest(unittest.TestCase):
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

    def test_orphaned_track_metadata_is_rekeyed_by_fingerprint(self):
        track = self.music_root / "Artist" / "Album" / "01 Song.mp3"
        track.parent.mkdir(parents=True)
        track.write_bytes(b"audio-bytes")
        music_store.sync_music_cache(self.music_root)
        old_key = music_store.list_music(self.music_root)[0]["entry_key"]
        music_store.save_music_metadata(
            self.music_root, old_key, provider="musicbrainz", provider_id="1", title="Song",
            art_relative_path=None, artist_art_relative_path=None, extra={},
        )

        moved = self.music_root / "Artist" / "Reissue" / "01 Song.mp3"
        moved.parent.mkdir(parents=True)
        track.rename(moved)
        result = music_store.sync_music_cache(self.music_root)

        new_key = music_store.list_music(self.music_root)[0]["entry_key"]
        self.assertIsNone(music_store.get_music_metadata(self.music_root, old_key))
        self.assertEqual(music_store.get_music_metadata(self.music_root, new_key)["title"], "Song")
        self.assertEqual(result["metadata_rekeyed"], 1)


if __name__ == "__main__":
    unittest.main()
