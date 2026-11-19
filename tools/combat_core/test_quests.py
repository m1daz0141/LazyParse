import json
from dataclasses import replace
from pathlib import Path
import struct
import shutil
import tempfile
import unittest

from .metadata import IdentityEvent
from .quests import QuestSession, SUPPORTED_CLASSIC, SUPPORTED_CLASSICS
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


def transfer(sequence, qpc):
    data = bytearray(84)
    struct.pack_into('<I4B', data, 0, 84, 0, 3, 0, 0)
    for offset, object_id, kind in [(8, 55, 5), (20, SELF.object_id, 4)]:
        struct.pack_into('<IIHH', data, offset, object_id, 0, kind, 0)
    return IdentityEvent(sequence, 0, qpc * 10000, qpc, 1000, 84, bytes(data))


def campship_exit(sequence, qpc, name=b'oa_campship_exit'):
    data = bytearray(128)
    struct.pack_into('<I4B', data, 0, 168, 0, 8, 0xb, 0)
    struct.pack_into('<IIHH', data, 8, 3, 0, 6, 17)
    data[0x24:0x24 + len(name)] = name
    return IdentityEvent(sequence, 0, qpc * 10000, qpc, 1000, 168, bytes(data))


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

    def test_maintenance_profile_preserves_boundaries_totals_and_rejects_unknown_build(self):
        identities, damage = fixture()
        expected = None
        for source in sorted(SUPPORTED_CLASSICS):
            with self.subTest(source=source):
                session = self.session(source=source)
                session.update(identities, damage, 3500, end_qpc=3500)
                view = session.view()
                self.assertFalse(view['suspended'])
                quests = [r for r in view['history'] if r['quest_measurement']['kind'] == 'quest']
                actual = [(r['roles']['self']['damage_dealt'], r['timing']['elapsed_seconds'],
                           r['quest_measurement']['end_reason']) for r in quests]
                self.assertEqual(len(actual), 2)
                if expected is None:
                    expected = actual
                self.assertEqual(actual, expected)
        unsupported = self.session(source='b434e9e2' + '0' * 56)
        unsupported.update(identities, damage, 3500, end_qpc=3500)
        self.assertEqual(unsupported.view()['outside_records'], len(damage))
        self.assertIsNone(unsupported.view()['current'])

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

    def test_world_notice_after_relogin_gap_resumes_automatic_quests(self):
        identities, _ = fixture()
        # Clear quest 1, lose the binding (title screen), log in again: lobby
        # then a new quest, all within one recording.
        # The binding reset invalidates identities; the game re-announces them.
        again = [IdentityEvent(sequence, 0, qpc * 10000, qpc, 1000, 20, identity_packet(major, minor, ref))
                 for sequence, qpc, (major, minor, ref) in [(11, 2450, (6, 0, SELF)), (13, 2550, (8, 0xd, ENEMY))]]
        identities = identities[:8] + [notice(9, 2200, 0xff, 1), level(10, 2400, 0, 504), again[0],
                                       level(12, 2500, 19), again[1],
                                       notice(14, 2900, 0xb, 0x26, 24), notice(15, 2901, 0xb, 0x11, 48)]
        damage = events([(1, 150, packet(value=100)), (2, 2300, packet(value=7)),
                         (3, 2600, packet(value=80)), (4, 2700, packet(value=20))])
        flags = ['binding_gap', 'identity_context_binding_reset']
        direct = self.session()
        direct.update(identities, damage, 3500, end_qpc=3500, flags=flags)
        staged = self.session()
        staged.update(identities[:9], damage[:2], 2350, flags=flags)
        self.assertTrue(staged.view()['suspended'])
        staged.update(identities, damage[2:], 3500, end_qpc=3500, flags=flags)
        self.assertEqual(direct.view(), staged.view())
        view = direct.view()
        self.assertFalse(view['suspended'])
        quests = [r for r in view['history'] if r['quest_measurement']['kind'] == 'quest']
        self.assertEqual([r['quest_measurement']['world_candidate'] for r in quests], [17, 19])
        first, second = quests
        self.assertFalse(first['quest_measurement']['rules_suspended'])
        self.assertEqual(second['roles']['self']['damage_dealt'], 100)
        self.assertEqual(second['quest_measurement']['end_reason'], 'clear_candidate')
        self.assertFalse(second['quest_measurement']['rules_suspended'])
        self.assertIsNone(second['quest_measurement']['observation_gap_qpc'])
        gap = next(r for r in view['history'] if r['quest_measurement']['start_qpc'] == 2200)
        self.assertEqual((gap['quest_measurement']['kind'], gap['counts']['records']), ('outside_quest', 1))
        self.assertTrue(gap['quest_measurement']['rules_suspended'])
        self.assertEqual(view['all_records'], 4)

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

    def test_failure_return_freezes_only_on_return_and_retry_starts_a_new_clock(self):
        identities = fixture()[0][:2] + [level(3, 100, 17, 1132),
            notice(4, 500, 0xb, 0x13, 52)]  # Incapacitation is not a terminal notice.
        damage = events([(1, 200, packet(value=100)), (2, 450, packet(ENEMY, SELF, 10))])
        session = self.session()
        session.update(identities, damage, 700)
        current = session.view()['current']
        self.assertIsNone(current['quest_measurement']['end_qpc'])
        self.assertAlmostEqual(current['timing']['elapsed_seconds'], .5)
        identities += [notice(5, 800, 0xb, 0x21, 12)]
        session.update(identities, [], 800)
        self.assertIsNone(session.view()['current']['quest_measurement']['end_qpc'])
        identities += [notice(6, 801, 0xb, 0x27, 120), level(7, 1000, 17, 1132)]
        session.update(identities, [], 1200)
        first = session.view()['current']
        self.assertEqual(first['quest_measurement']['end_reason'], 'failure_return_candidate')
        self.assertEqual(first['quest_measurement']['failure_return_pair_sequences'], [5, 6])
        self.assertEqual(first['quest_measurement']['end_qpc'], 801)
        self.assertAlmostEqual(first['timing']['elapsed_seconds'], .601)
        session.update(identities, [], 1500)
        self.assertEqual(session.view()['current'], first)
        identities += [level(8, 1800, 18, 1132)]
        session.update(identities, [], 1800)
        self.assertEqual(session.view()['current']['counts']['records'], 0)
        self.assertEqual(session.view()['current']['timing']['elapsed_seconds'], 0)
        session.update(identities, events([(3, 1100, packet(value=7)),
            (4, 1900, packet(value=80))]), 2200)
        view = session.view()
        self.assertEqual(view['all_records'], 4)
        self.assertEqual(view['outside_records'], 1)
        self.assertEqual(view['current']['roles']['self']['damage_dealt'], 80)
        self.assertAlmostEqual(view['current']['timing']['elapsed_seconds'], .3)
        self.assertEqual(view['history'][0], first)

    def test_failure_return_requires_observed_quest_sizes_adjacency_order_and_time(self):
        for bad_prefix, bad_suffix, quest in [
            (notice(4, 800, 0xb, 0x21, 16), notice(5, 801, 0xb, 0x27, 120), 1132),
            (notice(4, 800, 0xb, 0x21, 12), notice(5, 801, 0xb, 0x27, 124), 1132),
            (notice(4, 800, 0xb, 0x21, 12), notice(6, 801, 0xb, 0x27, 120), 1132),
            (notice(4, 800, 0xb, 0x21, 12), notice(5, 900, 0xb, 0x27, 120), 1132),
            (notice(4, 800, 0xb, 0x27, 120), notice(5, 801, 0xb, 0x21, 12), 1132),
            (notice(4, 800, 0xb, 0x21, 12), notice(5, 801, 0xb, 0x27, 120), 475),
        ]:
            with self.subTest(prefix=bad_prefix, suffix=bad_suffix, quest=quest):
                session = self.session()
                identities = fixture()[0][:2] + [level(3, 100, 17, quest), bad_prefix, bad_suffix]
                session.update(identities, events([(1, 200, packet(value=100))]), 1200)
                self.assertIsNone(session.view()['current']['quest_measurement']['end_qpc'])

    def test_start_after_failure_return_keeps_same_world_outside_until_new_attempt(self):
        identities = fixture()[0][:2] + [level(3, 100, 17, 1132),
            notice(4, 800, 0xb, 0x21, 12), notice(5, 801, 0xb, 0x27, 120),
            level(6, 1200, 17, 1132), level(7, 1800, 18, 1132)]
        session = self.session(start=900)
        session.update(identities, events([(1, 1300, packet(value=7)),
            (2, 1900, packet(value=80))]), 2200)
        self.assertEqual(session.view()['outside_records'], 1)
        self.assertEqual(session.view()['current']['quest_measurement']['world_candidate'], 18)
        self.assertEqual(session.view()['current']['roles']['self']['damage_dealt'], 80)
        # Loading the same world again before record start must not undo the
        # already observed terminal state when reconstructing the journal.
        later = self.session(start=1500)
        later.update(identities, [], 1700)
        self.assertIsNone(later.view()['current'])
        later.update(identities, events([(1, 1900, packet(value=80))]), 2200)
        self.assertEqual(later.view()['current']['roles']['self']['damage_dealt'], 80)
        # The prefix can precede record start while the return itself follows it.
        partial = self.session(start=800)
        partial.update(identities[:5], [], 1000)
        self.assertEqual(partial.view()['current']['quest_measurement']['end_qpc'], 801)

    def test_failure_return_incremental_and_late_damage_match_direct_replay(self):
        identities = fixture()[0][:2] + [level(3, 100, 17, 1132),
            notice(4, 800, 0xb, 0x21, 12), notice(5, 801, 0xb, 0x27, 120),
            level(6, 1800, 18, 1132)]
        damage = events([(1, 200, packet(value=100)), (2, 700, packet(value=50)),
            (3, 1000, packet(value=7)), (4, 1900, packet(value=80))])
        direct = self.session()
        direct.update(identities, damage, 2200, end_qpc=2200)
        staged = self.session()
        staged.update(identities[:4], damage[:1], 800)
        staged.update(identities, [], 2200)
        staged.view()
        staged.update(identities, damage[1:], 2200, end_qpc=2200)
        self.assertEqual(staged.view(), direct.view())

    def test_quest1130_campship_return_freezes_and_new_world_restarts(self):
        identities = fixture()[0][:2] + [level(3, 100, 17, 1130),
            notice(4, 400, 0xb, 0x13, 52), transfer(5, 500), campship_exit(6, 510)]
        damage = events([(1, 200, packet(value=100))])
        session = self.session()
        session.update(identities, damage, 515)
        self.assertIsNone(session.view()['current']['quest_measurement']['end_qpc'])
        identities += [notice(7, 520, 0xb, 0x21, 12), level(8, 800, 17, 1130)]
        session.update(identities, [], 900)
        first = session.view()['current']
        self.assertEqual(first['quest_measurement']['end_reason'], 'failure_return_candidate')
        self.assertEqual(first['quest_measurement']['campship_return_sequences'], [5, 6, 7])
        self.assertIsNone(first['quest_measurement']['failure_return_pair_sequences'])
        self.assertAlmostEqual(first['timing']['elapsed_seconds'], .32)
        session.update(identities, [], 1200)
        self.assertEqual(session.view()['current'], first)
        identities += [level(9, 1500, 18, 1130)]
        session.update(identities, [], 1500)
        self.assertEqual(session.view()['current']['counts']['records'], 0)
        session.update(identities, events([(2, 700, packet(value=7)),
            (3, 1600, packet(value=80))]), 1800)
        self.assertEqual(session.view()['outside_records'], 1)
        self.assertEqual(session.view()['current']['roles']['self']['damage_dealt'], 80)

    def test_quest1130_return_rejects_unrelated_incomplete_or_stale_evidence(self):
        cases = [
            (475, [transfer(4, 500), campship_exit(5, 510), notice(6, 520, 0xb, 0x21, 12)]),
            (1130, [campship_exit(5, 510), notice(6, 520, 0xb, 0x21, 12)]),
            (1130, [transfer(4, 500), notice(6, 520, 0xb, 0x21, 12)]),
            (1130, [transfer(4, 500), campship_exit(5, 510, b'oa_other_exit'), notice(6, 520, 0xb, 0x21, 12)]),
            (1130, [transfer(4, 500), campship_exit(5, 510), notice(6, 1520, 0xb, 0x21, 12)]),
            (1130, [transfer(4, 500), campship_exit(5, 510), notice(6, 520, 0xb, 0x21, 16)]),
            (1130, [transfer(4, 500), campship_exit(5, 510), transfer(6, 515), notice(7, 520, 0xb, 0x21, 12)]),
        ]
        for quest, tail in cases:
            with self.subTest(quest=quest, tail=tail):
                session = self.session()
                session.update(fixture()[0][:2] + [level(3, 100, 17, quest)] + tail,
                               events([(1, 200, packet(value=100))]), 2000)
                self.assertIsNone(session.view()['current']['quest_measurement']['end_qpc'])

    def test_quest1130_return_state_spans_record_start_and_preserves_late_damage(self):
        identities = fixture()[0][:2] + [level(3, 100, 17, 1130),
            transfer(4, 500), campship_exit(5, 510), notice(6, 520, 0xb, 0x21, 12),
            level(7, 800, 17, 1130), level(8, 1500, 18, 1130)]
        partial = self.session(start=515)
        partial.update(identities, [], 1000)
        self.assertEqual(partial.view()['current']['quest_measurement']['end_qpc'], 520)
        late_start = self.session(start=1000)
        late_start.update(identities, [], 1200)
        self.assertIsNone(late_start.view()['current'])
        damage = events([(1, 200, packet(value=100)), (2, 450, packet(value=50)),
            (3, 700, packet(value=7)), (4, 1600, packet(value=80))])
        direct, staged = self.session(), self.session()
        direct.update(identities, damage, 1800, end_qpc=1800)
        staged.update(identities[:5], damage[:1], 515)
        staged.update(identities, [], 1800)
        staged.update(identities, damage[1:], 1800, end_qpc=1800)
        self.assertEqual(staged.view(), direct.view())

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
CAPTURE = LIVE_ROOT / 'run-100000000000000011-1-0/segment-000001/capture.opcap'
GAP_CAPTURE = LIVE_ROOT / 'run-100000000000000012-1-0/segment-000001/capture.opcap'
FAILURE_RETURN_CAPTURE = LIVE_ROOT / 'run-100000000000000013-1-0/segment-000001/capture.opcap'
MULTI_QUEST_CAPTURE = LIVE_ROOT / 'run-100000000000000014-1-0/segment-000001/capture.opcap'


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


@unittest.skipUnless(FAILURE_RETURN_CAPTURE.exists(), 'private failure-return recording required')
class FailureReturnLiveRegressionTests(PinnedLiveTest):
    def test_return_freezes_before_retry_and_preserves_all_records(self):
        self.pin(FAILURE_RETURN_CAPTURE)
        result = replay_quests(self.capture)
        snapshot = RuntimeMonitor(root=self.root).poll()
        self.assertEqual(snapshot['quest_view'], result['quest_view'])
        view = result['quest_view']
        quests = [r for r in view['history'] if r['quest_measurement']['kind'] == 'quest']
        self.assertEqual(view['all_records'], 592)
        self.assertEqual(view['outside_records'], 0)
        first, second = quests
        self.assertEqual(first['roles']['self']['damage_dealt'], 17170327)
        self.assertEqual(first['quest_measurement']['end_qpc'], 83983557155)
        self.assertEqual(first['quest_measurement']['end_reason'], 'failure_return_candidate')
        self.assertEqual(first['quest_measurement']['failure_return_pair_sequences'], [1478, 1479])
        self.assertEqual(first['timing']['elapsed_seconds'], 151.1925303)
        self.assertEqual(second['counts']['records'], 0)
        self.assertEqual(second['quest_measurement']['world_candidate'], 35675)


@unittest.skipUnless(MULTI_QUEST_CAPTURE.exists(), 'private multi-quest return recording required')
class MultiQuestReturnLiveRegressionTests(PinnedLiveTest):
    def test_quest1132_live_retest_and_quest1130_return_preserve_every_record(self):
        self.pin(MULTI_QUEST_CAPTURE)
        result = replay_quests(self.capture)
        self.assertEqual(RuntimeMonitor(root=self.root).poll()['quest_view'], result['quest_view'])
        view = result['quest_view']
        quests = [r for r in view['history'] if r['quest_measurement']['kind'] == 'quest']
        self.assertEqual(view['all_records'], 1309)
        self.assertEqual(view['outside_records'], 0)
        self.assertEqual([r['roles']['self']['damage_dealt'] for r in quests], [28749187, 9726784, 3198498])
        self.assertEqual([r['timing']['elapsed_seconds'] for r in quests], [153.7339784, 100.0775758, 131.1808567])
        self.assertEqual(quests[2]['quest_measurement']['campship_return_sequences'], [1614, 1619, 1630])
        self.assertEqual(quests[2]['quest_measurement']['end_reason'], 'failure_return_candidate')


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
        self.assertEqual(first['roles']['self']['damage_dealt'], 35180334)
        self.assertEqual(first['timing']['elapsed_seconds'], 503.6025999)
        self.assertFalse(first['quest_measurement']['rules_suspended'])
        self.assertEqual(second['quest_measurement']['end_reason'], 'observation_gap')
        self.assertEqual(second['roles']['self']['damage_dealt'], 30041098)
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
        self.assertEqual([r['roles']['self']['damage_dealt'] for r in quests], [36865099, 3139428])
        self.assertEqual(quests[0]['timing']['elapsed_seconds'], 673.7316882)
        self.assertEqual(quests[0]['quest_measurement']['result_qpc'], 282193638538)
        for index, report in enumerate(quests, 1):
            diagnostic = CAPTURE.parents[3] / 'quest-live-20261019-165142-5ae3b2ee' / f'attempt-{index}-candidate-report.json'
            if diagnostic.exists():
                expected = json.loads(diagnostic.read_text(encoding='utf-8'))
                expected_sum = expected.get('damage_totals', expected['counts'])['positive_sum']
                zero_sum = sum(a['damage_dealt'] for a in expected['attacks'] if a['attack_id'] == 0)
                self.assertEqual(report['damage_totals']['positive_sum'], expected_sum - zero_sum)
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
