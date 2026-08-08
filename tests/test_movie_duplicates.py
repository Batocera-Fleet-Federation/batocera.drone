import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.storage.movies_store as movies_store
from app.common.settings import Settings
from app.movies import movie_duplicates


def _build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "MOVIES_ROOT": str(root / "movies"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "movie-duplicates-test",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


def _write_movie(root: Path, rel: str, data: bytes = b"x") -> Path:
    path = root / "movies" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class NormalizationAndRankingTests(unittest.TestCase):
    """normalize_movie_title()/movie_quality_rank() -- the pure-function
    core, tested directly against real scene-release-style names."""

    def test_strips_year_and_everything_after_it(self) -> None:
        self.assertEqual(
            movie_duplicates.normalize_movie_title("Inception.2010.1080p.BluRay.x264-GROUP.mkv"),
            "inception",
        )
        self.assertEqual(movie_duplicates.normalize_movie_title("Inception (2010) [720p].mkv"), "inception")

    def test_distinct_titles_stay_distinct(self) -> None:
        self.assertNotEqual(
            movie_duplicates.normalize_movie_title("Inception.2010.1080p.mkv"),
            movie_duplicates.normalize_movie_title("Interstellar.2014.1080p.mkv"),
        )

    def test_movie_year_extracts_the_release_year(self) -> None:
        self.assertEqual(movie_duplicates.movie_year("Inception.2010.1080p.BluRay.x264-GROUP.mkv"), "2010")
        self.assertIsNone(movie_duplicates.movie_year("Ant-Man (1080p).mkv"))

    def test_higher_resolution_outranks_lower(self) -> None:
        self.assertGreater(
            movie_duplicates.movie_quality_rank("Movie.2020.1080p.mkv", 1000),
            movie_duplicates.movie_quality_rank("Movie.2020.720p.mkv", 1000),
        )

    def test_bluray_source_outranks_webrip(self) -> None:
        self.assertGreater(
            movie_duplicates.movie_quality_rank("Movie.2020.1080p.BluRay.mkv", 1000),
            movie_duplicates.movie_quality_rank("Movie.2020.1080p.WEBRip.mkv", 1000),
        )

    def test_resolution_outranks_source_when_they_disagree(self) -> None:
        # A higher resolution WEBRip should still beat a lower resolution
        # BluRay -- resolution is checked first.
        self.assertGreater(
            movie_duplicates.movie_quality_rank("Movie.2020.1080p.WEBRip.mkv", 1000),
            movie_duplicates.movie_quality_rank("Movie.2020.720p.BluRay.mkv", 1000),
        )

    def test_file_size_breaks_a_tie_when_resolution_and_source_match(self) -> None:
        self.assertGreater(
            movie_duplicates.movie_quality_rank("Movie.2020.1080p.BluRay.mkv", 5000),
            movie_duplicates.movie_quality_rank("Movie.2020.1080p.BluRay.mkv", 1000),
        )

    def test_a_tagged_poor_source_still_outranks_no_tag_at_all(self) -> None:
        self.assertGreater(
            movie_duplicates.movie_quality_rank("Movie.2020.CAMRip.mkv", 1000),
            movie_duplicates.movie_quality_rank("Movie.2020.mkv", 1000),
        )


class FindDuplicateMoviesIntegrationTests(unittest.TestCase):
    def _seed_and_settings(self, root: Path) -> Settings:
        settings = _build_settings(root)
        movies_store.sync_movies_cache(settings.movies_root)
        return settings

    def test_groups_different_quality_releases_of_the_same_movie(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Inception.2010.1080p.BluRay.x264-GROUP.mkv")
            _write_movie(root, "Inception (2010) [720p].mkv")
            _write_movie(root, "Interstellar.2014.1080p.BluRay.mkv")  # no duplicate -- singleton
            settings = self._seed_and_settings(root)

            groups = movie_duplicates.find_duplicate_movies(settings.movies_root)

            movie_groups = [g for g in groups if g["kind"] == "movie"]
            self.assertEqual(len(movie_groups), 1)
            names = {item["movie_name"] for item in movie_groups[0]["items"]}
            self.assertEqual(names, {"Inception.2010.1080p.BluRay.x264-GROUP.mkv", "Inception (2010) [720p].mkv"})

    def test_does_not_group_different_movies_that_share_a_title_but_not_a_year(self) -> None:
        # The classic "a dozen different Halloween movies" case -- same
        # title, different years, must never be treated as duplicates.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Halloween.1978.1080p.mkv")
            _write_movie(root, "Halloween.2018.1080p.mkv")
            settings = self._seed_and_settings(root)

            groups = movie_duplicates.find_duplicate_movies(settings.movies_root)

            self.assertEqual(groups, [])

    def test_recommended_keep_is_the_highest_quality_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Inception.2010.720p.WEBRip.mkv")
            _write_movie(root, "Inception.2010.1080p.BluRay.mkv")
            settings = self._seed_and_settings(root)

            groups = movie_duplicates.find_duplicate_movies(settings.movies_root)
            group = next(g for g in groups if g["kind"] == "movie")

            kept = [item for item in group["items"] if item["recommended_keep"]]
            self.assertEqual(len(kept), 1)
            self.assertEqual(kept[0]["movie_name"], "Inception.2010.1080p.BluRay.mkv")

    def test_groups_the_same_episode_across_releases_by_show_season_episode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Shows/Dexter/Dexter - S01E01 - Pilot (1080p).mkv")
            _write_movie(root, "Shows/Dexter/Dexter.S01E01.720p.HDTV.mkv")
            _write_movie(root, "Shows/Dexter/Dexter - S01E02 - Crocodile.mkv")  # singleton -- different episode
            settings = self._seed_and_settings(root)

            groups = movie_duplicates.find_duplicate_movies(settings.movies_root)

            episode_groups = [g for g in groups if g["kind"] == "episode"]
            self.assertEqual(len(episode_groups), 1)
            self.assertEqual(len(episode_groups[0]["items"]), 2)

    def test_ampersand_and_the_word_and_group_the_same_show_together(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Shows/Law & Order SVU/Law & Order SVU - S01E01 - Pilot.mkv")
            _write_movie(root, "Shows/Law and Order SVU/Law and Order SVU - S01E01 - Pilot.mkv")
            settings = self._seed_and_settings(root)

            groups = movie_duplicates.find_duplicate_movies(settings.movies_root)

            episode_groups = [g for g in groups if g["kind"] == "episode"]
            self.assertEqual(len(episode_groups), 1)
            self.assertEqual(len(episode_groups[0]["items"]), 2)

    def test_extras_are_excluded_from_duplicate_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Shows/Lost (2004)/Lost (2004) S01/Featurettes/Behind the Scenes.mkv")
            _write_movie(root, "Shows/Lost (2004)/Lost (2004) S01/Featurettes/Behind the Scenes (2).mkv")
            settings = self._seed_and_settings(root)

            groups = movie_duplicates.find_duplicate_movies(settings.movies_root)

            self.assertEqual(groups, [])

    def test_kind_filter_narrows_to_movies_or_episodes_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Inception.2010.720p.mkv")
            _write_movie(root, "Inception.2010.1080p.mkv")
            _write_movie(root, "Shows/Dexter/Dexter - S01E01 - Pilot.mkv")
            _write_movie(root, "Shows/Dexter/Dexter.S01E01.720p.mkv")
            settings = self._seed_and_settings(root)

            movie_groups = movie_duplicates.find_duplicate_movies(settings.movies_root, kind_filter="movie")
            self.assertTrue(all(g["kind"] == "movie" for g in movie_groups))
            self.assertEqual(len(movie_groups), 1)

            episode_groups = movie_duplicates.find_duplicate_movies(settings.movies_root, kind_filter="episode")
            self.assertTrue(all(g["kind"] == "episode" for g in episode_groups))
            self.assertEqual(len(episode_groups), 1)

    def test_query_filter_matches_movie_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Inception.2010.720p.mkv")
            _write_movie(root, "Inception.2010.1080p.mkv")
            _write_movie(root, "Interstellar.2014.720p.mkv")
            _write_movie(root, "Interstellar.2014.1080p.mkv")
            settings = self._seed_and_settings(root)

            groups = movie_duplicates.find_duplicate_movies(settings.movies_root, query="inception")

            self.assertEqual(len(groups), 1)
            self.assertTrue(all("inception" in item["movie_name"].lower() for item in groups[0]["items"]))

    def test_handler_wraps_find_duplicate_movies(self) -> None:
        from app.drone_api import RomRequestHandler
        from app.web import handlers_movies

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_movie(root, "Inception.2010.720p.mkv")
            _write_movie(root, "Inception.2010.1080p.mkv")
            settings = self._seed_and_settings(root)

            class Handler(handlers_movies.HandlersMoviesMixin):
                pass

            handler = Handler.__new__(Handler)
            handler.settings = settings
            captured = {}
            handler._send_json = lambda status, payload, **kwargs: captured.update({"status": status, "payload": payload})

            handler._handle_admin_movie_duplicates(kind="movie")

            self.assertEqual(captured["status"], 200)
            self.assertEqual(len(captured["payload"]["groups"]), 1)


if __name__ == "__main__":
    unittest.main()
