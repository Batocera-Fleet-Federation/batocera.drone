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

    def test_bracketed_tags_with_no_separator_before_them(self) -> None:
        # Regression: found live on a real library -- "House M.D. S01E10
        # [1080p] [x265] [pseudo].mkv" has no "- " (or any separator) between
        # the episode number and its trailing quality tags. An earlier
        # version of _EPISODE_RE required that separator and simply failed
        # to match this, falling through to KIND_MOVIE -- which then got
        # searched against TMDb's *movie* endpoint and could never match.
        name = "House M.D. S01E10 [1080p] [x265] [pseudo].mkv"
        result = filename_parser.classify(name, name)
        self.assertEqual(result.kind, filename_parser.KIND_EPISODE)
        self.assertEqual(result.season, 1)
        self.assertEqual(result.episode, 10)
        self.assertEqual(result.episode_title, "")

    def test_space_separated_tags_with_no_separator_before_them(self) -> None:
        # Same real-library regression, no brackets this time -- just plain
        # space-separated tags jammed directly after the episode number.
        name = "Law and Order SVU S27E17 1080p AMZN WEB-DL DDP5 1 H 264-NTb.mkv"
        result = filename_parser.classify(name, name)
        self.assertEqual(result.kind, filename_parser.KIND_EPISODE)
        self.assertEqual(result.show_title, "Law and Order SVU")
        self.assertEqual(result.season, 27)
        self.assertEqual(result.episode, 17)

    def test_custom_parenthetical_after_episode_number_does_not_break_parsing(self) -> None:
        name = "Tales from the Crypt - S02E10 (The Ventriloquist's Dummy).mp4"
        result = filename_parser.classify(name, name)
        self.assertEqual(result.kind, filename_parser.KIND_EPISODE)
        self.assertEqual(result.show_title, "Tales from the Crypt")
        self.assertEqual(result.season, 2)
        self.assertEqual(result.episode, 10)

    def test_ep_only_style_with_no_season_defaults_to_season_one(self) -> None:
        # Real-library regression: a continuously-numbered show (no SxxEyy,
        # no NxNN, and -- unlike the extras-folder case -- no season
        # subfolder either) fell all the way through to KIND_MOVIE and was
        # searched against TMDb's movie endpoint, which can never match a
        # TV show. "CENTURIONS - Ep. 04 - Found, One Lost World (480p -
        # DVDRip).mp4" verified live against TMDb's own "The Centurions"
        # entry, which files this exact episode under season 1.
        name = "CENTURIONS - Ep. 04 - Found, One Lost World (480p - DVDRip).mp4"
        result = filename_parser.classify("Shows/The CENTURIONS/" + name, name)
        self.assertEqual(result.kind, filename_parser.KIND_EPISODE)
        self.assertEqual(result.show_title, "CENTURIONS")
        self.assertEqual(result.season, 1)
        self.assertEqual(result.episode, 4)
        self.assertEqual(result.episode_title, "Found, One Lost World")

    def test_ep_only_style_spelled_out_episode_word(self) -> None:
        name = "Some Show - Episode 12 - Title.mkv"
        result = filename_parser.classify(name, name)
        self.assertEqual(result.kind, filename_parser.KIND_EPISODE)
        self.assertEqual(result.show_title, "Some Show")
        self.assertEqual(result.season, 1)
        self.assertEqual(result.episode, 12)

    def test_ep_only_style_does_not_shadow_real_season_episode_markers(self) -> None:
        # SxxEyy must still win when it's actually present -- _EPISODE_ONLY_RE
        # is only ever tried as a fallback after both stricter patterns fail.
        name = "Dexter (2006) - S01E04 - Let's Give the Boy a Hand.mkv"
        result = filename_parser.classify(name, name)
        self.assertEqual(result.season, 1)
        self.assertEqual(result.episode, 4)


class ClassifyExtraTests(unittest.TestCase):
    def test_real_sxxeyy_marker_in_extras_folder_wins_as_a_real_episode(self) -> None:
        # Real reported gap: a genuine, unambiguous S00E01 marker in the
        # file's own name used to be discarded because the extras-folder
        # check ran first and short-circuited before the filename was even
        # looked at -- Adult Swim/Cartoon Network-style libraries commonly
        # catalog bonus/special content as a real TMDb-numbered episode with
        # its own artwork, not just generic bonus content that happens to
        # share a folder with real Featurettes.
        name = "Aqua Teen Hunger Force (2000) - S00E01 - Baffler Meal (480p DVD x265 r00t).mkv"
        result = filename_parser.classify("Shows/Aqua Teen Hunger Force/Featurettes/Vol. 2/" + name, name)
        self.assertEqual(result.kind, filename_parser.KIND_EPISODE)
        self.assertEqual(result.show_title, "Aqua Teen Hunger Force")
        self.assertEqual(result.year, "2000")
        self.assertEqual(result.season, 0)
        self.assertEqual(result.episode, 1)
        self.assertEqual(result.episode_title, "Baffler Meal")

    def test_featurettes_folder_is_an_extra_even_without_episode_shape(self) -> None:
        result = filename_parser.classify(
            "Shows/Dexter/Dexter (2006) S01/Featurettes/Blood Splatter 101.mkv",
            "Blood Splatter 101.mkv",
        )
        self.assertEqual(result.kind, filename_parser.KIND_EXTRA)
        # Directory structure still resolves a show/season for it -- an
        # extra isn't a numbered episode, but it should still be groupable
        # under the right show/season in the Movies UI rather than left as
        # an orphan card with no context.
        self.assertEqual(result.show_title, "Dexter")
        self.assertEqual(result.season, 1)
        self.assertIsNone(result.episode)

    def test_nested_interviews_folder_is_an_extra(self) -> None:
        result = filename_parser.classify(
            "Shows/Dexter/Dexter (2006) S02/Featurettes/Interviews/C.S. Lee.mkv",
            "C.S. Lee.mkv",
        )
        self.assertEqual(result.kind, filename_parser.KIND_EXTRA)
        self.assertEqual(result.show_title, "Dexter")
        self.assertEqual(result.season, 2)

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

    def test_extras_in_a_named_subfolder_under_featurettes_are_still_extras(self) -> None:
        # Regression: found live -- a numbered-clip naming style ("01. Seeing
        # Walt.mkv", no SxxEyy at all) living two levels under Featurettes,
        # in a subfolder with its own descriptive name. The extras-folder
        # check just needs *any* ancestor segment to match, not the direct
        # parent -- confirms that still holds with a named subfolder between.
        result = filename_parser.classify(
            "torrents/Lost (2004) S01-S06/Lost (2004) S02/Featurettes/Deleted Scenes/01. Seeing Walt.mkv",
            "01. Seeing Walt.mkv",
        )
        self.assertEqual(result.kind, filename_parser.KIND_EXTRA)
        self.assertEqual(result.show_title, "Lost")
        self.assertEqual(result.season, 2)

    def test_hidden_extras_folder_is_recognized(self) -> None:
        # Found live: "Hidden Extras" is a real folder name in use (5
        # instances) but wasn't in EXTRAS_FOLDER_NAMES at all -- its contents
        # fell through to plain movie/episode classification instead of
        # being recognized as bonus content.
        result = filename_parser.classify(
            "Shows/Dexter/Dexter (2006) S06/Hidden Extras/Season Recap.mkv",
            "Season Recap.mkv",
        )
        self.assertEqual(result.kind, filename_parser.KIND_EXTRA)
        self.assertEqual(result.show_title, "Dexter")
        self.assertEqual(result.season, 6)

    def test_combined_season_folder_with_quality_tags_resolves_show_and_season(self) -> None:
        # Found live: a season folder that never cleanly ends in " SNN" (see
        # _SEASON_FOLDER_WITH_SHOW_RE) or matches bare "Season NN" exactly
        # (_BARE_SEASON_FOLDER_RE) -- everything (show, a redundant spelled-
        # out "Season 1", the real "S01" token, and quality tags) is jammed
        # into one folder segment. Previously this Featurette was left an
        # ungrouped orphan card.
        result = filename_parser.classify(
            "Shows/WandaVision (2021) Season 1 S01 (1080p BluRay x265 HEVC 10bit EAC3 5.1 Silence)/"
            "Featurettes/Making Of.mkv",
            "Making Of.mkv",
        )
        self.assertEqual(result.kind, filename_parser.KIND_EXTRA)
        self.assertEqual(result.show_title, "WandaVision")
        self.assertEqual(result.season, 1)

    def test_combined_season_folder_without_year_or_spelled_out_season(self) -> None:
        # Found live: a scene-release-style season folder with no
        # parenthesized year to anchor folder_title_candidate on, and no
        # spelled-out "Season" word either -- just a bare "S01" token buried
        # in a dot-separated release name.
        result = filename_parser.classify(
            "Shows/Secret.Invasion.S01.COMPLETE.1080p.DSNP.WEB-DL.DDP5.1.H.264-NTb[TGx]/"
            "Featurettes/Behind the Invasion.mkv",
            "Behind the Invasion.mkv",
        )
        self.assertEqual(result.kind, filename_parser.KIND_EXTRA)
        self.assertEqual(result.show_title, "Secret Invasion")
        self.assertEqual(result.season, 1)

    def test_combined_season_folder_fallback_never_fires_on_an_episode_marker(self) -> None:
        # _SEASON_TOKEN_RE's bare-"SNN" alternative must not match the "S01"
        # inside an actual "S01E04"-shaped token -- that's never legitimate
        # in a folder name to begin with, but guard it explicitly since the
        # new fallback is deliberately more permissive than the two stricter
        # patterns it sits behind.
        self.assertIsNone(filename_parser._SEASON_TOKEN_RE.search("Random Folder S01E04 Something"))
        self.assertEqual(
            filename_parser._season_and_show_from_combined_folder("Random Folder S01E04 Something"),
            ("", None),
        )

    def test_ordinary_folder_is_not_treated_as_an_extra(self) -> None:
        result = filename_parser.classify("Shows/Dexter/Dexter (2006) S01/x.mkv", "Blood Splatter 101.mkv")
        self.assertEqual(result.kind, filename_parser.KIND_MOVIE)

    def test_real_reported_case_lost_on_location_deeply_nested(self) -> None:
        # The exact reported example: a Featurette two levels deeper still
        # ("Lost - On Location" between Featurettes and the file), numbered
        # rather than SxxEyy-named, with no season/show indicator anywhere
        # in its own filename at all.
        result = filename_parser.classify(
            "Shows/Lost (2004)/Lost (2004) S02/Featurettes/Lost - On Location/01. Adrift.mkv",
            "01. Adrift.mkv",
        )
        self.assertEqual(result.kind, filename_parser.KIND_EXTRA)
        self.assertEqual(result.show_title, "Lost")
        self.assertEqual(result.season, 2)

    def test_bare_season_folder_resolves_show_from_its_own_parent(self) -> None:
        # Plex/Kodi/Jellyfin's "Season NN" convention carries no show name of
        # its own -- has to come from one level further up (real example:
        # Forensic Files' anthology-style "Season 00" specials folder).
        result = filename_parser.classify(
            "Shows/Forensic Files/Season 00/Featurettes/Behind the Investigation.mkv",
            "Behind the Investigation.mkv",
        )
        self.assertEqual(result.kind, filename_parser.KIND_EXTRA)
        self.assertEqual(result.show_title, "Forensic Files")
        self.assertEqual(result.season, 0)

    def test_no_resolvable_show_or_season_leaves_extra_ungrouped(self) -> None:
        # No "<Show> SXX" or "Season NN" folder anywhere in the path -- must
        # not guess; an ungrouped extra falls back to today's behavior
        # (its own orphan card) rather than a wrong grouping.
        result = filename_parser.classify("Movies/Featurettes/RandomClip.mkv", "RandomClip.mkv")
        self.assertEqual(result.kind, filename_parser.KIND_EXTRA)
        self.assertEqual(result.show_title, "")
        self.assertIsNone(result.season)


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

    def test_edition_tag_before_the_year_is_stripped_from_the_year_cut_candidate(self) -> None:
        # Real reported failure: this release puts "Directors.Cut" *before*
        # the year (most scene releases put edition tags after it), and
        # TMDb's own title is "Alien Resurrection", not "Alien Resurrection
        # Directors Cut" -- searching "Alien Resurrection" alone found it,
        # "Alien Resurrection Director's Cut" did not. The year-cut
        # candidate used to only run a punctuation collapse, never the
        # scene-token strip, so this text leaked straight into candidate #1.
        candidates = filename_parser.search_candidates(
            "Alien.Resurrection.Directors.Cut.1997.1080p.BRrip.x264.GAZ.YIFY"
        )
        self.assertEqual(candidates[0], ("Alien Resurrection", "1997"))
        # The un-stripped version is kept as a lower-priority fallback rather
        # than dropped outright, in case the vocabulary ever over-matches.
        self.assertIn(("Alien Resurrection Directors Cut", "1997"), candidates)

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

    def test_folder_name_candidate_is_tried_before_any_filename_rung(self) -> None:
        # A well-organized parent folder rescues a filename that otherwise
        # parses to nothing useful.
        candidates = filename_parser.search_candidates("----", folder_name="Lost (2004)")
        self.assertEqual(candidates[0], ("Lost", "2004"))

    def test_folder_name_candidate_deduplicates_with_a_matching_filename_rung(self) -> None:
        candidates = filename_parser.search_candidates(
            "Alien.Resurrection.Directors.Cut.1997.1080p.BRrip.x264.GAZ.YIFY",
            folder_name="Alien Resurrection (1997)",
        )
        self.assertEqual(candidates.count(("Alien Resurrection", "1997")), 1)

    def test_folder_name_without_a_year_yields_no_extra_candidate(self) -> None:
        # "Forensic Files" (no parenthesized year) is a real case where the
        # filename's own show_title is already correct -- the folder isn't
        # needed, and its absence must not raise or alter anything.
        with_folder = filename_parser.search_candidates("Some.Movie.1999.1080p", folder_name="Forensic Files")
        without_folder = filename_parser.search_candidates("Some.Movie.1999.1080p")
        self.assertEqual(with_folder, without_folder)

    def test_no_folder_name_behaves_exactly_as_before(self) -> None:
        self.assertEqual(
            filename_parser.search_candidates("28.Days.Later.2002.1080p.BluRay.DDP5.1.x265.10bit-GalaxyRG265"),
            filename_parser.search_candidates(
                "28.Days.Later.2002.1080p.BluRay.DDP5.1.x265.10bit-GalaxyRG265", folder_name=None,
            ),
        )


class FolderTitleCandidateTests(unittest.TestCase):
    def test_title_and_year_extracted_from_bare_folder_name(self) -> None:
        self.assertEqual(filename_parser.folder_title_candidate("Lost (2004)"), ("Lost", "2004"))

    def test_trailing_quality_tags_after_the_year_are_tolerated(self) -> None:
        self.assertEqual(
            filename_parser.folder_title_candidate("Alien Resurrection (1997) [1080p] [BluRay]"),
            ("Alien Resurrection", "1997"),
        )

    def test_no_parenthesized_year_yields_none(self) -> None:
        # A bare, unparenthesized year is deliberately not enough -- too
        # likely to belong to a release-group or site-branding folder name
        # rather than an actual "Title (Year)" folder.
        self.assertIsNone(filename_parser.folder_title_candidate("Forensic Files"))
        self.assertIsNone(filename_parser.folder_title_candidate("torrents"))
        self.assertIsNone(filename_parser.folder_title_candidate("Season 00"))
        self.assertIsNone(filename_parser.folder_title_candidate("WatchSoMiuch 2016 Releases"))

    def test_empty_folder_name_yields_none(self) -> None:
        self.assertIsNone(filename_parser.folder_title_candidate(""))


if __name__ == "__main__":
    unittest.main()
