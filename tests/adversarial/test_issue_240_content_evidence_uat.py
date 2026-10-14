"""Issue #240 section 7/13: successful sessions verify exact content only.

Repository sample-fp-v1 intentionally reads three windows, so two ROM revisions
can legitimately have identical sampled library fingerprints. A successful
session for the first must not verify the second. The integration below uses
real sampled hashing, full hashing, SQLite evidence and orchestration; emulator
launch/network are explicitly simulated. No full-library hash is required.
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (str(ROOT), str(ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.common.fingerprint import build_fingerprint, FINGERPRINT_SAMPLE_BYTES, FINGERPRINT_SMALL_FILE_BYTES
from app.multiplayer import models as m
from multiplayer_support import make_swarm


class ContentEvidenceUAT(unittest.TestCase):
    def test_confirmed_revision_does_not_verify_an_unsampled_content_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub, nodes = make_swarm(tmp, open_ports=(55435,))
            host, guest = nodes["host"].orch, nodes["guest"].orch
            size = max(FINGERPRINT_SMALL_FILE_BYTES + 1, 8 * FINGERPRINT_SAMPLE_BYTES)
            paths = {}
            for name, node in nodes.items():
                node.network.host_address = lambda peer, preferred="auto": "192.168.50.10"
                path = node.root / "roms/snes" / node.repo.games["snes"][0]["file"]
                path.write_bytes(b"A" * size)
                paths[name] = path
                node.repo.games["snes"][0].update(size=size, fingerprint=build_fingerprint(path))
                node.orch.library.invalidate()
            sampled = build_fingerprint(paths["host"])
            request = {"system": "snes", "game_id": "g1", "peers": ["guest"],
                       "emulator_override": {"core": "snes9x"}}
            sid = host.create_session(request)["id"]
            first_digest = host.store.require(sid)["game"]["sha256"]
            guest.accept_invitation(sid)
            guest.set_ready(sid, True)
            host.set_ready(sid, True)
            host.start_session(sid)
            guest.confirm_launch(sid)
            self.assertEqual(host.get_session(sid)["state"], m.RUNNING)
            host.end_session(sid)
            host.record_result(sid, True)
            first = host.assessor.assess(host.library.get("snes", "g1"), core_override="snes9x")
            self.assertEqual(first[0]["classification"], m.VERIFIED_COMPATIBLE,
                             "Control: unchanged successful content retains its local evidence")
            for name, path in paths.items():
                # Beyond the head window, before the middle window; no size/name/config change.
                with path.open("r+b") as rom:
                    rom.seek(2 * FINGERPRINT_SAMPLE_BYTES)
                    rom.write(b"B")
                self.assertEqual(build_fingerprint(path), sampled,
                                 "Fixture must have a genuine sampled match, not fabricated stale metadata")
                nodes[name].orch.library.invalidate()
            second = host.create_session(request)["id"]
            second_digest = host.store.require(second)["game"]["sha256"]
            self.assertNotEqual(first_digest, second_digest)
            assessments = host.assessor.assess(host.library.get("snes", "g1"), core_override="snes9x")
            self.assertNotEqual(assessments[0]["classification"], m.VERIFIED_COMPATIBLE,
                                "Evidence for different full content must not verify this unplayed ROM revision")


if __name__ == "__main__":
    unittest.main()
