import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.storage.music_scrape_job_items as music_scrape_job_items
import app.storage.music_scrape_jobs as music_scrape_jobs
import app.storage.music_store as music_store
from app.common.settings import Settings
from app.music import metadata_manager
from app.music.musicbrainz_client import MusicBrainzNotFoundError, MusicBrainzUnavailableError


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
    "date": "2001-05-01",
    "genres": ["Rock"],
    "release_group_mbid": "rg-1",
    "tracks": [
        {"disc_number": 1, "track_number": 1, "title": "Track One", "recording_mbid": "rec-1", "length_ms": 180000},
        {"disc_number": 1, "track_number": 2, "title": "Track Two", "recording_mbid": "rec-2", "length_ms": 200000},
    ],
}
_SINGLE_TRACK_RELEASE = {
    "release_mbid": "release-2",
    "title": "Single",
    "artist": "Solo Singer",
    "date": "2010-01-01",
    "genres": [],
    "release_group_mbid": "rg-2",
    "tracks": [
        {"disc_number": 1, "track_number": 1, "title": "The Only Song", "recording_mbid": "rec-9", "length_ms": 210000},
    ],
}


class FakeMusicBrainzClient:
    def __init__(self, *, release_search_results=None, recording_search_results=None, releases=None, raise_on_lookup=None):
        self._release_search_results = release_search_results or []
        self._recording_search_results = recording_search_results or []
        self._releases = releases or {}
        self._raise_on_lookup = raise_on_lookup or {}
        self.lookup_calls = []

    def search_release(self, query, limit=10):
        return self._release_search_results

    def search_recording(self, query, limit=10):
        return self._recording_search_results

    def release_lookup(self, release_mbid):
        self.lookup_calls.append(release_mbid)
        if release_mbid in self._raise_on_lookup:
            raise self._raise_on_lookup[release_mbid]
        return self._releases[release_mbid]


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


class ApplyTests(unittest.TestCase):
    def test_matches_by_disc_and_track_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Sample Artist/Sample Album/02 - Track Two.mp3")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]

            client = FakeMusicBrainzClient(releases={"release-1": _ALBUM_RELEASE})
            cover_client = FakeCoverArtClient()
            result = metadata_manager.apply(settings, entry_key, "release-1", client=client, cover_client=cover_client)

            self.assertEqual(result["title"], "Track Two")
            self.assertEqual(result["provider"], "musicbrainz")
            self.assertEqual(result["provider_id"], "rec-2")
            self.assertEqual(result["artist"], "Sample Artist")
            self.assertEqual(result["album"], "Sample Album")
            self.assertEqual(result["track_number"], 2)
            self.assertEqual(result["genres"], ["Rock"])
            self.assertTrue(result["art_relative_path"])
            self.assertEqual(len(cover_client.downloaded_urls), 1)

    def test_single_track_release_matches_regardless_of_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Solo Singer/Weirdly Named File.mp3")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]

            client = FakeMusicBrainzClient(releases={"release-2": _SINGLE_TRACK_RELEASE})
            result = metadata_manager.apply(settings, entry_key, "release-2", client=client, cover_client=FakeCoverArtClient())

            self.assertEqual(result["title"], "The Only Song")

    def test_title_match_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # No leading track number to match on, but the parsed (whole-stem)
            # title matches a track title in the release exactly.
            _write_track(root, "Sample Artist/Sample Album/Track One.mp3")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]

            client = FakeMusicBrainzClient(releases={"release-1": _ALBUM_RELEASE})
            result = metadata_manager.apply(settings, entry_key, "release-1", client=client, cover_client=FakeCoverArtClient())

            self.assertEqual(result["provider_id"], "rec-1")

    def test_recording_mbid_hint_matches_exactly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Sample Artist/Sample Album/99 - Mislabeled.mp3")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]

            client = FakeMusicBrainzClient(releases={"release-1": _ALBUM_RELEASE})
            result = metadata_manager.apply(
                settings, entry_key, "release-1", recording_mbid="rec-2", client=client, cover_client=FakeCoverArtClient(),
            )
            self.assertEqual(result["provider_id"], "rec-2")

    def test_no_match_raises_music_match_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Sample Artist/Sample Album/Completely Unrelated Name.mp3")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]

            client = FakeMusicBrainzClient(releases={"release-1": _ALBUM_RELEASE})
            with self.assertRaises(metadata_manager.MusicMatchError):
                metadata_manager.apply(settings, entry_key, "release-1", client=client, cover_client=FakeCoverArtClient())

    def test_unknown_entry_key_raises_music_not_found_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(metadata_manager.MusicNotFoundError):
                metadata_manager.apply(settings, "deadbeef", "release-1", client=FakeMusicBrainzClient())

    def test_missing_cover_art_does_not_fail_the_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Sample Artist/Sample Album/01 - Track One.mp3")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]

            client = FakeMusicBrainzClient(releases={"release-1": _ALBUM_RELEASE})
            cover_client = FakeCoverArtClient(front_url=None)
            result = metadata_manager.apply(settings, entry_key, "release-1", client=client, cover_client=cover_client)
            self.assertIsNone(result["art_relative_path"])


class DeleteTests(unittest.TestCase):
    def test_delete_metadata_is_idempotent_and_removes_artwork(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Sample Artist/Sample Album/01 - Track One.mp3")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            entry_key = music_store.list_music(settings.music_root)[0]["entry_key"]
            metadata_manager.apply(
                settings, entry_key, "release-1",
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


class SearchDefaultQueryTests(unittest.TestCase):
    def test_tries_release_query_before_recording_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            client = FakeMusicBrainzClient(release_search_results=[{"release_mbid": "release-1", "title": "Sample Album"}])
            outcome = metadata_manager.search_track_default_query(settings, "Sample Artist", "Sample Album", "Track One", client=client)
            self.assertEqual(outcome["results"][0]["kind"], "release")

    def test_falls_back_to_recording_query_when_no_album(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            client = FakeMusicBrainzClient(recording_search_results=[{"recording_mbid": "rec-9", "title": "The Only Song"}])
            outcome = metadata_manager.search_track_default_query(settings, "Solo Singer", "", "The Only Song", client=client)
            self.assertEqual(outcome["results"][0]["kind"], "recording")

    def test_no_results_anywhere_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            outcome = metadata_manager.search_track_default_query(settings, "Nobody", "Nothing", "Untitled", client=FakeMusicBrainzClient())
            self.assertEqual(outcome["results"], [])


class MatchTrackInReleaseTests(unittest.TestCase):
    def test_single_track_release_fallback_matches_when_allowed(self):
        matched = metadata_manager._match_track_in_release(
            _SINGLE_TRACK_RELEASE, recording_mbid=None, disc_number=None, track_number=None, parsed_title="",
        )
        self.assertEqual(matched["recording_mbid"], "rec-9")

    def test_single_track_release_fallback_is_suppressed_when_disallowed(self):
        # The bulk scraper's groups loop disables this for a multi-track
        # local group -- see the docstring on allow_single_track_fallback.
        matched = metadata_manager._match_track_in_release(
            _SINGLE_TRACK_RELEASE, recording_mbid=None, disc_number=None, track_number=None, parsed_title="",
            allow_single_track_fallback=False,
        )
        self.assertIsNone(matched)

    def test_disc_and_track_number_match_still_works_regardless_of_the_fallback_flag(self):
        matched = metadata_manager._match_track_in_release(
            _ALBUM_RELEASE, recording_mbid=None, disc_number=1, track_number=2, parsed_title="",
            allow_single_track_fallback=False,
        )
        self.assertEqual(matched["recording_mbid"], "rec-2")

    def test_title_match_still_works_when_the_fallback_is_disallowed(self):
        matched = metadata_manager._match_track_in_release(
            _SINGLE_TRACK_RELEASE, recording_mbid=None, disc_number=None, track_number=None,
            parsed_title="The Only Song", allow_single_track_fallback=False,
        )
        self.assertEqual(matched["recording_mbid"], "rec-9")


class SelectReleaseCandidateTests(unittest.TestCase):
    def test_prefers_a_candidate_whose_track_count_covers_the_local_group(self):
        results = [
            {"release_mbid": "broadcast-1", "track_count": 1},
            {"release_mbid": "full-album", "track_count": 11},
        ]
        chosen = metadata_manager._select_release_candidate(results, 11)
        self.assertEqual(chosen["release_mbid"], "full-album")

    def test_falls_back_to_the_first_result_when_no_candidate_is_plausible(self):
        results = [{"release_mbid": "release-a", "track_count": 1}, {"release_mbid": "release-b", "track_count": 2}]
        chosen = metadata_manager._select_release_candidate(results, 11)
        self.assertEqual(chosen["release_mbid"], "release-a")

    def test_falls_back_to_the_first_result_when_every_track_count_is_unknown(self):
        results = [{"release_mbid": "release-a", "track_count": None}, {"release_mbid": "release-b", "track_count": None}]
        chosen = metadata_manager._select_release_candidate(results, 11)
        self.assertEqual(chosen["release_mbid"], "release-a")

    def test_skips_past_an_unknown_track_count_to_find_a_plausible_later_candidate(self):
        results = [{"release_mbid": "release-a", "track_count": None}, {"release_mbid": "release-b", "track_count": 11}]
        chosen = metadata_manager._select_release_candidate(results, 11)
        self.assertEqual(chosen["release_mbid"], "release-b")

    def test_a_single_result_is_always_returned_regardless_of_track_count(self):
        results = [{"release_mbid": "only-one", "track_count": 1}]
        chosen = metadata_manager._select_release_candidate(results, 11)
        self.assertEqual(chosen["release_mbid"], "only-one")


class GroupBulkCandidatesTests(unittest.TestCase):
    def test_groups_by_artist_and_album_separates_singles_and_orphans(self):
        rows = [
            {"entry_key": "a", "file_path": "Band/Album/01 - A.mp3", "track_name": "01 - A.mp3"},
            {"entry_key": "b", "file_path": "Band/Album/02 - B.mp3", "track_name": "02 - B.mp3"},
            {"entry_key": "c", "file_path": "Band/Single.mp3", "track_name": "Single.mp3"},
            {"entry_key": "d", "file_path": "Orphan.mp3", "track_name": "Orphan.mp3"},
        ]
        groups, singles, orphans = metadata_manager._group_bulk_candidates(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["rows"]), 2)
        self.assertEqual(groups[0]["artist"], "Band")
        self.assertEqual(groups[0]["album"], "Album")
        self.assertEqual([r["entry_key"] for r in singles], ["c"])
        self.assertEqual([r["entry_key"] for r in orphans], ["d"])


class BulkScrapeJobTests(unittest.TestCase):
    def test_full_album_matches_every_track_with_one_release_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Sample Artist/Sample Album/01 - Track One.mp3")
            _write_track(root, "Sample Artist/Sample Album/02 - Track Two.mp3")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            rows = music_store.list_music(settings.music_root)

            client = FakeMusicBrainzClient(
                release_search_results=[{"release_mbid": "release-1", "title": "Sample Album"}],
                releases={"release-1": _ALBUM_RELEASE},
            )
            groups, singles, _orphans = metadata_manager._group_bulk_candidates(rows)
            job = music_scrape_jobs.create_running(settings, rescan_all=True, total=len(groups) + len(singles))
            metadata_manager._run_bulk_scrape_job(settings, job["id"], groups, singles, client, FakeCoverArtClient())

            status = music_scrape_jobs.latest(settings)
            self.assertEqual(status["status"], "complete")
            self.assertEqual(status["matched_count"], 2)
            self.assertEqual(status["failed_count"], 0)
            # One release lookup serves both tracks -- the whole point of
            # grouping by (artist, album) instead of iterating per track.
            self.assertEqual(client.lookup_calls, ["release-1"])

    def test_not_found_release_search_result_skips_the_group_not_the_whole_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Band/Album/01 - A.mp3")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            rows = music_store.list_music(settings.music_root)

            client = FakeMusicBrainzClient(release_search_results=[])  # no results at all
            groups, singles, _orphans = metadata_manager._group_bulk_candidates(rows)
            job = music_scrape_jobs.create_running(settings, rescan_all=True, total=len(groups) + len(singles))
            metadata_manager._run_bulk_scrape_job(settings, job["id"], groups, singles, client, FakeCoverArtClient())

            status = music_scrape_jobs.latest(settings)
            self.assertEqual(status["status"], "complete")
            self.assertEqual(status["skipped_count"], 1)

    def test_unavailable_error_mid_run_marks_remaining_failed_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Band A/Album A/01 - A.mp3")
            _write_track(root, "Band B/Album B/01 - B.mp3")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            rows = music_store.list_music(settings.music_root)
            groups, singles, _orphans = metadata_manager._group_bulk_candidates(rows)
            self.assertEqual(len(groups), 2)

            client = FakeMusicBrainzClient(
                release_search_results=[{"release_mbid": "release-1"}],
                raise_on_lookup={"release-1": MusicBrainzUnavailableError("rate limited")},
            )
            job = music_scrape_jobs.create_running(settings, rescan_all=True, total=len(groups) + len(singles))
            metadata_manager._run_bulk_scrape_job(settings, job["id"], groups, singles, client, FakeCoverArtClient())

            status = music_scrape_jobs.latest(settings)
            self.assertEqual(status["status"], "complete")
            self.assertEqual(status["failed_count"], 2)
            failed_items = music_scrape_job_items.list_by_status(settings, "failed")
            self.assertEqual(failed_items["total"], 2)

    def test_a_multi_track_group_matched_to_a_single_track_release_fails_safely_instead_of_duplicating(self):
        # Reproduces a real live bug: an 11-track local compilation folder's
        # top MusicBrainz search hit was a "Broadcast" release with exactly
        # one track (a DJ radio set) -- every one of the 11 distinct local
        # files silently received that one track's identical scraped title.
        # Untagged filenames (no leading track number) here deliberately
        # isolate the single-track fallback from genuine disc/track-number
        # matching -- see MatchTrackInReleaseTests for that distinction.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Paul Oakenfold/Perfecto On Tour 2000/Dope Smugglaz - Double Double Dutch.mp3")
            _write_track(root, "Paul Oakenfold/Perfecto On Tour 2000/Dope Smugglaz - The World.mp3")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            rows = music_store.list_music(settings.music_root)

            client = FakeMusicBrainzClient(
                release_search_results=[{"release_mbid": "release-2", "track_count": 1}],
                releases={"release-2": _SINGLE_TRACK_RELEASE},
            )
            groups, singles, _orphans = metadata_manager._group_bulk_candidates(rows)
            job = music_scrape_jobs.create_running(settings, rescan_all=True, total=len(groups) + len(singles))
            metadata_manager._run_bulk_scrape_job(settings, job["id"], groups, singles, client, FakeCoverArtClient())

            status = music_scrape_jobs.latest(settings)
            self.assertEqual(status["matched_count"], 0)
            self.assertEqual(status["failed_count"], 2)
            for row in rows:
                self.assertIsNone(music_store.get_music_metadata(settings.music_root, row["entry_key"]))

    def test_prefers_a_track_count_matching_candidate_over_the_top_search_hit(self):
        # The fix's other half: given a *choice* of candidates, prefer the
        # one whose track_count actually fits the local group instead of
        # blindly trusting the top search result -- avoids the bad pick
        # (and therefore the safe-failure above) entirely when a better
        # candidate is available.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Paul Oakenfold/Perfecto On Tour 2000/01 - Track One.mp3")
            _write_track(root, "Paul Oakenfold/Perfecto On Tour 2000/02 - Track Two.mp3")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            rows = music_store.list_music(settings.music_root)

            client = FakeMusicBrainzClient(
                release_search_results=[
                    {"release_mbid": "release-2", "track_count": 1},  # the wrong, top-ranked hit
                    {"release_mbid": "release-1", "track_count": 2},  # the real album
                ],
                releases={"release-1": _ALBUM_RELEASE, "release-2": _SINGLE_TRACK_RELEASE},
            )
            groups, singles, _orphans = metadata_manager._group_bulk_candidates(rows)
            job = music_scrape_jobs.create_running(settings, rescan_all=True, total=len(groups) + len(singles))
            metadata_manager._run_bulk_scrape_job(settings, job["id"], groups, singles, client, FakeCoverArtClient())

            status = music_scrape_jobs.latest(settings)
            self.assertEqual(status["matched_count"], 2)
            self.assertEqual(client.lookup_calls, ["release-1"])

    def test_stop_requested_before_the_job_starts_processing_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Sample Artist/Sample Album/01 - Track One.mp3")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            rows = music_store.list_music(settings.music_root)
            groups, singles, _orphans = metadata_manager._group_bulk_candidates(rows)
            job = music_scrape_jobs.create_running(settings, rescan_all=True, total=len(groups) + len(singles))
            music_scrape_jobs.request_stop(settings, job["id"])

            client = FakeMusicBrainzClient(
                release_search_results=[{"release_mbid": "release-1"}], releases={"release-1": _ALBUM_RELEASE},
            )
            metadata_manager._run_bulk_scrape_job(settings, job["id"], groups, singles, client, FakeCoverArtClient())

            status = music_scrape_jobs.latest(settings)
            self.assertEqual(status["status"], "stopped")
            self.assertEqual(status["processed"], 0)
            self.assertEqual(status["matched_count"], 0)
            self.assertEqual(client.lookup_calls, [])

    def test_stop_requested_during_groups_halts_before_singles(self):
        # The groups loop and the singles loop are two separate `for`
        # statements in _run_bulk_scrape_job -- this exercises the second
        # loop's own stop-check, not just the first's.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Filenames whose parsed titles literally equal _ALBUM_RELEASE's
            # track titles -- same title-match path
            # test_full_album_matches_every_track_with_one_release_lookup
            # relies on (there's no disc folder here, so the disc/track
            # number branch in _match_track_in_release never applies).
            _write_track(root, "Band A/Album A/01 - Track One.mp3")
            _write_track(root, "Band B/Single Track.mp3")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            rows = music_store.list_music(settings.music_root)
            groups, singles, _orphans = metadata_manager._group_bulk_candidates(rows)
            self.assertEqual(len(groups), 1)
            self.assertEqual(len(singles), 1)
            job = music_scrape_jobs.create_running(settings, rescan_all=True, total=len(groups) + len(singles))

            class _StopAfterGroupClient(FakeMusicBrainzClient):
                def release_lookup(self, release_mbid):
                    result = super().release_lookup(release_mbid)
                    music_scrape_jobs.request_stop(settings, job["id"])
                    return result

            client = _StopAfterGroupClient(
                release_search_results=[{"release_mbid": "release-1"}], releases={"release-1": _ALBUM_RELEASE},
            )
            metadata_manager._run_bulk_scrape_job(settings, job["id"], groups, singles, client, FakeCoverArtClient())

            status = music_scrape_jobs.latest(settings)
            self.assertEqual(status["status"], "stopped")
            # The group's own track (matching _ALBUM_RELEASE's track 1 by
            # track number) was matched before the stop took effect; the
            # single, reached only by the second loop, was never attempted.
            self.assertEqual(status["matched_count"], 1)
            self.assertEqual(status["skipped_count"], 0)
            self.assertEqual(status["failed_count"], 0)

    def test_release_not_found_only_fails_that_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_track(root, "Band A/Album A/01 - A.mp3")
            _write_track(root, "Sample Artist/Sample Album/01 - Track One.mp3")
            settings = _build_settings(root)
            music_store.sync_music_cache(settings.music_root)
            rows = music_store.list_music(settings.music_root)
            groups, singles, _orphans = metadata_manager._group_bulk_candidates(rows)
            groups.sort(key=lambda g: g["artist"])  # deterministic order: "Band A" then "Sample Artist"

            call_count = {"n": 0}

            class _SequencedClient(FakeMusicBrainzClient):
                def search_release(self, query, limit=10):
                    call_count["n"] += 1
                    if call_count["n"] == 1:
                        return [{"release_mbid": "missing-release"}]
                    return [{"release_mbid": "release-1"}]

            client = _SequencedClient(
                releases={"release-1": _ALBUM_RELEASE},
                raise_on_lookup={"missing-release": MusicBrainzNotFoundError("no such release")},
            )
            job = music_scrape_jobs.create_running(settings, rescan_all=True, total=len(groups) + len(singles))
            metadata_manager._run_bulk_scrape_job(settings, job["id"], groups, singles, client, FakeCoverArtClient())

            status = music_scrape_jobs.latest(settings)
            self.assertEqual(status["status"], "complete")
            self.assertEqual(status["matched_count"], 1)
            self.assertEqual(status["failed_count"], 1)


if __name__ == "__main__":
    unittest.main()
