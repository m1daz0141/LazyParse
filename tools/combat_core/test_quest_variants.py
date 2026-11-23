"""Scoped Extreme/Story boundaries, plus the full private live recording."""
from dataclasses import replace
from pathlib import Path
import unittest

from .quests import QuestSession, SUPPORTED_CLASSIC
from .quest_replay import replay_quests
from .test_quests import fixture, level, notice, transfer, campship_exit
from .test_combat_core import events, packet


def extreme_clear(sequence=4, qpc=800):
    return [notice(sequence + i, qpc + i, 0xb, minor, size)
            for i, (minor, size) in enumerate([(0x26, 24), (0x4e, 12), (0x44, 32), (0x11, 72)])]


def ids(quest, tail):
    return fixture()[0][:2] + [level(3, 100, 17, quest)] + tail


def session(start=20, source=SUPPORTED_CLASSIC):
    return QuestSession('fixture', start, 1000, source)


class ScopedQuestTests(unittest.TestCase):
    def test_extreme_stage_orders_continue_and_full_clear_freezes_for_both_quests(self):
        for quest in (1025, 1027):
            with self.subTest(quest=quest):
                metadata = ids(quest, [notice(4, 300, 0xb, 0x3f, 12), notice(5, 301, 0xb, 3, 28),
                    notice(6, 302, 0xb, 0x41, 16), transfer(7, 400)])
                s = session()
                s.update(metadata, events([(1, 200, packet(value=100))]), 700)
                self.assertIsNone(s.view()['current']['quest_measurement']['end_qpc'])
                metadata += extreme_clear(8) + [notice(12, 1200, 0xb, 0x14, 1856)]
                s.update(metadata, events([(2, 900, packet(value=3, attack=0))]), 1400)
                r = s.view()['current']
                self.assertEqual(r['roles']['self']['damage_dealt'], 100)
                self.assertEqual(r['quest_measurement']['end_reason'], 'clear_candidate_with_result')
                self.assertEqual(r['quest_measurement']['clear_notice_sequences'], [8, 9, 10, 11])
                self.assertEqual(r['quest_measurement']['clear_pair_sequences'], [8, 11])
                self.assertEqual(r['timing']['elapsed_seconds'], .603)
                self.assertEqual(s.view()['outside_records'], 1)
                s.update(metadata, [], 1800)
                self.assertEqual(s.view()['current'], r)

    def test_extreme_clear_rejects_gaps_and_timeout(self):
        good = extreme_clear()
        cases = [(1025, good[:2] + [replace(good[2], sequence=8)] + [replace(good[3], sequence=9)]),
            (1025, good[:3] + [replace(good[3], qpc=900)]),
            (1025, good[:3] + [replace(good[3], qpc=700)]),
            (1025, good[:3]), (1027, good[1:])]
        for quest, tail in cases:
            with self.subTest(quest=quest, tail=tail):
                s = session()
                s.update(ids(quest, tail), events([(1, 200, packet(value=100))]), 1200)
                self.assertIsNone(s.view()['current']['quest_measurement']['end_qpc'])

    def test_pair_clears_with_any_0b11_size(self):
        for quest, size, expected in [(939, 24, True), (475, 24, True), (1025, 24, True),
                                       (939, 48, True), (475, 48, True), (475, 36, True),
                                       (475, 32, True), (1021, 72, True)]:
            with self.subTest(quest=quest, size=size):
                s = session()
                s.update(ids(quest, [notice(4, 800, 0xb, 0x26, 24), notice(5, 801, 0xb, 0x11, size)]),
                         events([(1, 200, packet(value=100))]), 1000)
                self.assertEqual(s.view()['current']['quest_measurement']['end_qpc'], 801 if expected else None)

    def test_extreme_failure_needs_full_return_and_preceding_failure_not_just_death(self):
        for quest in (1025, 1027):
            with self.subTest(quest=quest):
                metadata = ids(quest, [notice(4, 500, 0xb, 0x27, 48)])
                s = session()
                s.update(metadata, events([(1, 200, packet(value=100))]), 700)
                self.assertIsNone(s.view()['current']['quest_measurement']['end_qpc'])
                metadata += [transfer(5, 800), campship_exit(6, 810), notice(7, 820, 0xb, 0x21, 12)]
                s.update(metadata, [], 1200)
                q = s.view()['current']['quest_measurement']
                self.assertEqual(q['end_reason'], 'failure_return_candidate')
                self.assertEqual(q['end_qpc'], 820)
                self.assertEqual(q['campship_return_sequences'], [4, 5, 6, 7])
        for quest, failure in [(1025, []), (1027, []), (475, [notice(4, 500, 0xb, 0x27, 48)]),
                               (1025, [notice(4, 500, 0xb, 0x27, 120)]),
                               (1025, [notice(4, -40000, 0xb, 0x27, 48)])]:
            s = session()
            s.update(ids(quest, failure + [transfer(5, 800), campship_exit(6, 810), notice(7, 820, 0xb, 0x21, 12)]),
                     events([(1, 200, packet(value=100))]), 1200)
            self.assertIsNone(s.view()['current']['quest_measurement']['end_qpc'])

    def test_story_failure120_does_not_leak_to_other_quests_or_restart_same_world(self):
        for quest, size, expected in [(939, 120, True), (939, 48, False), (475, 120, False),
                                       (1025, 120, False), (1132, 120, False)]:
            with self.subTest(quest=quest, size=size):
                s = session()
                metadata = ids(quest, [notice(4, 800, 0xb, 0x27, size), transfer(5, 1000),
                                      notice(6, 1010, 0xb, 6, 104), level(7, 1100, 17, quest)])
                s.update(metadata, events([(1, 200, packet(value=100))]), 1200)
                q = s.view()['current']['quest_measurement']
                self.assertEqual(q['end_qpc'], 800 if expected else None)
                if expected:
                    self.assertEqual(q['end_reason'], 'failure_candidate')
                    self.assertEqual(q['failure_notice_sequence'], 4)
                    metadata += [level(8, 1500, 18, quest)]
                    s.update(metadata, [], 1500)
                    self.assertEqual(s.view()['current']['counts']['records'], 0)

    def test_clear_and_return_state_straddle_start_and_terminal_state_is_seeded(self):
        for quest, tail, end in [(1025, extreme_clear(), 803),
            (939, [notice(4, 800, 0xb, 0x26, 24), notice(5, 801, 0xb, 0x11, 24)], 801),
            (1027, [notice(4, 500, 0xb, 0x27, 48), transfer(5, 800), campship_exit(6, 810),
                    notice(7, 820, 0xb, 0x21, 12)], 820)]:
            metadata = ids(quest, tail)
            for start in [tail[0].qpc, end - 1]:
                with self.subTest(quest=quest, start=start):
                    s = session(start)
                    s.update(metadata, [], 1000)
                    self.assertEqual(s.view()['current']['quest_measurement']['end_qpc'], end)
            s = session(1100)
            s.update(metadata + [level(20, 1200, 17, quest)], [], 1300)
            self.assertIsNone(s.view()['current'])
        s = session(1000)
        s.update(ids(939, [notice(4, 800, 0xb, 0x27, 120), level(5, 1100, 17, 939)]), [], 1200)
        self.assertIsNone(s.view()['current'])

    def test_incremental_late_hits_gaps_and_new_world_reset_partial_sequences(self):
        for quest, tail in [(1025, extreme_clear()), (939, [notice(4, 800, 0xb, 0x26, 24),
                            notice(5, 801, 0xb, 0x11, 24)])]:
            metadata = ids(quest, tail) + [level(20, 1500, 18, quest)]
            damage = events([(1, 200, packet(value=100)), (2, 700, packet(value=50)),
                             (3, 1000, packet(value=3, attack=0)), (4, 1600, packet(value=80))])
            direct, staged = session(), session()
            direct.update(metadata, damage, 1800, end_qpc=1800)
            staged.update(metadata[:4], damage[:1], 800)
            staged.update(metadata, [], 1800)
            staged.update(metadata, damage[1:], 1800, end_qpc=1800)
            self.assertEqual(direct.view(), staged.view())
            self.assertEqual(direct.view()['all_records'], 4)
            self.assertEqual(direct.view()['outside_records'], 1)
        s = session()
        s.update(ids(1025, [notice(4, 800, 0xb, 0x26, 24), notice(5, 801, 0xff, 1),
                 *extreme_clear(6, 802)[1:]]), events([(1, 200, packet(value=100))]), 1000,
                 flags=['binding_gap'])
        self.assertEqual(s.view()['current']['quest_measurement']['end_reason'], 'observation_gap')
        # Failure evidence from an old world cannot authorize a fresh world's return.
        s = session()
        s.update(ids(1025, [notice(4, 500, 0xb, 0x27, 48), level(5, 600, 18, 1025),
            transfer(6, 800), campship_exit(7, 810), notice(8, 820, 0xb, 0x21, 12)]),
            events([(1, 700, packet(value=100))]), 1000)
        self.assertIsNone(s.view()['current']['quest_measurement']['end_qpc'])


CAPTURE = Path(__file__).resolve().parents[1] / 'native_capture/captures/classic-auto-fixture-001/run-100000000000000001-1-0/segment-000001/capture.opcap'


@unittest.skipUnless(CAPTURE.exists(), 'private multi-quest lifecycle recording required')
class RealQuestVariantTests(unittest.TestCase):
    def test_all_5633_records_preserved_with_observed_ends_and_post_clear_values_outside(self):
        view = replay_quests(CAPTURE)['quest_view']
        quests = [q for q in view['history'] if q['quest_measurement']['kind'] == 'quest']
        self.assertEqual(view['all_records'], 5633)
        self.assertEqual(view['outside_records'], 4)
        expected = [(1130, 36452, 2227169, 69.4866837, 'failure_return_candidate'),
            (1130, 36469, 0, 0, 'new_world'),
            (1025, 36473, 40996380, 376.9989922, 'clear_candidate_with_result'),
            (1027, 36514, 62082822, 396.8790624, 'clear_candidate_with_result'),
            (939, 36557, 155000, 19.1480595, 'failure_candidate'),
            (939, 36577, 6259645, 66.386818, 'clear_candidate_with_result'),
            (1025, 36628, 1401893, 25.8266712, 'failure_return_candidate'),
            (1027, 36641, 899927, 25.5148977, 'failure_return_candidate')]
        actual = [(q['quest_measurement']['quest_candidate'], q['quest_measurement']['world_candidate'],
                   q.get('roles', {}).get('self', {}).get('damage_dealt', 0), q['timing']['elapsed_seconds'],
                   q['quest_measurement']['end_reason']) for q in quests]
        self.assertEqual(actual, expected)


if __name__ == '__main__':
    unittest.main()
