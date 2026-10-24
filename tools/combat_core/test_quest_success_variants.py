"""Short Extreme results and the 36-byte exploration success variant."""
from dataclasses import replace
from pathlib import Path
import unittest

from .quests import QuestSession, SUPPORTED_CLASSIC
from .quest_replay import replay_quests
from .test_quests import fixture, level, notice, transfer, campship_exit
from .test_combat_core import events, packet


def short_success(extreme):
    shapes = [(0x26, 24), (0x4e, 12), (0x11, 72)] if extreme else [(0x26, 24), (0x11, 36)]
    return [notice(4 + i, 800 + i, 11, minor, size) for i, (minor, size) in enumerate(shapes)]


class ShortSuccessTests(unittest.TestCase):
    def test_complete_variants_freeze_incrementally_and_survive_return_and_retry(self):
        for quest, extreme in ((498, False), (496, False), (1021, True), (1025, True), (9999, True)):
            with self.subTest(quest=quest):
                prefix = fixture()[0][:2] + [level(3, 100, 17, quest)]
                tail = short_success(extreme)
                s = QuestSession('fixture', 20, 1000, SUPPORTED_CLASSIC)
                s.update(prefix + tail[:-1], events([(1, 200, packet(value=100))]), tail[-1].qpc - 1)
                self.assertIsNone(s.view()['current']['quest_measurement']['end_qpc'])
                s.update(prefix + tail, [], 1000)
                cleared = s.view()['current']
                self.assertEqual(cleared['quest_measurement']['end_qpc'], tail[-1].qpc)
                self.assertEqual(cleared['quest_measurement']['end_reason'], 'clear_candidate')
                returned = prefix + tail + [transfer(10, 1100), campship_exit(11, 1110),
                    notice(12, 1120, 11, 0x21, 12), notice(13, 1121, 11, 0x27, 120),
                    notice(14, 1200, 11, 0x14, 1856)]
                s.update(returned, events([(2, 1001, packet(value=3, attack=0))]), 1300)
                final = s.view()['current']
                self.assertEqual(final['quest_measurement']['end_reason'], 'clear_candidate_with_result')
                self.assertEqual(final['quest_measurement']['end_qpc'], tail[-1].qpc)
                for key in ('elapsed_seconds', 'dps'):
                    self.assertEqual(final['timing'][key], cleared['timing'][key])
                self.assertEqual([span['end_qpc'] for span in final['timing']['spans']],
                    [span['end_qpc'] for span in cleared['timing']['spans']])
                self.assertEqual(final['roles']['self']['damage_dealt'], 100)
                self.assertEqual(s.view()['outside_records'], 1)
                s.update(returned + [level(15, 1500, 18, quest)], [], 1600)
                self.assertIsNone(s.view()['current']['quest_measurement']['end_qpc'])
                self.assertEqual(s.view()['current']['counts']['records'], 0)

    def test_partial_gapped_reordered_delayed_or_different_sizes_do_not_clear(self):
        for extreme in (False, True):
            good = short_success(extreme)
            cases = [good[:-1], good[1:],
                good[:-1] + [replace(good[-1], sequence=99)],
                good[:-1] + [replace(good[-1], qpc=851)],
                good[:-1] + [replace(good[-1], qpc=799)],
                good[:-1] + [replace(good[-1], packet_size=68)],
                [good[-1]] + good[:-1],
                good[:1] + [notice(5, 801, 11, 0x13, 52)] +
                    [replace(e, sequence=e.sequence+1) for e in good[1:]]]
            for tail in cases:
                with self.subTest(extreme=extreme, tail=tail):
                    s = QuestSession('fixture', 20, 1000, SUPPORTED_CLASSIC)
                    s.update(fixture()[0][:2] + [level(3, 100, 17, 1021 if extreme else 498)] + tail,
                        events([(1, 200, packet(value=100))]), 1000)
                    self.assertIsNone(s.view()['current']['quest_measurement']['end_qpc'])


ROOT = Path(__file__).resolve().parents[2] / 'logs_private_fixture/captures'
EXPLORATIONS = ROOT / 'run-100000000000000002-1-0/segment-000001/capture.opcap'
EXTREME = ROOT / 'run-100000000000000003-1-0/segment-000001/capture.opcap'


@unittest.skipUnless(EXPLORATIONS.exists() and EXTREME.exists(), 'private Oct24 recordings required')
class ShortSuccessRecordingTests(unittest.TestCase):
    def test_recordings_incremental_clear_return_and_gui_agree_without_losing_records(self):
        from .identity_context import IdentityContext
        from .reader import CaptureDecoder
        from .live import RunJournal, SegmentFollower
        from tools.gui.backend import saved_snapshot

        for capture, expected_records, sequences in (
            (EXPLORATIONS, 13071, {3978: [1979, 1980], 4041: [8600, 8601], 4054: [9722, 9723]}),
            (EXTREME, 933, {8048: [2072, 2073, 2074], 8064: [2659, 2660, 2661],
                8078: [3284, 3285, 3286], 8093: [3812, 3813, 3814], 8100: [4525, 4526, 4527]})):
            with self.subTest(capture=capture):
                result = replay_quests(capture)
                view = result['quest_view']
                self.assertEqual(view['all_records'], expected_records)
                metadata, ref, _, through, _ = IdentityContext(capture).read(result['capture_report']['capture_diagnostics'])
                decoder = CaptureDecoder(str(capture.resolve()))
                damage = decoder.feed(capture.read_bytes())
                decoder.finish()
                session = QuestSession(str(capture.resolve()), ref['start_qpc'], ref['qpc_frequency'], ref['source_sha256'])
                cursor = ref['start_qpc'] - 1
                for world, seqs in sequences.items():
                    end = next(e.qpc for e in metadata if e.sequence == seqs[-1])
                    cut = next(e.qpc for e in metadata if e.sequence == seqs[-1] - 1)
                    session.update([e for e in metadata if e.sequence < seqs[-1]],
                        [e for e in damage if cursor < e.qpc <= cut], cut)
                    before = session.view()['current']['quest_measurement']
                    self.assertEqual(before['world_candidate'], world)
                    self.assertIsNone(before['end_qpc'])  # Every intermediate stage kept running.
                    session.update([e for e in metadata if e.sequence <= seqs[-1]],
                        [e for e in damage if cut < e.qpc <= end], end)
                    cleared = session.view()['current']
                    self.assertEqual(cleared['quest_measurement']['end_reason'], 'clear_candidate')
                    self.assertEqual(cleared['quest_measurement']['clear_notice_sequences'], seqs)
                    final = next(r for r in view['history'] if r['quest_measurement']['kind'] == 'quest'
                        and r['quest_measurement']['world_candidate'] == world)
                    self.assertEqual(final['quest_measurement']['end_reason'], 'clear_candidate_with_result')
                    self.assertEqual(final['quest_measurement']['end_qpc'], end)
                    self.assertGreater(final['quest_measurement']['result_qpc'], end)
                    self.assertEqual(final['timing']['elapsed_seconds'], cleared['timing']['elapsed_seconds'])
                    self.assertEqual(final['roles'], cleared['roles'])
                    cursor = end
                session.update(metadata, [e for e in damage if e.qpc > cursor], through,
                    end_qpc=ref['end_qpc'], flags=result['capture_report']['quality']['flags'])
                self.assertEqual(session.view(), view)
                journal = RunJournal(capture.parent.parent, ref['pid'], ref['source_sha256'], 10)
                journal.poll()
                follower = SegmentFollower(capture)
                follower.poll(journal)
                self.assertEqual(follower.quests.view(), view)
                self.assertEqual(saved_snapshot(capture, auto_quests=True)['quest_view'], view)


if __name__ == '__main__':
    unittest.main()
