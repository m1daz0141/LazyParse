import json
from dataclasses import replace
from pathlib import Path
import struct
import shutil
import tempfile
import unittest

from .metadata import IdentityEvent
from .quests import QuestSession, SUPPORTED_CLASSIC
from .quest_replay import replay_quests
from .live import RuntimeMonitor
from .test_combat_core import SELF, ENEMY, packet, identity_packet, events
from .test_live import RuntimeFixture
from ..gui.backend import Backend, saved_snapshot


def notice(sequence, qpc, major, minor, size=8):
    prefix = struct.pack('<I4B', size, 0, major, minor, 0)
    return IdentityEvent(sequence, 0, qpc * 10000, qpc, 1000, size, prefix)


def level(sequence, qpc, world, quest=475):
    data = bytearray(128)
    struct.pack_into('<I4B', data, 0, 2656, 0, 3, 0x24, 0)
    for offset, value, kind in [(8, world + 10 if world else 0, 5), (20, SELF.object_id, 4),
                                 (84, world, 16), (96, quest, 11), (108, 99, 13)]:
        struct.pack_into('<IIHH', data, offset, value, 0, kind, 0)
    return IdentityEvent(sequence, 0, qpc * 10000, qpc, 1000, 2656, bytes(data))


def fixture():
    identities = []
    for major, minor, ref in [(6, 0, SELF), (8, 0xd, ENEMY)]:
        data = identity_packet(major, minor, ref)
        identities.append(IdentityEvent(len(identities) + 1, 0, 1, len(identities) + 1,
                                        1000, 20, data))
    identities += [level(3, 3, 0, 504), level(4, 100, 17), level(5, 400, 17),
                   notice(6, 999, 0xb, 0x26, 24), notice(7, 1000, 0xb, 0x11, 48),
                   notice(8, 2000, 0xb, 0x14, 1856), level(9, 2500, 18), level(10, 3000, 0, 504)]
    damage = events([(1, 150, packet(value=100)), (2, 200, packet(value=50)),
                     (3, 300, packet(value=-10)), (4, 1100, packet(value=1, attack=0)),
                     (5, 2600, packet(value=80)), (6, 3400, packet(ENEMY, SELF, 5))])
    return identities, damage


class QuestTests(unittest.TestCase):
    def session(self, start=20, source=SUPPORTED_CLASSIC):
        return QuestSession('fixture', start, 1000, source)

    def test_same_quest_repeat_clear_clock_roundtrip_duplicate_and_outside_retention(self):
        identities, damage = fixture()
        session = self.session()
        session.update(identities, damage, 3500, end_qpc=3500)
        view = session.view()
        self.assertEqual(view['all_records'], 6)
        self.assertEqual(view['outside_records'], 2)
        quests = [r for r in view['history'] if r['quest_measurement']['kind'] == 'quest']
        self.assertEqual(len(quests), 2)
        first, second = quests
        self.assertEqual([r['quest_measurement']['number'] for r in quests], [1, 2])
        self.assertEqual(first['roles']['self']['damage_dealt'], 150)
        self.assertEqual(first['counts']['negative_sum'], -10)
        self.assertAlmostEqual(first['timing']['elapsed_seconds'], .85)
        self.assertEqual(first['quest_measurement']['end_qpc'], 1000)
        self.assertEqual(first['quest_measurement']['result_qpc'], 2000)
        self.assertEqual(first['quest_measurement']['end_reason'], 'clear_candidate_with_result')
        self.assertEqual(second['roles']['self']['damage_dealt'], 80)
        self.assertEqual(second['quest_measurement']['end_reason'], 'left_quest_without_clear')

    def test_late_damage_after_new_world_and_clear_updates_original_bucket(self):
        identities, damage = fixture()
        delayed = self.session()
        delayed.update(identities, damage[:1], 3500)
        before = delayed.view()['history'][0]
        self.assertEqual(before['roles']['self']['damage_dealt'], 100)
        delayed.update(identities, damage[1:], 3500, end_qpc=3500)
        direct = self.session()
        direct.update(identities, damage, 3500, end_qpc=3500)
        self.assertEqual(delayed.view(), direct.view())

    def test_missing_clear_pair_does_not_freeze_at_delayed_results(self):
        identities, damage = fixture()
        identities = [e for e in identities if e.packet_id not in {(0xb, 0x26), (3, 0x24)} or e.sequence <= 4]
        session = self.session()
        session.update(identities, damage[:3], 2200)
        current = session.view()['current']
        self.assertIsNone(current['quest_measurement']['end_qpc'])
        self.assertAlmostEqual(current['timing']['elapsed_seconds'], 2.05)

    def test_unsupported_source_and_missing_identity_context_keep_manual_totals(self):
        identities, damage = fixture()
        session = self.session(source='unknown')
        session.update(identities, damage, 3500, end_qpc=3500)
        view = session.view()
        self.assertIsNone(view['current'])
        self.assertEqual(view['outside_records'], 6)
        session = self.session()
        session.update(identities, damage, 3500, end_qpc=3500, flags=['identity_context_dropped'])
        self.assertTrue(session.view()['suspended'])
        self.assertIsNone(session.view()['current'])

    def test_gap_after_start_suspends_boundaries_and_mixed_world_map_is_unknown(self):
        identities, damage = fixture()
        session = self.session()
        session.update(identities, damage[:3], 900)
        session.update(identities, [], 2200, flags=['binding_gap'])
        current = session.view()['current']
        self.assertTrue(current['quest_measurement']['rules_suspended'])
        self.assertIsNone(current['quest_measurement']['end_qpc'])
        mixed = level(1, 1, 17)
        prefix = bytearray(mixed.prefix)
        struct.pack_into('<I', prefix, 84, 0)
        mixed = IdentityEvent(1, 0, 1, 1, 1000, mixed.packet_size, bytes(prefix))
        self.assertIsNone(QuestSession.level(mixed))

    def test_timed_late_gap_preserves_earlier_clear_and_marks_only_affected_intervals(self):
        identities, damage = fixture()
        identities = identities[:9] + [notice(10, 3000, 0xff, 1)]
        flags = ['binding_gap', 'identity_context_binding_reset', 'zero_source_or_attack_id']
        direct = self.session()
        direct.update(identities, damage, 3500, end_qpc=3500, flags=flags)
        staged = self.session()
        staged.update(identities[:9], damage[:5], 2900, flags=['zero_source_or_attack_id'])
        staged.view()
        staged.update(identities, damage[5:], 3500, end_qpc=3500, flags=flags)
        self.assertEqual(direct.view(), staged.view())
        view = direct.view()
        quests = [r for r in view['history'] if r['quest_measurement']['kind'] == 'quest']
        self.assertEqual(len(quests), 2)
        self.assertTrue(view['suspended'])
        self.assertEqual(view['all_records'], 6)
        first, second = quests
        self.assertEqual(first['quest_measurement']['end_reason'], 'clear_candidate_with_result')
        self.assertFalse(first['quest_measurement']['rules_suspended'])
        self.assertNotIn('binding_gap', first['quality']['flags'])
        self.assertEqual(second['quest_measurement']['end_reason'], 'observation_gap')
        self.assertTrue(second['quest_measurement']['rules_suspended'])
        self.assertEqual(second['quest_measurement']['end_qpc'], 3000)
        self.assertIn('binding_gap', second['quality']['flags'])

    def test_gap_after_segment_end_does_not_invalidate_the_segment(self):
        identities, damage = fixture()
        identities += [notice(11, 3600, 0xff, 1)]
        session = self.session()
        session.update(identities, damage[:5], 4000, end_qpc=2900,
                       flags=['binding_gap', 'identity_context_binding_reset', 'zero_source_or_attack_id'])
        self.assertFalse(session.view()['suspended'])
        self.assertEqual(len([b for b in session.buckets if b.kind == 'quest']), 2)

    def test_start_mid_quest_or_after_clear_does_not_invent_prior_damage(self):
        identities, damage = fixture()
        session = self.session(start=180)
        session.update(identities, damage[1:3], 900)
        report = session.view()['current']
        self.assertTrue(report['quest_measurement']['started_mid_quest'])
        self.assertEqual(report['roles']['self']['damage_dealt'], 50)
        late = self.session(start=1500)
        late.update(identities, [], 2100)
        self.assertIsNone(late.view()['current'])

    def test_clear_pair_requires_size_sequence_and_time_and_duplicate_world_cannot_restart(self):
        identities, damage = fixture()
        for bad in (notice(7, 1000, 0xb, 0x11, 52), notice(17, 1000, 0xb, 0x11, 48),
                    notice(7, 1500, 0xb, 0x11, 48)):
            sequence = identities[:6] + [bad]
            session = self.session()
            session.update(sequence, damage[:3], 1800)
            self.assertIsNone(session.view()['current']['quest_measurement']['end_qpc'])
        session = self.session()
        session.update(identities[:7] + [level(8, 1500, 17)], damage[:4], 1800)
        self.assertEqual(len([b for b in session.buckets if b.kind == 'quest']), 1)

    def test_published_identity_ahead_of_damage_keeps_late_hits_in_the_old_quest(self):
        identities, damage = fixture()
        with tempfile.TemporaryDirectory() as work:
            runtime = RuntimeFixture(work)
            runtime.state['classic_hash_required'] = SUPPORTED_CLASSIC
            runtime.status['source_sha256'] = SUPPORTED_CLASSIC
            runtime.identities = [(e.sequence, e.qpc, e.prefix.ljust(e.packet_size, b'\0')) for e in identities]
            runtime.write_context(through=3500)
            segment = runtime.begin(1, start=20)
            runtime.ref['source_sha256'] = SUPPORTED_CLASSIC
            runtime.ref_path.write_text(json.dumps(runtime.ref))
            runtime.damage_status['source_sha256'] = SUPPORTED_CLASSIC
            rows = [(e.sequence, e.qpc, bytes.fromhex(e.packet_hex)) for e in damage]
            runtime.damage(rows[:1])
            monitor = runtime.monitor()
            first = monitor.poll()
            old = first['quest_view']['history'][0]
            self.assertEqual(old['roles']['self']['damage_dealt'], 100)
            runtime.damage(rows)
            runtime.stop(3500)
            last = monitor.poll()
            self.assertFalse(last['pending'])
            self.assertEqual(last['quest_view']['all_records'], 6)
            self.assertEqual(last['quest_view']['history'][0]['roles']['self']['damage_dealt'], 150)
            direct = QuestSession(str((segment / 'capture.opcap').resolve()), 20, 1000, SUPPORTED_CLASSIC)
            direct.update(monitor.journal.events, [replace(e, capture_id=direct.capture_id) for e in damage],
                          3500, end_qpc=3500, flags=last['measurement']['report']['quality']['flags'])
            self.assertEqual(last['quest_view'], direct.view())


LIVE_ROOT = Path(__file__).resolve().parents[1] / 'native_capture/captures/classic-auto-fixture-007'
CAPTURE = LIVE_ROOT / 'run-100000000000000007-1-0/segment-000001/capture.opcap'
GAP_CAPTURE = LIVE_ROOT / 'run-100000000000000008-1-0/segment-000001/capture.opcap'


class PinnedLiveTest(unittest.TestCase):
    def pin(self, capture):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name) / 'capture'
        source_run = capture.parent.parent
        run = self.root / source_run.name
        shutil.copytree(source_run, run)
        self.capture = run / capture.parent.name / capture.name
        ref = json.loads(self.capture.with_name('identity-context.json').read_text(encoding='utf-8'))
        state = {'control_abi': 1, 'state': 'shutdown', 'pid': ref['pid'],
                 'state_filetime': ref['context_start_filetime'], 'run_name': run.name,
                 'run_directory': str(run), 'capture_directory': str(self.capture.parent),
                 'segment_number': int(capture.parent.name[8:]), 'recording': False,
                 'identity_observing': False, 'classic_hash_required': ref['source_sha256']}
        (self.root / 'autostart-state.json').write_text(json.dumps(state), encoding='utf-8')


@unittest.skipUnless(GAP_CAPTURE.exists(), 'private GUI live run with shutdown binding loss required')
class LateGapLiveRegressionTests(PinnedLiveTest):
    def test_open_after_shutdown_preserves_clear_and_repeat_before_the_late_gap(self):
        self.pin(GAP_CAPTURE)
        replayed = replay_quests(self.capture)
        monitor = RuntimeMonitor(root=self.root)
        snapshot = monitor.poll()
        self.assertEqual(snapshot['run_name'], GAP_CAPTURE.parent.parent.name)
        view = snapshot['quest_view']
        self.assertEqual(view, replayed['quest_view'])
        self.assertEqual(view['all_records'], 4038)
        self.assertEqual(view['outside_records'], 3)
        quests = [r for r in view['history'] if r['quest_measurement']['kind'] == 'quest']
        self.assertEqual(len(quests), 2)
        first, second = quests
        self.assertEqual(first['roles']['self']['damage_dealt'], 35180343)
        self.assertEqual(first['timing']['elapsed_seconds'], 503.6025999)
        self.assertFalse(first['quest_measurement']['rules_suspended'])
        self.assertEqual(second['quest_measurement']['end_reason'], 'observation_gap')
        self.assertEqual(second['roles']['self']['damage_dealt'], 30041124)
        self.assertTrue(second['quest_measurement']['rules_suspended'])


@unittest.skipUnless(CAPTURE.exists(), 'private quest lifecycle recording required')
class LiveQuestRegressionTests(PinnedLiveTest):
    def setUp(self):
        self.pin(CAPTURE)

    def test_saved_live_run_matches_incremental_monitor_and_keeps_all_798_records(self):
        result = replay_quests(self.capture)
        monitor = RuntimeMonitor(root=self.root)
        view = monitor.poll()['quest_view']
        self.assertEqual(view, result['quest_view'])
        self.assertEqual(view['all_records'], 798)
        self.assertEqual(view['outside_records'], 2)
        self.assertTrue(all('sequence_gap' not in r['quality']['flags'] for r in view['history']))
        quests = [r for r in view['history'] if r['quest_measurement']['kind'] == 'quest']
        self.assertEqual([r['roles']['self']['damage_dealt'] for r in quests], [36865123, 3139428])
        self.assertEqual(quests[0]['timing']['elapsed_seconds'], 673.7316882)
        self.assertEqual(quests[0]['quest_measurement']['result_qpc'], 282193638538)
        for index, report in enumerate(quests, 1):
            diagnostic = CAPTURE.parents[3] / 'quest-live-20261019-165142-5ae3b2ee' / f'attempt-{index}-candidate-report.json'
            if diagnostic.exists():
                expected = json.loads(diagnostic.read_text(encoding='utf-8'))
                self.assertEqual(report['roles'], expected['roles'])
                self.assertEqual(report['counts'], expected['counts'])

    def test_gui_auto_save_is_idempotent_openable_and_never_sends_capture_commands(self):
        class NoControl:
            def invoke(self, *args):
                raise AssertionError('automatic aggregation must not issue capture commands')
        with tempfile.TemporaryDirectory() as work:
            backend = Backend(root=self.root, controller=NoControl(), log_directory=Path(work))
            first = backend._poll()
            second = backend._poll()
            self.assertEqual(first['measurement']['report'], second['measurement']['report'])
            self.assertEqual(len(list(Path(work).glob('quest-*.json'))), 3)
            for entry in second['quest_history']:
                self.assertEqual(saved_snapshot(entry['report_path'])['measurement']['report'], entry['report'])
            backend._execute('AutoOff', None, None)
            manual = backend._poll()
            self.assertEqual(manual['measurement']['report']['counts']['records'], 798)

    def test_save_failure_preserves_gui_data_and_retries_without_replacing_user_file(self):
        with tempfile.TemporaryDirectory() as work:
            destination = Path(work) / 'not-a-directory'
            destination.write_text('keep')
            backend = Backend(root=self.root, log_directory=destination)
            failed = backend._poll()
            self.assertTrue(failed['auto_save_error'])
            self.assertEqual(failed['measurement']['report']['counts']['records'], 46)
            self.assertEqual(destination.read_text(), 'keep')
            backend.log_directory = Path(work) / 'recovered'
            recovered = backend._poll()
            self.assertIsNone(recovered['auto_save_error'])
            self.assertEqual(len(list(backend.log_directory.glob('*.json'))), 3)

    def test_modified_archive_is_preserved_and_history_can_use_in_memory_report(self):
        with tempfile.TemporaryDirectory() as work:
            backend = Backend(root=self.root, log_directory=Path(work))
            first = backend._poll()
            path = Path(first['quest_history'][0]['report_path'])
            path.write_text('user changed this')
            second = backend._poll()
            self.assertEqual(path.read_text(), 'user changed this')
            self.assertTrue(second['auto_save_error'])
            self.assertIsNone(second['quest_history'][0]['report_path'])
            self.assertIn('actors', second['quest_history'][0]['report'])


if __name__ == '__main__':
    unittest.main()
