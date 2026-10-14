"""Issue #240 sections 10, 14, 17, 18: disable and cleanup error acceptance.

Expectations derived from the issue: disabling an adapter prevents a NEW start;
ending must attempt independent cleanup even when process shutdown fails; a
transient restore error retains its journal and can recover through housekeeping
without a Drone restart. Fakes simulate emulators only; config files and SQLite
are real, isolated temporary resources. No real devices or network are used.
"""
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
for path in (str(ROOT), str(ROOT / 'tests')):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.multiplayer import models as m
from multiplayer_support import make_swarm


class CleanupErrorUAT(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        hostname = mock.patch('app.multiplayer.core.socket.gethostname', return_value='test-drone')
        hostname.start()
        self.addCleanup(hostname.stop)
        self.clock = datetime(2032, 5, 10, 12, tzinfo=timezone.utc)
        clock = mock.patch.object(m, 'now', side_effect=lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        self.hub, self.nodes = make_swarm(temporary.name, open_ports=(55435,))
        for node in self.nodes.values():
            node.network.host_address = lambda peer, preferred='auto': '192.168.50.10'
        self.h, self.g = self.nodes['host'].orch, self.nodes['guest'].orch
        self.original = self.nodes['host'].conf_text

    def lobby(self):
        sid = self.h.create_session({'system': 'snes', 'game_id': 'g1', 'peers': ['guest'],
                                     'emulator_override': {'core': 'snes9x'}})['id']
        self.g.accept_invitation(sid)
        self.g.set_ready(sid, True)
        self.h.set_ready(sid, True)
        self.assertEqual(self.h.get_session(sid)['state'], m.READY)
        return sid

    def running(self):
        sid = self.lobby()
        self.h.start_session(sid)
        self.g.confirm_launch(sid)
        self.assertEqual(self.h.get_session(sid)['state'], m.RUNNING)
        self.assertNotEqual(self.nodes['host'].conf_text, self.original)
        self.assertIn(sid, self.h.guard.pending())
        return sid

    def test_disabling_host_adapter_after_ready_prevents_new_launch(self):
        sid = self.lobby()
        self.h.update_settings({'emulators': {'disabled_adapters': ['retroarch']}})
        self.assertFalse(self.h.adapter_detail('retroarch')['enabled'])
        with self.assertRaises(m.MultiplayerError):
            self.h.start_session(sid)
        self.assertEqual(self.nodes['host'].launcher.launched, [])
        self.assertEqual(self.nodes['host'].conf_text, self.original)
        self.assertEqual(self.h.guard.pending(), {})

    def test_exit_failure_does_not_skip_configuration_restoration(self):
        sid = self.running()
        # Shutdown and file restoration are independent obligations. A surfaced
        # shutdown error is allowed; skipping the remaining cleanup is not.
        with mock.patch.object(self.nodes['host'].launcher, 'exit_game',
                               side_effect=OSError('emulator shutdown unavailable')):
            try:
                self.h.end_session(sid)
            except (OSError, m.MultiplayerError):
                pass
        self.assertEqual(self.nodes['host'].conf_text, self.original,
                         'process shutdown failure must not leave temporary netplay settings')
        self.assertEqual(self.h.guard.pending(), {})

    def test_transient_restore_failure_is_visible_and_journal_is_retained(self):
        sid = self.running()
        with mock.patch.object(self.h.guard, '_atomic_write',
                               side_effect=OSError('temporary storage unavailable')):
            self.h.end_session(sid)
        self.assertIn(sid, self.h.guard.pending())
        self.assertNotEqual(self.nodes['host'].conf_text, self.original)
        view = self.h.get_session(sid)
        self.assertTrue(any('temporary storage unavailable' in event['message']
                            for event in view['events']), 'restore failure must be actionable')
        # Existing manual recovery must still work after the storage recovers.
        self.assertEqual(self.h.restore_configuration(sid)['errors'], [])
        self.assertEqual(self.nodes['host'].conf_text, self.original)

    def test_housekeeping_recovers_a_transient_end_restore_failure(self):
        sid = self.running()
        with mock.patch.object(self.h.guard, '_atomic_write',
                               side_effect=OSError('temporary storage unavailable')):
            self.h.end_session(sid)
        self.assertIn(sid, self.h.guard.pending())
        # Storage is healthy again. Permit multiple bounded housekeeping sweeps;
        # users should not need to restart Drone to restore abandoned settings.
        from datetime import timedelta
        for _ in range(6):
            self.clock += timedelta(seconds=30)
            self.h.tick()
        self.assertEqual(self.nodes['host'].conf_text, self.original,
                         'ended-session journal must be retried after transient restore errors')
        self.assertEqual(self.h.guard.pending(), {})


if __name__ == '__main__':
    unittest.main()
