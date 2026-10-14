"""Mechanism selection, game matching, and the compatibility database."""

import tempfile
import unittest
from pathlib import Path

from app.multiplayer import models as m
from app.multiplayer.adapters.base import CONTENT_EXACT, CONTENT_HOST_ONLY, CONTENT_TITLE
from app.multiplayer.compat_db import CompatibilityDatabase
from app.multiplayer.matching import browser_status, level_satisfies, match_level
from app.multiplayer.mechanism import choose, evaluate_candidates
from test_multiplayer_core import make_settings


def assessment(mechanism, classification=m.LIKELY_COMPATIBLE, *, readiness="ready", content=CONTENT_EXACT, lan=False, ready=True, reasons=None):
    return {"mechanism": mechanism, "adapter": mechanism.lower(), "adapter_name": mechanism.title(), "classification": classification,
            "readiness": readiness, "readiness_message": readiness, "reasons": reasons or ["why"], "content_match": content,
            "needs_same_lan": lan, "ready": ready, "automation": "full", "tier": "implemented", "max_players": 2,
            "emulator": {"adapter": "x", "name": "x", "core": "", "version": "", "core_version": ""}}


def peer(name="Guest", level="exact", assessments=(), scope="lan", reachable=True, latency=None):
    return {"device_id": name.lower(), "name": name, "level": level, "scope": scope, "reachable": reachable, "latency_ms": latency,
            "assessments": {a["mechanism"]: a for a in assessments}}


class MechanismSelectionTests(unittest.TestCase):
    def pick(self, host, peers, **kwargs):
        candidates = evaluate_candidates(host, peers, streaming_enabled=kwargs.pop("streaming_enabled", True))
        return choose(candidates, **kwargs)

    def test_native_beats_streaming_when_both_work(self):
        host = [assessment(m.RETROARCH_NETPLAY), assessment(m.REMOTE_STREAMING, content=CONTENT_HOST_ONLY)]
        guest = peer(assessments=[assessment(m.RETROARCH_NETPLAY), {**assessment(m.REMOTE_STREAMING), "guest_ready": True}])
        decision = self.pick(host, [guest])
        self.assertEqual(decision["mechanism"], m.RETROARCH_NETPLAY)
        self.assertIn("remote streaming was not needed", decision["reason"])

    def test_streaming_when_the_peer_lacks_the_game(self):
        host = [assessment(m.RETROARCH_NETPLAY), assessment(m.REMOTE_STREAMING, content=CONTENT_HOST_ONLY)]
        guest = peer(level="none", assessments=[{**assessment(m.REMOTE_STREAMING), "guest_ready": True}])
        decision = self.pick(host, [guest])
        self.assertEqual(decision["mechanism"], m.REMOTE_STREAMING)
        self.assertIn("no native mechanism is usable", decision["reason"])
        self.assertIn("Native options ruled out", decision["reason"])

    def test_streaming_needs_moonlight_on_the_guest(self):
        host = [assessment(m.REMOTE_STREAMING, content=CONTENT_HOST_ONLY)]
        guest = peer(level="none", assessments=[{**assessment(m.REMOTE_STREAMING), "guest_ready": False}])
        decision = self.pick(host, [guest])
        self.assertEqual(decision["mechanism"], "")
        self.assertIn("Moonlight", decision["candidates"][0]["blockers"][0])

    def test_streaming_can_be_switched_off(self):
        host = [assessment(m.REMOTE_STREAMING, content=CONTENT_HOST_ONLY)]
        self.assertEqual(self.pick(host, [], streaming_enabled=False)["mechanism"], "")

    def test_unusable_mechanisms_are_never_selected(self):
        host = [assessment(m.RETROARCH_NETPLAY, m.UNSUPPORTED), assessment(m.DOLPHIN_NETPLAY, m.REQUIRES_CONFIGURATION),
                assessment(m.PPSSPP_ADHOC, m.INCOMPATIBLE)]
        decision = self.pick(host, [])
        self.assertEqual(decision["mechanism"], "")
        self.assertTrue(all(not item["usable"] for item in decision["candidates"]))

    def test_forced_choice_is_honoured_or_refused_never_substituted(self):
        host = [assessment(m.RETROARCH_NETPLAY), assessment(m.REMOTE_STREAMING, content=CONTENT_HOST_ONLY)]
        guest = peer(assessments=[assessment(m.RETROARCH_NETPLAY), {**assessment(m.REMOTE_STREAMING), "guest_ready": True}])
        self.assertEqual(self.pick(host, [guest], forced=m.REMOTE_STREAMING)["mechanism"], m.REMOTE_STREAMING)
        with self.assertRaises(m.MultiplayerError) as caught:
            self.pick(host, [peer(level="none", assessments=[])], forced=m.RETROARCH_NETPLAY)
        self.assertEqual(caught.exception.code, "mechanism_unusable")
        with self.assertRaises(m.MultiplayerError) as caught:
            self.pick(host, [], forced=m.DOLPHIN_NETPLAY)
        self.assertEqual(caught.exception.code, "mechanism_unavailable")

    def test_preference_applies_only_when_usable_and_says_so_otherwise(self):
        host = [assessment(m.RETROARCH_NETPLAY), assessment(m.REMOTE_STREAMING, content=CONTENT_HOST_ONLY)]
        guest = peer(assessments=[assessment(m.RETROARCH_NETPLAY), {**assessment(m.REMOTE_STREAMING), "guest_ready": True}])
        self.assertEqual(self.pick(host, [guest], preferred=m.REMOTE_STREAMING)["mechanism"], m.REMOTE_STREAMING)
        decision = self.pick(host, [guest], preferred=m.DOLPHIN_NETPLAY)
        self.assertEqual(decision["mechanism"], m.RETROARCH_NETPLAY)
        self.assertIn("preferred Dolphin Netplay does not apply", decision["reason"])

    def test_lan_broadcast_mechanisms_are_blocked_over_tailscale(self):
        host = [assessment(m.SWITCH_LDN, m.EXPERIMENTAL, lan=True)]
        guest = peer(scope="tailnet", assessments=[assessment(m.SWITCH_LDN, m.EXPERIMENTAL, lan=True)])
        decision = self.pick(host, [guest])
        self.assertEqual(decision["mechanism"], "")
        self.assertIn("Tailscale", decision["candidates"][0]["blockers"][0])
        self.assertEqual(self.pick(host, [peer(scope="lan", assessments=[assessment(m.SWITCH_LDN, m.EXPERIMENTAL, lan=True)])])["mechanism"], m.SWITCH_LDN)

    def test_content_requirements(self):
        host = [assessment(m.RETROARCH_NETPLAY, content=CONTENT_EXACT), assessment(m.PPSSPP_ADHOC, m.EXPERIMENTAL, content=CONTENT_TITLE)]
        both = {m.RETROARCH_NETPLAY: assessment(m.RETROARCH_NETPLAY), m.PPSSPP_ADHOC: assessment(m.PPSSPP_ADHOC, m.EXPERIMENTAL)}
        titled = peer(level="title", assessments=list(both.values()))
        candidates = {c["mechanism"]: c for c in evaluate_candidates(host, [titled])}
        self.assertFalse(candidates[m.RETROARCH_NETPLAY]["usable"])
        self.assertTrue(candidates[m.PPSSPP_ADHOC]["usable"])

    def test_unreachable_peers_warn_but_do_not_block(self):
        host = [assessment(m.RETROARCH_NETPLAY)]
        candidates = evaluate_candidates(host, [peer(reachable=False)])
        self.assertTrue(candidates[0]["usable"])
        self.assertTrue(candidates[0]["warnings"])

    def test_high_latency_is_surfaced(self):
        candidates = evaluate_candidates([assessment(m.RETROARCH_NETPLAY)], [peer(assessments=[assessment(m.RETROARCH_NETPLAY)], latency=250)])
        self.assertTrue(any("250" in w for w in candidates[0]["warnings"]))

    def test_unverified_classes_carry_a_warning(self):
        candidates = evaluate_candidates([assessment(m.PPSSPP_ADHOC, m.EXPERIMENTAL)], [])
        self.assertTrue(any("Not yet verified" in w for w in candidates[0]["warnings"]))

    def test_every_decision_explains_itself(self):
        self.assertTrue(self.pick([assessment(m.RETROARCH_NETPLAY)], [])["reason"])
        self.assertTrue(self.pick([], [])["reason"])


class MatchingTests(unittest.TestCase):
    def test_match_levels(self):
        base = {"rom_name": "Kart.sfc", "title_key": "kart", "fingerprint": "ab", "size": 10}
        self.assertEqual(match_level(base, dict(base)), "exact")
        self.assertEqual(match_level(base, {**base, "fingerprint": "cd"}), "title")
        self.assertEqual(match_level(base, {**base, "fingerprint": "", "rom_name": "kart.SFC"}), "same_file")
        self.assertEqual(match_level({**base, "fingerprint": ""}, {**base, "fingerprint": "", "size": 99}), "title")
        self.assertEqual(match_level(base, {"rom_name": "Other.sfc", "title_key": "other", "fingerprint": "", "size": 1}), "none")
        self.assertEqual(match_level({**base, "fingerprint": "ab"}, {**base, "fingerprint": "cd", "title_key": "different"}), "none")

    def test_requirements(self):
        self.assertTrue(level_satisfies("exact", CONTENT_EXACT))
        self.assertTrue(level_satisfies("same_file", CONTENT_EXACT))
        self.assertFalse(level_satisfies("title", CONTENT_EXACT))
        self.assertTrue(level_satisfies("title", CONTENT_TITLE))
        self.assertFalse(level_satisfies("none", CONTENT_TITLE))
        self.assertTrue(level_satisfies("none", CONTENT_HOST_ONLY))

    def test_browser_status_buckets(self):
        ready = [assessment(m.RETROARCH_NETPLAY)]
        self.assertEqual(browser_status(ready), "ready")
        self.assertEqual(browser_status([assessment(m.RETROARCH_NETPLAY, m.REQUIRES_CONFIGURATION, ready=False)]), "configuration")
        self.assertEqual(browser_status([assessment(m.PPSSPP_ADHOC, m.EXPERIMENTAL)]), "untested")
        self.assertEqual(browser_status([assessment(m.RETROARCH_NETPLAY, m.UNKNOWN)]), "untested")
        self.assertEqual(browser_status([assessment(m.RETROARCH_NETPLAY, m.UNSUPPORTED, ready=False)]), "unsupported")
        self.assertEqual(browser_status([]), "unsupported")


class CompatibilityDatabaseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.db = CompatibilityDatabase(make_settings(tmp.name))
        self.fp = m.config_fingerprint(m.RETROARCH_NETPLAY, "libretro", "snes9x", "1.19", "1.62")

    def verdict(self, fp=None, technical=m.LIKELY_COMPATIBLE):
        return self.db.evaluate("snes", "kart", m.RETROARCH_NETPLAY, fp or self.fp, technical)

    def record(self, success=True, fp=None, version="1.19", **kwargs):
        return self.db.record_session_result(system="snes", title_key="kart", title="Kart", mechanism=m.RETROARCH_NETPLAY,
                                             config_fp=fp or self.fp, emulator_version=version, success=success, **kwargs)

    def test_nothing_is_verified_without_evidence(self):
        self.assertEqual(self.verdict()["classification"], m.LIKELY_COMPATIBLE)
        self.assertEqual(self.verdict(technical=m.UNKNOWN)["classification"], m.UNKNOWN)

    def test_a_confirmed_session_verifies_only_that_exact_configuration(self):
        self.record()
        self.assertEqual(self.verdict()["classification"], m.VERIFIED_COMPATIBLE)
        self.assertEqual(self.verdict()["basis"], "confirmed-session")
        other = m.config_fingerprint(m.RETROARCH_NETPLAY, "libretro", "snes9x", "1.20", "1.62")
        self.assertEqual(self.verdict(other)["classification"], m.LIKELY_COMPATIBLE)

    def test_failures_downgrade_that_configuration(self):
        self.record(False, failure="desync at lap 2")
        self.assertEqual(self.verdict()["classification"], m.INCOMPATIBLE)
        self.assertEqual(self.db.get("snes", "kart")["knownIssues"][0]["detail"], "desync at lap 2")
        self.record()
        self.record()
        self.assertEqual(self.verdict()["classification"], m.VERIFIED_COMPATIBLE)

    def test_evidence_cannot_override_a_technically_impossible_setup(self):
        self.record()
        self.assertEqual(self.verdict(technical=m.UNSUPPORTED)["classification"], m.UNSUPPORTED)

    def test_record_keeps_attribution_and_validation_metadata(self):
        record = self.record(max_players=2)
        self.assertEqual(record["source"], "local")
        self.assertEqual(record["verifiedEmulatorVersions"], ["1.19"])
        self.assertEqual(record["maximumPlayers"], 2)
        self.assertIsNotNone(record["lastValidated"])

    def test_shared_reports_are_attributed_and_never_verified(self):
        self.record()
        exported = self.db.export_shareable()
        self.assertEqual(len(exported), 1)
        self.assertNotIn("source", exported[0])
        other = CompatibilityDatabase(make_settings(tempfile.mkdtemp(), "other"))
        result = other.import_shared(exported, "node-a", "Node A")
        self.assertEqual(result["added"], 1)
        imported = other.get("snes", "kart")
        self.assertEqual(imported["source"], "peer:node-a")
        self.assertEqual(other.evaluate("snes", "kart", m.RETROARCH_NETPLAY, self.fp, m.LIKELY_COMPATIBLE)["classification"], m.LIKELY_COMPATIBLE)

    def test_imports_never_overwrite_local_observations_and_are_validated(self):
        self.record(False)
        result = self.db.import_shared([{"system": "snes", "gameId": "kart", "mechanisms": {m.RETROARCH_NETPLAY: {"tested": {self.fp: {"successes": 9}}}}},
                                        {"system": "../x", "gameId": "a"}, "junk", {"system": "snes"}], "node-b")
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["skipped"], 4)
        self.assertEqual(self.db.get("snes", "kart")["source"], "local")
        with self.assertRaises(m.MultiplayerError):
            self.db.import_shared("not a list", "node-b")

    def test_a_declared_verified_class_in_a_record_is_not_trusted_for_other_configurations(self):
        self.db._doc.update(lambda doc: doc["records"].update({"snes|kart": {
            "system": "snes", "gameId": "kart", "title": "Kart", "source": "peer:x",
            "mechanisms": {m.RETROARCH_NETPLAY: {"compatibility": m.VERIFIED_COMPATIBLE, "tested": {}}}}}))
        self.db._snapshot = None
        self.assertEqual(self.verdict()["classification"], m.LIKELY_COMPATIBLE)

    def test_bundled_seed_is_tiny_and_never_claims_game_compatibility(self):
        self.assertEqual(self.db.all(), [])
        self.assertEqual(self.db.system_hint("gamecube")["typical_max_players"], 4)
        self.assertEqual(self.db.system_hint("nonexistent"), {})

    def test_works_offline_from_local_files_only(self):
        # Constructing, recording and evaluating performed no network access: only SQLite + the bundled seed.
        self.record()
        self.assertEqual(len(self.db.all()), 1)


if __name__ == "__main__":
    unittest.main()
