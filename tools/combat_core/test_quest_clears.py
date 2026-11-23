"""Shared success layouts stop before return without closing partial stages."""
from dataclasses import replace
from pathlib import Path
import struct
import unittest

from .quests import QuestSession, SUPPORTED_CLASSIC
from .quest_replay import replay_quests
from .test_quests import fixture, level, notice, transfer, campship_exit
from .test_combat_core import events, packet, ENEMY, SELF


def success(quest, sequence=4, qpc=800):
    # Literal notification shapes from the new successful recordings.
    shapes = {
        1130: [(11, 0x26, 24), (11, 0xed, 24), (11, 0xf0, 32), (11, 0x11, 24)],
        1132: [(11, 0x26, 24), (8, 0xb, 192), (11, 0xed, 24), (11, 0xf0, 20), (11, 0x11, 24)],
        1005: [(11, 0x26, 24), (8, 0xb, 192), (11, 0xf1, 20), (11, 0xcf, 32), (11, 0xfe, 44),
               (11, 0xff, 48), (11, 0xfc, 11680), (11, 0xf1, 20), (11, 0xf1, 20),
               (11, 0xcc, 24), (11, 0x11, 36)],
        1023: [(11, 0x26, 24), (11, 0x4e, 12), (11, 0x44, 32), (11, 0x11, 72)],
        1024: [(11, 0x26, 24), (11, 0x4e, 12), (11, 0x44, 32), (11, 0x11, 72)],
        # Boss defeat: observed at the defeating blow.
        1137: [(11, 0x26, 24), (8, 0xb, 192), (11, 0x11, 24)],
        1253: [(11, 0x26, 24), (8, 0xb, 192), (11, 0x11, 36)],
        467: [(11, 0x26, 24), (8, 0xb, 192), (11, 0x11, 84)],
    }
    if quest == 1006:
        shapes[1006] = [(major, minor, 11600 if (major, minor) == (11, 0xfc) else size)
                        for major, minor, size in shapes[1005]]
    result = []
    for i, (major, minor, size) in enumerate(shapes[quest]):
        event = notice(sequence + i, qpc + i, major, minor, size)
        if major == 8:
            data = bytearray(192)
            struct.pack_into('<I4B', data, 0, size, 0, major, minor, 0)
            struct.pack_into('<IIHH', data, 8, 73, 0, 6, 17)
            name = {1132: b'ob_3250_0023', 1253: b'ob_3250_0023', 1137: b'ob_2100_0011',
                    467: b'ob_2100_0011'}.get(quest, b'ob_3200_0041')
            data[0x24:0x24 + len(name)] = name
            event = replace(event, prefix=bytes(data))
        result.append(event)
    return result


def metadata(quest, tail):
    return fixture()[0][:2] + [level(3, 100, 17, quest)] + tail


class QuestSuccessTests(unittest.TestCase):
    def session(self, start=20):
        return QuestSession('fixture', start, 1000, SUPPORTED_CLASSIC)

    def test_masquerade_short_success_is_incremental_and_return_cannot_reclassify_it(self):
        tail = success(1130)
        tail[2] = replace(tail[2], packet_size=20)
        for quest in (1130, 9999):
            s = self.session()
            prefix = metadata(quest, [])
            damage = events([(1, 200, packet(value=100))])
            s.update(prefix + tail[:-1], damage, tail[-1].qpc - 1)
            self.assertIsNone(s.view()['current']['quest_measurement']['end_qpc'])
            s.update(prefix + tail, [], 1000)
            cleared = s.view()['current']
            self.assertEqual(cleared['quest_measurement']['end_reason'], 'clear_candidate')
            self.assertEqual(cleared['quest_measurement']['end_qpc'], tail[-1].qpc)
            self.assertEqual(cleared['quest_measurement']['clear_notice_sequences'], [4, 5, 6, 7])
            returned = prefix + tail + [transfer(8, 1100), campship_exit(9, 1110),
                notice(10, 1120, 11, 0x14, 1856), notice(11, 1121, 11, 0x21, 12)]
            s.update(returned, events([(2, 1001, packet(value=3, attack=0))]), 2000)
            final = s.view()['current']
            self.assertEqual(final['quest_measurement']['end_reason'], 'clear_candidate_with_result')
            self.assertIsNone(final['quest_measurement']['campship_return_sequences'])
            self.assertEqual(final['quest_measurement']['end_qpc'], tail[-1].qpc)
            self.assertEqual(final['timing']['elapsed_seconds'], cleared['timing']['elapsed_seconds'])
            self.assertEqual(final['timing']['dps'], cleared['timing']['dps'])
            self.assertEqual([span['end_qpc'] for span in final['timing']['spans']],
                             [span['end_qpc'] for span in cleared['timing']['spans']])
            self.assertEqual(final['roles']['self']['damage_dealt'], 100)
            self.assertEqual(s.view()['outside_records'], 1)
            direct = self.session()
            direct.update(returned, damage + events([(2, 1001, packet(value=3, attack=0))]), 2000)
            self.assertEqual(direct.view(), s.view())

    def test_masquerade_short_success_rejects_partial_reordered_delayed_and_unknown_size(self):
        good = success(1130)
        good[2] = replace(good[2], packet_size=20)
        cases = [good[:-1], good[:2] + good[3:], good[:1] + [good[2], good[1]] + good[3:],
            good[:-1] + [replace(good[-1], qpc=851)],
            good[:-1] + [replace(good[-1], sequence=99)],
            good[:2] + [replace(good[2], packet_size=24)] + good[3:]]
        for tail in cases:
            s = self.session()
            s.update(metadata(1130, tail), events([(1, 200, packet(value=100))]), 1000)
            self.assertIsNone(s.view()['current']['quest_measurement']['end_qpc'])

    def test_success_precedes_return_and_return_failure_pairs_cannot_close_twice(self):
        for quest in (1130, 1132, 1005, 1006, 1023, 1024, 1137, 1253, 467):
            with self.subTest(quest=quest):
                tail = success(quest)
                seq = tail[-1].sequence
                notices = metadata(quest, tail)
                s = self.session()
                s.update(notices, events([(1, 200, packet(value=100))]), 1000)
                first = s.view()['current']
                self.assertEqual(first['quest_measurement']['end_qpc'], tail[-1].qpc)
                self.assertEqual(first['quest_measurement']['end_reason'], 'clear_candidate')
                self.assertEqual(first['quest_measurement']['clear_notice_sequences'], [e.sequence for e in tail])
                notices += [transfer(seq+1, 1100), campship_exit(seq+2, 1110),
                            notice(seq+3, 1120, 11, 0x21, 12), notice(seq+4, 1121, 11, 0x27, 120),
                            notice(seq+5, 1200, 11, 0x14, 1856)]
                s.update(notices, events([(2, 1001, packet(value=3, attack=0))]), 2000)
                final = s.view()['current']
                self.assertEqual(final['quest_measurement']['end_qpc'], first['quest_measurement']['end_qpc'])
                self.assertEqual(final['quest_measurement']['end_reason'], 'clear_candidate_with_result')
                self.assertIsNone(final['quest_measurement']['failure_return_pair_sequences'])
                self.assertIsNone(final['quest_measurement']['campship_return_sequences'])
                self.assertEqual(final['timing']['elapsed_seconds'], first['timing']['elapsed_seconds'])
                self.assertEqual(final['timing']['dps'], first['timing']['dps'])
                self.assertEqual([span['end_qpc'] for span in final['timing']['spans']],
                                 [span['end_qpc'] for span in first['timing']['spans']])
                self.assertEqual(final['roles']['self']['damage_dealt'], 100)
                self.assertEqual(s.view()['outside_records'], 1)
                s.update(notices, [], 3000)
                self.assertEqual(s.view()['current'], final)
                self.assertEqual(len([r for r in s.view()['history'] if r['quest_measurement']['kind']=='quest']), 1)

    def test_complete_success_layouts_work_without_registered_quest_or_object_names(self):
        for pattern_quest in (1130, 1132, 1005, 1006, 1023, 1024, 1137, 1253, 467):
            for other in (475, 992, 970, 980, 988, 1047):
                with self.subTest(pattern=pattern_quest, other=other):
                    s = self.session()
                    tail = success(pattern_quest)
                    if tail[1].packet_id == (8, 0xb):
                        data = bytearray(tail[1].prefix)
                        data[0x24:0x24+32] = bytes(32)
                        name = b'ob_9999_0001'
                        data[0x24:0x24+len(name)] = name
                        tail[1] = replace(tail[1], prefix=bytes(data))
                    s.update(metadata(other, tail), events([(1, 200, packet(value=100))]), 1000)
                    q = s.view()['current']['quest_measurement']
                    self.assertEqual(q['end_qpc'], tail[-1].qpc)
                    self.assertEqual(q['end_reason'], 'clear_candidate')

    def test_divide_intermediate_sequence_is_not_treated_as_final_clear(self):
        # Observed stage 992: its 26/11 pair encloses a different sequence
        # from the final stage. Do not skip arbitrary packets between them.
        shapes = [(11, 0x26, 24), (8, 0xb, 192), (11, 0xf1, 20), (11, 0xcf, 32),
                  (11, 0xf1, 20), (11, 0xfe, 44), (11, 0xfc, 11680),
                  (11, 0xf1, 20), (11, 0xf1, 20), (11, 0x11, 24)]
        tail = [notice(4+i, 800+i, major, minor, size)
                for i, (major, minor, size) in enumerate(shapes)]
        tail[1] = success(1005)[1]  # Valid object; differing sequence must reject.
        for quest in (992, 970, 980, 988, 475):
            s = self.session()
            s.update(metadata(quest, tail), events([(1, 200, packet(value=100))]), 1000)
            self.assertIsNone(s.view()['current']['quest_measurement']['end_qpc'])

    def test_observed_step_26_to_29_short_results_do_not_close_the_stage(self):
        # The same 11600-byte packet appears in intermediate results. The
        # final sequence additionally contains ff/48 and cc/24 in order.
        for quest, extra, end_size, count in ((972, False, 36, 3),
                (999, True, 24, 2), (966, True, 24, 2), (946, False, 24, 2)):
            shapes = [(11, 0x26, 24), (8, 0xb, 192), (11, 0xf1, 20), (11, 0xcf, 32)]
            if extra:
                shapes.append((11, 0xf1, 20))
            shapes += [(11, 0xfe, 44), (11, 0xfc, 11600)] + [(11, 0xf1, 20)] * count
            shapes.append((11, 0x11, end_size))
            tail = [notice(4+i, 800+i, major, minor, size)
                    for i, (major, minor, size) in enumerate(shapes)]
            tail[1] = success(1006)[1]
            s = self.session()
            s.update(metadata(quest, tail), events([(1, 200, packet(value=100))]), 1000)
            self.assertIsNone(s.view()['current']['quest_measurement']['end_qpc'])

    def test_rejects_incomplete_wrong_size_gaps_order_time_and_wrong_objects(self):
        for quest in (1130, 1132, 1005, 1006):
            good = success(quest)
            cases = [good[:-1], good[:1]+good[2:],
                     good[:-1]+[replace(good[-1], packet_size=good[-1].packet_size+4)],
                     good[:-1]+[replace(good[-1], sequence=99)],
                     good[:-1]+[replace(good[-1], qpc=851)],
                     good[:1]+[good[2],good[1]]+good[3:],
                     good[:1]+[notice(5, 801, 4, 6, 32)]+good[1:]]
            if quest != 1130:
                wrong = bytearray(good[1].prefix)
                struct.pack_into('<H', wrong, 16, 4)  # Object must still have type 6.
                cases.append(good[:1]+[replace(good[1],prefix=bytes(wrong))]+good[2:])
                empty = bytearray(good[1].prefix); empty[0x24:] = bytes(len(empty)-0x24)
                cases.append(good[:1]+[replace(good[1],prefix=bytes(empty))]+good[2:])
                cases.append(good[:1]+[replace(good[1],prefix=good[1].prefix[:8])]+good[2:])
            if quest in (1005, 1006):
                # The 0b:fc size is per step; everything else stays exact.
                cases += [good[:5]+good[6:], good[:9]+good[10:],
                          good[:5]+[replace(good[5], packet_size=52)]+good[6:],
                          good[:9]+[replace(good[9], packet_size=28)]+good[10:]]
            for tail in cases:
                with self.subTest(quest=quest, sequences=[e.sequence for e in tail]):
                    s = self.session()
                    s.update(metadata(quest, tail), events([(1, 200, packet(value=100))]), 1000)
                    self.assertIsNone(s.view()['current']['quest_measurement']['end_qpc'])

    def test_divide_final_any_fc_size_ends_time_at_the_defeating_blow(self):
        for size in (11332, 11600, 11680, 11604):
            with self.subTest(size=size):
                tail = success(1006)
                tail = tail[:6] + [replace(tail[6], packet_size=size)] + tail[7:]
                damage = events([(1, 200, packet(value=100)), (2, 500, packet(value=50)),
                                 (3, 700, packet(ENEMY, SELF, 5))])  # Enemy hit after the blow.
                s = self.session()
                s.update(metadata(1006, tail), damage, 1000)
                quest = next(r for r in s.view()['history'] if r['quest_measurement']['kind'] == 'quest')
                m = quest['quest_measurement']
                self.assertEqual((m['end_reason'], m['end_qpc']), ('clear_candidate', tail[-1].qpc))
                self.assertEqual((m['time_end_qpc'], m['time_end_evidence']),
                                 (500, 'last_player_hit_before_divide_final_clear'))
                self.assertAlmostEqual(quest['timing']['elapsed_seconds'], .3)
                self.assertEqual(quest['roles']['self']['damage_dealt'], 150)
                self.assertEqual(quest['counts']['records'], 3)
        # Other success layouts keep the clear notice as the end of time.
        s = self.session()
        s.update(metadata(1130, success(1130)), events([(1, 200, packet(value=100)), (2, 500, packet(value=50))]), 1000)
        m = next(r for r in s.view()['history'] if r['quest_measurement']['kind'] == 'quest')['quest_measurement']
        self.assertIsNone(m['time_end_qpc'])

    def test_start_straddles_sequence_late_hits_and_next_quest_are_consistent(self):
        for quest in (1130, 1132, 1005, 1006):
            tail = success(quest)
            all_meta = metadata(quest, tail)
            for start in (tail[0].qpc, tail[-1].qpc-1):
                s = self.session(start)
                s.update(all_meta, [], 1000)
                self.assertEqual(s.view()['current']['quest_measurement']['end_qpc'], tail[-1].qpc)
            s = self.session(1000)
            s.update(all_meta+[level(30, 1200, 17, quest)], [], 1300)
            self.assertIsNone(s.view()['current'])  # Same completed world never reopens.
            all_meta += [level(30, 1500, 18, quest)]
            damage = events([(1, 200, packet(value=100)), (2, 700, packet(value=50)),
                             (3, 1000, packet(value=3, attack=0)), (4, 1600, packet(value=80))])
            direct, late = self.session(), self.session()
            direct.update(all_meta, damage, 1800, end_qpc=1800)
            late.update(all_meta[:4], damage[:1], 800)
            late.update(all_meta, [], 1800)
            late.update(all_meta, damage[1:], 1800, end_qpc=1800)
            self.assertEqual(direct.view(), late.view())
            self.assertEqual(direct.view()['all_records'], 4)
            self.assertEqual(direct.view()['outside_records'], 1)


RUN = Path(__file__).resolve().parents[1] / 'native_capture/captures/classic-auto-247ae3146ebb4d6994fc1961a7e8e9f4/run-134369825290611262-2444-0'


@unittest.skipUnless(RUN.exists(), 'private successful quest recording required')
class SuccessfulRecordingTests(unittest.TestCase):
    def test_real_successes_freeze_before_return_and_preserve_every_record(self):
        expected = {1: {1132: (101896060272, 66093158)},
                    2: {1130: (106339466056, 25670684), 1023: (109930989094, 13788911)},
                    3: {1005: (117361141362, 20164526)}}
        for segment, quests in expected.items():
            result = replay_quests(RUN / f'segment-{segment:06d}/capture.opcap')
            view = result['quest_view']
            self.assertEqual(view['all_records'], result['capture_report']['counts']['records'])
            by_quest = {r['quest_measurement']['quest_candidate']: r for r in view['history']
                        if r['quest_measurement']['kind'] == 'quest'}
            for quest, (end, damage) in quests.items():
                row = by_quest[quest]
                self.assertEqual(row['quest_measurement']['end_reason'], 'clear_candidate_with_result')
                self.assertEqual(row['quest_measurement']['end_qpc'], end)
                self.assertEqual(row['roles']['self']['damage_dealt'], damage)
                self.assertGreater(row['quest_measurement']['result_qpc'], end)
            if segment == 3:
                for stage in (992, 970, 980, 988):
                    self.assertEqual(by_quest[stage]['quest_measurement']['end_reason'], 'new_world')


STEP30 = Path(__file__).resolve().parents[2] / 'logs_202610211926/captures/run-134370479272858682-7040-0/segment-000001/capture.opcap'
MASQUERADE = Path(__file__).resolve().parents[2] / 'logs_202610211926/c/captures/run-134370586778891416-12820-0/segment-000001/capture.opcap'


@unittest.skipUnless(MASQUERADE.exists(), 'private short Masquerade success recording required')
class MasqueradeRecordingTests(unittest.TestCase):
    def test_short_clear_freezes_before_return_in_replay_incremental_and_gui_readers(self):
        from .identity_context import IdentityContext
        from .reader import CaptureDecoder
        from .live import RunJournal, SegmentFollower
        from tools.gui.backend import saved_snapshot

        result = replay_quests(MASQUERADE)
        view = result['quest_view']
        quest = view['current']
        q = quest['quest_measurement']
        self.assertEqual(q['quest_candidate'], 1130)
        self.assertEqual(q['clear_notice_sequences'], [1408, 1409, 1410, 1411])
        self.assertEqual(q['end_qpc'], 2166280020632)
        self.assertEqual(q['end_reason'], 'clear_candidate_with_result')
        self.assertEqual(q['result_notice_sequence'], 1465)
        self.assertGreater(q['result_qpc'], q['end_qpc'])
        self.assertIsNone(q['campship_return_sequences'])
        self.assertEqual(quest['roles']['self']['damage_dealt'], 6613423)
        self.assertAlmostEqual(quest['timing']['elapsed_seconds'], 37.1254967)
        self.assertEqual(view['all_records'], 143)
        self.assertEqual(view['outside_records'], 6)

        metadata, ref, _, through, _ = IdentityContext(MASQUERADE).read(result['capture_report']['capture_diagnostics'])
        decoder = CaptureDecoder(str(MASQUERADE.resolve()))
        damage = decoder.feed(MASQUERADE.read_bytes())
        decoder.finish()
        session = QuestSession(str(MASQUERADE.resolve()), ref['start_qpc'], ref['qpc_frequency'], ref['source_sha256'])
        cut = next(event.qpc for event in metadata if event.sequence == 1410)
        session.update([event for event in metadata if event.sequence <= 1410],
                       [event for event in damage if event.qpc <= cut], cut)
        self.assertIsNone(session.view()['current']['quest_measurement']['end_qpc'])
        session.update([event for event in metadata if event.sequence <= 1411], [], q['end_qpc'])
        cleared = session.view()['current']
        self.assertEqual(cleared['quest_measurement']['end_reason'], 'clear_candidate')
        session.update(metadata, [event for event in damage if event.qpc > cut], through,
                       end_qpc=ref['end_qpc'], flags=result['capture_report']['quality']['flags'])
        self.assertEqual(session.view(), view)
        self.assertEqual(cleared['timing']['elapsed_seconds'], quest['timing']['elapsed_seconds'])

        journal = RunJournal(MASQUERADE.parent.parent, ref['pid'], ref['source_sha256'], 10)
        journal.poll()
        follower = SegmentFollower(MASQUERADE)
        follower.poll(journal)
        self.assertEqual(follower.quests.view(), view)
        self.assertEqual(saved_snapshot(MASQUERADE, auto_quests=True)['quest_view'], view)


@unittest.skipUnless(STEP30.exists(), 'private Step 30 recording required')
class Step30RecordingTests(unittest.TestCase):
    def test_final_clear_freezes_at_observed_notice_without_closing_intermediate_stages(self):
        result = replay_quests(STEP30)
        view = result['quest_view']
        self.assertEqual(view['all_records'], 14154)
        self.assertEqual(result['capture_report']['counts']['records'], 14154)
        by_quest = {r['quest_measurement']['quest_candidate']: r for r in view['history']
                    if r['quest_measurement']['kind'] == 'quest'}
        final = by_quest[1006]
        q = final['quest_measurement']
        self.assertEqual(q['clear_pair_sequences'], [3174, 3184])
        self.assertEqual(q['end_qpc'], 383944252657)
        self.assertEqual(q['end_reason'], 'clear_candidate_with_result')
        self.assertEqual(final['roles']['self']['damage_dealt'], 24366985)
        self.assertEqual(view['outside_records'], 47)  # Includes all 16 post-clear records.
        # Time ends at the defeating blow, ~16 s before the post-cut-scene result.
        self.assertEqual(q['time_end_evidence'], 'last_player_hit_before_divide_final_clear')
        self.assertAlmostEqual(final['timing']['elapsed_seconds'], 96.8814019)
        self.assertAlmostEqual((q['end_qpc'] - q['time_end_qpc']) / 10_000_000, 15.951, places=3)
        for stage in (972, 999, 966, 946):
            self.assertEqual(by_quest[stage]['quest_measurement']['end_reason'], 'new_world')


if __name__ == '__main__':
    unittest.main()
