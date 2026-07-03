"""Property tests for the shared sampled-hash fingerprint (``sample-fp-v1``).

``common/fingerprint.py`` decides cross-drone file identity: two drones must agree
"is this the same file?" from a constant-cost sampled hash rather than reading
multi-GB disc images end to end. The algorithm makes correctness claims in its own
docstring that nothing exercised — this locks them:

* **deterministic** — same bytes always yield the same fingerprint;
* **size-folded** — files of different size can never collide (size is hashed in);
* **small files are exact** — files at/below the threshold are hashed whole;
* **large files are sampled** — only head/middle/tail windows are read, so a change
  inside a window is detected but a change in the un-sampled gap is *intentionally*
  not (the imohash tradeoff that keeps cost constant — locked so nobody "fixes" it
  into a full read and silently blows the perf guarantee);
* **BIOS uses a true full-file MD5** (:func:`build_md5`).

Also guards that ``RomRepository.build_*`` stay thin delegations (tests monkeypatch
``RomRepository.build_fingerprint``) and that saves reuse the same algorithm on the
sampled (large-file) path, not just for tiny files. See ``drone-db-management`` /
``drone-p2p-transfer-security``.
"""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from app.common import fingerprint as fp
from app.drone_api import RomRepository
from app.storage import saves_store

SAMPLE = fp.FINGERPRINT_SAMPLE_BYTES
SMALL = fp.FINGERPRINT_SMALL_FILE_BYTES


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def _flip_byte_in_place(path: Path, offset: int) -> None:
    """Overwrite a single byte, preserving file size (so only content changes)."""
    with path.open("r+b") as handle:
        handle.seek(offset)
        original = handle.read(1)
        handle.seek(offset)
        handle.write(b"\xff" if original != b"\xff" else b"\x00")


class FingerprintPropertyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    # --- determinism -----------------------------------------------------
    def test_deterministic_small_and_large(self):
        small = _write(self.dir / "small.rom", b"A" * 1024)
        large = _write(self.dir / "large.rom", b"A" * (SMALL + 4 * SAMPLE))
        for path in (small, large):
            with self.subTest(path=path.name):
                self.assertEqual(fp.build_fingerprint(path), fp.build_fingerprint(path))

    # --- size fold: different size can never collide ---------------------
    def test_size_is_folded_in_so_different_sizes_never_collide(self):
        # Two all-zero files above the small-file threshold: every sampled window
        # (head/middle/tail) is identical (all zeros) in both, so the ONLY differing
        # input to the digest is the folded file size. Different fingerprints here
        # prove the size fold is what prevents collisions.
        a = _write(self.dir / "a.bin", b"\x00" * (SMALL + SAMPLE))
        b = _write(self.dir / "b.bin", b"\x00" * (SMALL + 2 * SAMPLE))
        self.assertNotEqual(fp.build_fingerprint(a), fp.build_fingerprint(b))

    # --- small files are exact (hashed whole) ----------------------------
    def test_small_file_is_hashed_whole_so_any_byte_change_shows(self):
        path = _write(self.dir / "small.rom", b"A" * 1024)  # 1024 <= SMALL
        before = fp.build_fingerprint(path)
        _flip_byte_in_place(path, 500)  # a byte no sampled window would cover on a large file
        self.assertNotEqual(before, fp.build_fingerprint(path))

    # --- large files: sampled windows are detected -----------------------
    def test_large_file_detects_changes_in_sampled_windows(self):
        size = SMALL + 4 * SAMPLE
        center = size // 2
        for label, offset in [("head", 10), ("middle", center), ("tail", size - 10)]:
            with self.subTest(window=label):
                path = _write(self.dir / f"{label}.bin", b"A" * size)
                before = fp.build_fingerprint(path)
                _flip_byte_in_place(path, offset)
                self.assertNotEqual(before, fp.build_fingerprint(path),
                                    f"change in {label} window ({offset}) was not detected")

    def test_large_file_ignores_change_in_unsampled_gap_by_design(self):
        # Documented tradeoff: between head [0,SAMPLE) and the middle window, the bytes
        # are never read, so an in-place edit there (size preserved) does NOT change the
        # fingerprint. This is intentional (constant cost on huge disc images). If this
        # ever fails, someone changed the sampling to read more — reconsider on purpose.
        size = SMALL + 4 * SAMPLE
        gap_offset = SAMPLE + SAMPLE // 2  # past head, well before the middle window
        path = _write(self.dir / "gap.bin", b"A" * size)
        before = fp.build_fingerprint(path)
        _flip_byte_in_place(path, gap_offset)
        self.assertEqual(before, fp.build_fingerprint(path),
                         "un-sampled gap byte affected the fingerprint (sampling changed?)")

    # --- BIOS full-file MD5 ---------------------------------------------
    def test_build_md5_is_true_full_file_md5(self):
        payload = os.urandom(3 * 1024 * 1024 + 7)  # spans multiple 1MB read chunks
        path = _write(self.dir / "bios.bin", payload)
        self.assertEqual(fp.build_md5(path), hashlib.md5(payload).hexdigest())

    # --- unique id: path + size + mtime ---------------------------------
    def test_unique_id_tracks_size_and_mtime(self):
        path = _write(self.dir / "u.rom", b"A" * 100)
        os.utime(path, (1000, 1000))
        base = fp.build_unique_id(path)
        self.assertEqual(base, fp.build_unique_id(path))  # stable with no change
        os.utime(path, (2000, 2000))                      # mtime change
        self.assertNotEqual(base, fp.build_unique_id(path))
        after_mtime = fp.build_unique_id(path)
        _write(path, b"A" * 200)                          # size change
        os.utime(path, (2000, 2000))                      # hold mtime; isolate size
        self.assertNotEqual(after_mtime, fp.build_unique_id(path))

    # --- directory stats -------------------------------------------------
    def test_directory_stats_sums_sizes_and_takes_latest_mtime(self):
        d = self.dir / "romset"
        d.mkdir()
        _write(d / "a.rom", b"x" * 100)
        _write(d / "b.rom", b"y" * 250)
        os.utime(d / "a.rom", (1500, 1500))
        os.utime(d / "b.rom", (2500, 2500))
        os.utime(d, (1000, 1000))  # set dir mtime last (writes had bumped it)
        total, latest = fp.build_directory_stats(d)
        self.assertEqual(total, 350)
        self.assertEqual(latest, 2500)

    # --- delegation guards ----------------------------------------------
    def test_rom_repository_wrappers_delegate_unchanged(self):
        path = _write(self.dir / "w.rom", b"A" * (SMALL + SAMPLE))
        os.utime(path, (1234, 1234))
        d = self.dir / "dir"
        d.mkdir()
        _write(d / "c.rom", b"z" * 42)
        self.assertEqual(RomRepository.build_fingerprint(path), fp.build_fingerprint(path))
        self.assertEqual(RomRepository.build_md5(path), fp.build_md5(path))
        self.assertEqual(RomRepository.build_unique_id(path), fp.build_unique_id(path))
        self.assertEqual(RomRepository.build_directory_stats(d), fp.build_directory_stats(d))

    def test_saves_reuse_the_same_algorithm_on_the_sampled_path(self):
        # The existing saves test only checks a tiny (whole-hashed) file; assert the
        # shared algorithm holds on the sampled large-file branch too.
        path = _write(self.dir / "game.srm", b"S" * (SMALL + 3 * SAMPLE))
        self.assertEqual(saves_store.build_save_fingerprint(path), fp.build_fingerprint(path))


if __name__ == "__main__":
    unittest.main()
