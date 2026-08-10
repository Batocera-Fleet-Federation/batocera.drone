import unittest

from app.music.filename_parser import (
    ParsedLocation,
    ParsedTrackName,
    classify_location,
    parse_track_filename,
    search_candidates,
)


class ClassifyLocationTests(unittest.TestCase):
    def test_no_folders_at_all_is_ungrouped(self):
        self.assertEqual(classify_location("Song.mp3"), ParsedLocation())

    def test_artist_only_folder_is_a_singles_bucket(self):
        result = classify_location("Solo Artist/Song.mp3")
        self.assertEqual(result.artist, "Solo Artist")
        self.assertEqual(result.album, "")
        self.assertIsNone(result.disc_number)

    def test_artist_album_is_the_primary_case(self):
        result = classify_location("Band/Album One/01 - Track.mp3")
        self.assertEqual(result.artist, "Band")
        self.assertEqual(result.album, "Album One")
        self.assertIsNone(result.disc_number)

    def test_disc_subfolder_under_album_is_absorbed_not_a_third_level(self):
        result = classify_location("Band/Double Album/CD1/01 - Track.mp3")
        self.assertEqual(result.artist, "Band")
        self.assertEqual(result.album, "Double Album")
        self.assertEqual(result.disc_number, 1)

    def test_disc_subfolder_variants(self):
        for folder, expected in (("CD2", 2), ("Disc 2", 2), ("Disc.2", 2), ("Disk 02", 2)):
            with self.subTest(folder=folder):
                result = classify_location(f"Band/Album/{folder}/Track.mp3")
                self.assertEqual(result.disc_number, expected)

    def test_flat_disc_folder_directly_under_artist_has_no_album(self):
        # Rare layout: Artist/CD1/Track.mp3 -- no real album folder at all.
        result = classify_location("Band/CD1/Track.mp3")
        self.assertEqual(result.artist, "Band")
        self.assertEqual(result.album, "")
        self.assertEqual(result.disc_number, 1)

    def test_deeper_nesting_beyond_artist_album_still_uses_album_as_the_grouping_level(self):
        result = classify_location("Band/Album/Bonus/Track.mp3")
        self.assertEqual(result.artist, "Band")
        self.assertEqual(result.album, "Album")
        self.assertIsNone(result.disc_number)

    def test_a_two_word_album_starting_with_disc_is_not_misread_as_a_disc_folder(self):
        # "Disc" is only a disc-folder signal when followed by digits.
        result = classify_location("Band/Discography Highlights/Track.mp3")
        self.assertEqual(result.album, "Discography Highlights")
        self.assertIsNone(result.disc_number)


class ParseTrackFilenameTests(unittest.TestCase):
    def test_dash_separated_leading_number(self):
        result = parse_track_filename("01 - Song Title.mp3")
        self.assertEqual(result.track_number, 1)
        self.assertIsNone(result.disc_number)
        self.assertEqual(result.title, "Song Title")

    def test_dot_separated_leading_number(self):
        result = parse_track_filename("01. Song Title.mp3")
        self.assertEqual(result.track_number, 1)
        self.assertEqual(result.title, "Song Title")

    def test_underscore_separated_leading_number(self):
        result = parse_track_filename("03_Song_Title.mp3")
        self.assertEqual(result.track_number, 3)
        self.assertEqual(result.title, "Song Title")

    def test_disc_track_combined_form(self):
        result = parse_track_filename("1-05 Song Title.mp3")
        self.assertEqual(result.disc_number, 1)
        self.assertEqual(result.track_number, 5)
        self.assertEqual(result.title, "Song Title")

    def test_no_leading_number_falls_back_to_whole_stem(self):
        result = parse_track_filename("Song Title.mp3")
        self.assertIsNone(result.track_number)
        self.assertIsNone(result.disc_number)
        self.assertEqual(result.title, "Song Title")

    def test_a_year_like_prefix_is_not_misread_as_a_track_number(self):
        # "2024" has 4 digits, never matches the 1-3 digit track group, and
        # isn't followed by a disc-style '-'/'.' separator either.
        result = parse_track_filename("2024 Remaster.mp3")
        self.assertIsNone(result.track_number)
        self.assertEqual(result.title, "2024 Remaster")

    def test_single_digit_track_number(self):
        result = parse_track_filename("3. Interlude.mp3")
        self.assertEqual(result.track_number, 3)
        self.assertEqual(result.title, "Interlude")

    def test_empty_filename_does_not_raise(self):
        result = parse_track_filename("")
        self.assertEqual(result, ParsedTrackName(title=""))


class SearchCandidatesTests(unittest.TestCase):
    def test_artist_and_album_yields_release_candidates_first(self):
        candidates = search_candidates("Sample Band", "Sample Album", "Track One")
        self.assertEqual(candidates[0], ("release", "Sample Band Sample Album"))
        self.assertIn(("release", "Sample Album"), candidates)

    def test_no_album_falls_back_to_recording_candidates(self):
        candidates = search_candidates("Sample Band", "", "Track One")
        self.assertFalse(any(query_type == "release" for query_type, _ in candidates))
        self.assertIn(("recording", "Sample Band Track One"), candidates)
        self.assertIn(("recording", "Track One"), candidates)

    def test_empty_everything_yields_no_candidates(self):
        self.assertEqual(search_candidates("", "", ""), [])


if __name__ == "__main__":
    unittest.main()
