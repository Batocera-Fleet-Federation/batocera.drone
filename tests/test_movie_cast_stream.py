import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.movies import cast_stream


def _settings(root: Path, **overrides) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "MOVIES_ROOT": str(root / "movies"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "movie-cast-stream-test",
    }
    env.update(overrides)
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class MovieCastCompatibilityTests(unittest.TestCase):
    def test_direct_compatibility_requires_receiver_supported_container_and_codecs(self) -> None:
        self.assertTrue(
            cast_stream.is_direct_compatible(
                Path("Movie.mp4"), cast_stream.MediaProbe("h264", "aac", 1920, 1080, 41)
            )
        )
        self.assertTrue(
            cast_stream.is_direct_compatible(
                Path("Movie.webm"), cast_stream.MediaProbe("vp8", "opus", 1920, 1080, 0)
            )
        )
        self.assertFalse(
            cast_stream.is_direct_compatible(
                Path("Movie.webm"), cast_stream.MediaProbe("vp9", "opus", 1920, 1080, 0)
            )
        )
        self.assertFalse(
            cast_stream.is_direct_compatible(
                Path("Movie.mp4"), cast_stream.MediaProbe("hevc", "aac", 1920, 1080, 51)
            )
        )
        self.assertFalse(
            cast_stream.is_direct_compatible(
                Path("Episode.mkv"), cast_stream.MediaProbe("h264", "aac", 1920, 1080, 42)
            )
        )

    def test_unknown_probe_keeps_web_native_files_direct_but_not_mkv(self) -> None:
        self.assertTrue(cast_stream.is_direct_compatible(Path("Movie.mp4"), None))
        self.assertTrue(cast_stream.is_direct_compatible(Path("Movie.webm"), None))
        self.assertFalse(cast_stream.is_direct_compatible(Path("Episode.mkv"), None))

    def test_ffmpeg_command_remuxes_compatible_mkv_without_reencoding(self) -> None:
        command = cast_stream.build_ffmpeg_command(
            "/usr/bin/ffmpeg",
            Path("Episode.mkv"),
            Path("/tmp/cast"),
            cast_stream.MediaProbe("h264", "aac", 1920, 1080, 41),
        )
        self.assertIn("-re", command)
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(command[command.index("-c:a") + 1], "copy")
        self.assertIn("independent_segments+temp_file", command)

    def test_ffmpeg_command_transcodes_hevc_and_incompatible_audio(self) -> None:
        command = cast_stream.build_ffmpeg_command(
            "/usr/bin/ffmpeg",
            Path("Episode.mkv"),
            Path("/tmp/cast"),
            cast_stream.MediaProbe("hevc", "eac3", 3840, 2160, 51),
        )
        self.assertEqual(command[command.index("-c:v") + 1], "libx264")
        self.assertEqual(command[command.index("-c:a") + 1], "aac")
        self.assertIn("scale=w='min(1920,iw)':h=-2:force_original_aspect_ratio=decrease,fps=30", command)

    def test_probe_media_reads_first_video_and_audio_streams(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            payload = json.dumps({
                "streams": [
                    {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160, "level": 153},
                    {"codec_type": "audio", "codec_name": "eac3"},
                ]
            })
            completed = mock.Mock(stdout=payload)
            with mock.patch.object(cast_stream, "_find_executable", return_value="/usr/bin/ffprobe"), mock.patch.object(
                cast_stream.subprocess, "run", return_value=completed
            ):
                result = cast_stream.probe_media(settings, Path(tmp) / "Movie.mkv")
            self.assertEqual(result, cast_stream.MediaProbe("hevc", "eac3", 3840, 2160, 153))

    def test_direct_prepare_does_not_require_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _settings(root)
            source = root / "Movie.mp4"
            source.write_bytes(b"not-a-real-mp4")
            with mock.patch.object(cast_stream, "probe_media", return_value=None):
                result = cast_stream.prepare(settings, source, "abcd", "A" * 32)
            self.assertEqual(result["delivery"], "direct")
            self.assertIn("cast-stream?token=", result["url_path"])

    def test_incompatible_prepare_reports_disabled_compatibility_streaming(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _settings(root, DRONE_CAST_TRANSCODE_ENABLED="0")
            source = root / "Episode.mkv"
            source.write_bytes(b"x")
            with mock.patch.object(cast_stream, "probe_media", return_value=None):
                with self.assertRaisesRegex(ValueError, "compatibility streaming"):
                    cast_stream.prepare(settings, source, "abcd", "A" * 32)

    def test_hls_resolver_only_exposes_playlist_and_numbered_segments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _settings(root)
            token = "A" * 32
            directory = settings.cast_cache_root / token
            directory.mkdir(parents=True)
            (directory / "index.m3u8").write_text("#EXTM3U\nmedia.m3u8\n", encoding="utf-8")
            (directory / "media.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
            (directory / "segment-000000.ts").write_bytes(b"segment")
            (directory / "ffmpeg.log").write_text("secret diagnostics", encoding="utf-8")

            playlist, playlist_type = cast_stream.resolve_hls_asset(settings, "abcd", token, "index.m3u8")
            media_playlist, media_playlist_type = cast_stream.resolve_hls_asset(settings, "abcd", token, "media.m3u8")
            segment, segment_type = cast_stream.resolve_hls_asset(settings, "abcd", token, "segment-000000.ts")
            self.assertEqual(playlist, (directory / "index.m3u8").resolve())
            self.assertEqual(playlist_type, cast_stream.HLS_CONTENT_TYPE)
            self.assertEqual(media_playlist, (directory / "media.m3u8").resolve())
            self.assertEqual(media_playlist_type, cast_stream.HLS_CONTENT_TYPE)
            self.assertEqual(segment, (directory / "segment-000000.ts").resolve())
            self.assertEqual(segment_type, cast_stream.HLS_SEGMENT_CONTENT_TYPE)
            for rejected in ("ffmpeg.log", "../ffmpeg.log", "segment-1.ts", "other.m3u8"):
                with self.assertRaises(FileNotFoundError, msg=rejected):
                    cast_stream.resolve_hls_asset(settings, "abcd", token, rejected)


if __name__ == "__main__":
    unittest.main()
