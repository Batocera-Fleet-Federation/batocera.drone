import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.storage import movie_cast_tokens


def _build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "MOVIES_ROOT": str(root / "movies"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "movie-cast-tokens-test",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class MovieCastTokensTests(unittest.TestCase):
    def test_create_then_verify_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            result = movie_cast_tokens.create(settings, "aaaa")
            self.assertTrue(result["token"])
            self.assertTrue(result["expires_at"])
            self.assertTrue(movie_cast_tokens.verify(settings, "aaaa", result["token"]))

    def test_token_does_not_work_for_a_different_entry_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            result = movie_cast_tokens.create(settings, "aaaa")
            self.assertFalse(movie_cast_tokens.verify(settings, "bbbb", result["token"]))

    def test_unknown_token_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self.assertFalse(movie_cast_tokens.verify(settings, "aaaa", "not-a-real-token"))

    def test_empty_token_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self.assertFalse(movie_cast_tokens.verify(settings, "aaaa", ""))

    def test_expired_token_fails_and_is_swept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            result = movie_cast_tokens.create(settings, "aaaa", ttl_seconds=-1)
            self.assertFalse(movie_cast_tokens.verify(settings, "aaaa", result["token"]))
            # A second create() call opportunistically sweeps expired rows --
            # confirm the expired row is actually gone, not just rejected.
            with movie_cast_tokens._open(settings.userdata_root) as connection:
                row = connection.execute(
                    "SELECT 1 FROM movie_cast_tokens WHERE token = ?", (result["token"],)
                ).fetchone()
            self.assertIsNone(row)

    def test_two_tokens_for_the_same_movie_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            first = movie_cast_tokens.create(settings, "aaaa")
            second = movie_cast_tokens.create(settings, "aaaa")
            self.assertNotEqual(first["token"], second["token"])
            self.assertTrue(movie_cast_tokens.verify(settings, "aaaa", first["token"]))
            self.assertTrue(movie_cast_tokens.verify(settings, "aaaa", second["token"]))


if __name__ == "__main__":
    unittest.main()
