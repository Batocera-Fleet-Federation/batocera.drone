import unittest

from app.movies import filename_parser


class ClassifyMovieTests(unittest.TestCase):
    def test_plain_paren_quality_style(self) -> None:
        result = filename_parser.classify("Ant-Man (1080p).mkv", "Ant-Man (1080p).mkv")
        self.assertEqual(result.kind, filename_parser.KIND_MOVIE)

    def test_dot_separated_scene_release_style(self) -> None:
        result = filename_parser.classify(
            "28.Days.Later.2002.1080p.BluRay.DDP5.1.x265.10bit-GalaxyRG265.mkv",
            "28.Days.Later.2002.1080p.BluRay.DDP5.1.x265.10bit-GalaxyRG265.mkv",
        )
        self.assertEqual(result.kind, filename_parser.KIND_MOVIE)

    def test_yify_bracket_group_style(self) -> None:
        result = filename_parser.classify(
            "Alien.Covenant.2017.1080p.BluRay.x264-[YTS.AG].mp4",
            "Alien.Covenant.2017.1080p.BluRay.x264-[YTS.AG].mp4",
        )
        self.assertEqual(result.kind, filename_parser.KIND_MOVIE)


class ClassifyEpisodeTests(unittest.TestCase):
    def test_sonarr_trash_style_with_year_and_episode_title(self) -> None:
        result = filename_parser.classify(
            "Shows/Dexter/Dexter (2006) S01/Dexter (2006) - S01E04 - Let's Give the Boy a Hand (1080p BluRay x265 Silence).mkv",
            "Dexter (2006) - S01E04 - Let's Give the Boy a Hand (1080p BluRay x265 Silence).mkv",
        )
        self.assertEqual(result.kind, filename_parser.KIND_EPISODE)
        self.assertEqual(result.show_title, "Dexter")
        self.assertEqual(result.year, "2006")
        self.assertEqual(result.season, 1)
        self.assertEqual(result.episode, 4)
        self.assertEqual(result.episode_title, "Let's Give the Boy a Hand")

    def test_episode_title_containing_no_quality_suffix(self) -> None:
        result = filename_parser.classify(
            "Dexter (2006) - S01E01 - Dexter (1080p BluRay x265 Silence).mkv",
            "Dexter (2006) - S01E01 - Dexter (1080p BluRay x265 Silence).mkv",
        )
        self.assertEqual(result.season, 1)
        self.assertEqual(result.episode, 1)
        self.assertEqual(result.episode_title, "Dexter")

    def test_alt_1x01_style_no_year(self) -> None:
        result = filename_parser.classify(
            "Breaking.Bad.1x01.Pilot.mkv", "Breaking.Bad.1x01.Pilot.mkv"
        )
        self.assertEqual(result.kind, filename_parser.KIND_EPISODE)
        self.assertEqual(result.show_title, "Breaking Bad")
        self.assertEqual(result.season, 1)
        self.assertEqual(result.episode, 1)

    def test_no_zero_padding(self) -> None:
        result = filename_parser.classify("Show - S1E1 - Title.mkv", "Show - S1E1 - Title.mkv")
        self.assertEqual(result.kind, filename_parser.KIND_EPISODE)
        self.assertEqual(result.season, 1)
        self.assertEqual(result.episode, 1)


class ClassifyExtraTests(unittest.TestCase):
    def test_featurettes_folder_is_an_extra_even_without_episode_shape(self) -> None:
        result = filename_parser.classify(
            "Shows/Dexter/Dexter (2006) S01/Featurettes/Blood Splatter 101.mkv",
            "Blood Splatter 101.mkv",
        )
        self.assertEqual(result.kind, filename_parser.KIND_EXTRA)

    def test_nested_interviews_folder_is_an_extra(self) -> None:
        result = filename_parser.classify(
            "Shows/Dexter/Dexter (2006) S02/Featurettes/Interviews/C.S. Lee.mkv",
            "C.S. Lee.mkv",
        )
        self.assertEqual(result.kind, filename_parser.KIND_EXTRA)

    def test_extras_folder_check_is_case_insensitive(self) -> None:
        result = filename_parser.classify(
            "Shows/Some Show/BEHIND THE SCENES/clip.mkv", "clip.mkv"
        )
        self.assertEqual(result.kind, filename_parser.KIND_EXTRA)

    def test_deleted_scenes_folder_is_an_extra(self) -> None:
        result = filename_parser.classify(
            "Movies/Some Movie (2020)/Deleted Scenes/scene1.mkv", "scene1.mkv"
        )
        self.assertEqual(result.kind, filename_parser.KIND_EXTRA)

    def test_ordinary_folder_is_not_treated_as_an_extra(self) -> None:
        result = filename_parser.classify("Shows/Dexter/Dexter (2006) S01/x.mkv", "Blood Splatter 101.mkv")
        self.assertEqual(result.kind, filename_parser.KIND_MOVIE)


class SearchCandidatesTests(unittest.TestCase):
    def test_year_token_truncates_trailing_scene_tags_and_yields_year_filtered_candidate_first(self) -> None:
        candidates = filename_parser.search_candidates(
            "28.Days.Later.2002.1080p.BluRay.DDP5.1.x265.10bit-GalaxyRG265"
        )
        self.assertEqual(candidates[0], ("28 Days Later", "2002"))
        # Same title retried without the year filter as the very next rung.
        self.assertEqual(candidates[1], ("28 Days Later", None))

    def test_yify_bracket_group_after_year(self) -> None:
        candidates = filename_parser.search_candidates("Alien.Covenant.2017.1080p.BluRay.x264-[YTS.AG]")
        self.assertEqual(candidates[0], ("Alien Covenant", "2017"))

    def test_hyphenated_title_survives_no_year_quality_paren_style(self) -> None:
        # Regression: an earlier version of the trailing-release-group strip
        # matched any "-word" run to the end of string, which ate the "-Man"
        # off "Ant-Man" and the "-Animator" off "Re-Animator".
        candidates = filename_parser.search_candidates("Ant-Man (1080p)")
        self.assertIn(("Ant Man", None), candidates)

    def test_hyphenated_title_with_trailing_words_survives(self) -> None:
        candidates = filename_parser.search_candidates("Bride of Re-Animator (1080p)")
        self.assertIn(("Bride of Re Animator", None), candidates)

    def test_no_year_no_brackets_plain_style(self) -> None:
        candidates = filename_parser.search_candidates("Black Panther (1080p)")
        self.assertIn(("Black Panther", None), candidates)

    def test_edition_tag_in_parens_is_dropped_by_the_year_cut_when_year_present(self) -> None:
        candidates = filename_parser.search_candidates("Hellraiser (2022) (1080p)")
        self.assertEqual(candidates[0], ("Hellraiser", "2022"))

    def test_wrong_or_unhelpful_year_falls_back_to_unfiltered_search(self) -> None:
        # candidates[1] (same title, no year) exists precisely so a bulk job
        # can retry without the filter when the year-filtered search misses.
        candidates = filename_parser.search_candidates("Some.Movie.1999.720p.WEBRip.x264-GROUP")
        titles_only = [c[0] for c in candidates]
        self.assertIn("Some Movie", titles_only)

    def test_unicode_fraction_slash_is_restored_to_a_real_slash(self) -> None:
        candidates = filename_parser.search_candidates("Face⁄Off (1080p)")
        titles_only = [c[0] for c in candidates]
        self.assertTrue(any("Face" in t and "Off" in t for t in titles_only))

    def test_empty_stem_yields_no_candidates(self) -> None:
        self.assertEqual(filename_parser.search_candidates("----"), [])

    def test_language_tag_stripped_in_aggressive_fallback(self) -> None:
        candidates = filename_parser.search_candidates(
            "Mortal.Kombat.II.2026.NORDIC.1080p.BluRay.x264.AAC5.1-[YTS.GG - YTS.BZ]"
        )
        # The year-cut candidate already gives a clean title; NORDIC only
        # shows up after the year so it never pollutes the primary candidate.
        self.assertEqual(candidates[0], ("Mortal Kombat II", "2026"))


if __name__ == "__main__":
    unittest.main()
