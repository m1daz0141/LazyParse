from copy import deepcopy
from io import StringIO
import contextlib
import hashlib
import unittest

from .engine import CombatEngine
from .hit_flags import read_legacy_ignore
from .hit_rates import HitRates
from .__main__ import replay, follow
from .test_combat_core import packet, events, registry, SELF, PET, ENEMY
from .test_hit_flags import HERO, MARON, FIRST
from ..gui.viewmodel import actor_rows, attack_rows, rate_tooltip
from ..gui.backend import saved_snapshot


def flagged(flags, **kwargs):
    data = bytearray(packet(**kwargs))
    data[0x44] = flags
    return bytes(data)


class RateTests(unittest.TestCase):
    def test_scope_before_owner_credit_and_incoming_healing_not_in_denominator(self):
        identities = registry()
        identities.register(PET, 'pet', 'fixture', owner=SELF)
        ignored, _ = read_legacy_ignore()
        ignored_id = next(iter(ignored))
        wire = [flagged(0x41), flagged(0, value=900), flagged(0x41, source=PET),
                flagged(0x41, attack=4065469038), flagged(0x41, attack=ignored_id),
                flagged(0x41, attack=0), flagged(0x41, value=-10000), flagged(0x41, value=0),
                flagged(0x40, source=ENEMY, target=SELF)]
        engine = CombatEngine()
        engine.begin_segment('fixture', identities)
        audit = [engine.feed(event) for event in events([(i, i * 100, data) for i, data in enumerate(wire, 1)])]
        report = engine.finish()
        own = report['roles']['self']
        counts = own['hit_rates']
        self.assertEqual((counts['ja_hits'], counts['ja_eligible_hits'], counts['ja_percent']), (1, 2, 50))
        self.assertEqual((counts['ja_excluded_hits'], counts['ja_unknown_hits']), (3, 0))
        self.assertEqual((counts['crit_hits'], counts['crit_eligible_hits']), (4, 5))
        self.assertEqual((own['damage_dealt'], own['max_hit'], own['damage_taken']), (1300, 900, 100))
        self.assertEqual(audit[2]['hit_flags']['ja_scope_reason'], 'pet_has_no_ja')
        self.assertIsNone(audit[2]['hit_flags']['ja'])
        self.assertTrue(audit[2]['ownership_applied'])
        for index in (6, 7):
            self.assertIsNone(audit[index]['hit_flags']['ja'])
            self.assertIsNone(audit[index]['hit_flags']['crit'])
        self.assertEqual(report['roles']['enemy']['hit_rates']['crit_percent'], 100)
        self.assertEqual(report['roles']['enemy']['hit_rates']['ja_excluded_hits'], 1)

    def test_empty_denominator_missing_ignore_and_zero_vs_unknown(self):
        engine = CombatEngine()
        engine.begin_segment('fixture', registry())
        engine.ja_ignored_ids = None
        audit = engine.feed(events([(1, 0, flagged(0))])[0])
        counts = engine.finish()['roles']['self']['hit_rates']
        self.assertIsNone(counts['ja_percent'])
        self.assertEqual(counts['ja_unknown_hits'], 1)
        self.assertEqual(counts['crit_percent'], 0)
        self.assertEqual(audit['hit_flags']['ja_scope_reason'], 'ja_exclusion_policy_unavailable')
        self.assertIsNone(HitRates().as_dict()['crit_percent'])

    def test_duplicates_snapshots_segments_and_reset_do_not_inflate_rates(self):
        engine = CombatEngine()
        engine.begin_segment('fixture', registry())
        event = events([(1, 0, flagged(0x41))])[0]
        engine.feed(event)
        self.assertIsNone(engine.feed(event))
        before = engine.snapshot()
        self.assertEqual(before, engine.snapshot())
        # Modifying an exported snapshot must not modify engine counts.
        before['roles']['self']['hit_rates']['ja_hits'] = 1000
        engine.begin_segment('other', registry())
        engine.feed(events([(1, 0, flagged(0))], capture_id='other')[0])
        counts = engine.finish()['roles']['self']['hit_rates']
        self.assertEqual((counts['ja_hits'], counts['ja_eligible_hits'], counts['ja_percent']), (1, 2, 50))
        engine.reset()
        self.assertEqual(engine.snapshot()['actors'], [])

    def test_gui_merges_counts_not_percentages_and_preserves_legacy_json_unknowns(self):
        def aggregate(flags, identity):
            engine = CombatEngine()
            engine.begin_segment(identity, registry())
            for event in events([(i, i * 100, flagged(flag)) for i, flag in enumerate(flags, 1)], capture_id=identity):
                engine.feed(event)
            return engine.finish()
        first, second = aggregate([0x41], 'one'), aggregate([0, 0, 0], 'two')
        report = deepcopy(first)
        # One actor lifetime in each capture, merged into one GUI player/attack.
        report['actors'].extend(second['actors'])
        report['attacks'].extend(second['attacks'])
        before = deepcopy(report)
        row = actor_rows(report)[0]
        self.assertEqual((row['ja'], row['crit']), (25, 25))
        attack = attack_rows(report, row)[0]
        self.assertEqual((attack['ja'], attack['crit']), (25, 25))
        self.assertEqual(report, before)
        legacy = deepcopy(first)
        for group in ('actors', 'attacks'):
            for entry in legacy[group]:
                entry.pop('hit_rates')
        legacy.pop('hit_rate_policy')
        row = actor_rows(legacy)[0]
        self.assertIsNone(row['ja'])
        self.assertIsNone(row['crit'])
        self.assertEqual(row['hit_rates']['ja_unknown_hits'], 1)
        self.assertIsNone(attack_rows(legacy, row)[0]['ja'])
        self.assertIn('未判定 1', rate_tooltip(row, 'ja'))
        # Mixed old/new rows preserve the known-hit denominator and unknown count.
        report['actors'].extend(legacy['actors'])
        mixed = actor_rows(report)[0]
        self.assertEqual(mixed['ja'], 25)
        self.assertEqual(mixed['hit_rates']['ja_unknown_hits'], 1)


@unittest.skipUnless(MARON.exists() and FIRST.exists() and (HERO / 'segment-000003/capture.opcap').exists(),
                     'private Classic recordings required')
class RecordingRateTests(unittest.TestCase):
    def test_controlled_hero_production_replay_follow_and_gui_rates(self):
        for segment, numerator, percent in [(2, 1, 100 / 12), (3, 12, 100)]:
            path = HERO / f'segment-{segment:06d}/capture.opcap'
            report = replay(path)
            self.assertEqual(report, replay(path, chunk_size=11))
            with contextlib.redirect_stdout(StringIO()):
                followed = follow(path, registry=None, idle_seconds=10, on_event=lambda row: None)
            self.assertEqual(report, followed)
            own = report['roles']['self']['hit_rates']
            self.assertEqual((own['ja_hits'], own['ja_eligible_hits']), (numerator, 12))
            self.assertEqual(own['crit_percent'], 100)
            gui = actor_rows(report)[0]
            self.assertAlmostEqual(gui['ja'], percent)
            for attack in attack_rows(report, gui):
                if attack['attack_id'] in {2441204394, 3756260250, 3756260700}:
                    self.assertEqual(attack['ja'], 0 if segment == 2 else 100)
            self.assertFalse(report['hit_rate_policy']['validation']['all_classes_and_attacks_verified'])

    def test_maron_owner_crit_includes_pet_but_ja_is_not_applicable(self):
        report = saved_snapshot(MARON)['measurement']['report']
        row = actor_rows(report)[0]
        self.assertIsNone(row['ja'])
        self.assertEqual(row['hit_rates']['ja_excluded_hits'], 13)
        # Maron has one Crit; the Takt's own three hits have two more.
        self.assertEqual((row['hit_rates']['crit_hits'], row['hit_rates']['crit_eligible_hits']), (3, 13))
        self.assertAlmostEqual(row['crit'], 300 / 13)
        self.assertEqual(row['damage_dealt'], 92341)
        pet_attack = next(attack for attack in attack_rows(report, row) if attack['attack_id'] == 2944352598)
        self.assertIsNone(pet_attack['ja'])
        self.assertEqual(pet_attack['crit'], 100)


GUI_LIVE = HERO.parent / 'run-100000000000000006-1-0'


@unittest.skipUnless((GUI_LIVE / 'segment-000002/capture.opcap').exists(),
                     'private GUI-operated Classic recordings required')
class GuiLiveRecordingTests(unittest.TestCase):
    def test_gui_boundary_and_map_return_preserve_totals_and_rates(self):
        expected = [
            (1, 265, 263, 19674602, 253, 259, 4, 260,
             '3ccd66696ab14ff9b2bd55121daeb3c414f24ba8e3c7751df99415e52f7e4ffe'),
            (2, 12, 8, 752445, 8, 8, 0, 8,
             '0136b2897849f1303c908aae4a853f16611b42cb7f13ab386caefddfbb69cd8c')]
        for number, records, hits, damage, ja, eligible, excluded, crit, digest in expected:
            with self.subTest(segment=number):
                path = GUI_LIVE / f'segment-{number:06d}/capture.opcap'
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
                report = replay(path)
                self.assertEqual(report, replay(path, chunk_size=11))
                with contextlib.redirect_stdout(StringIO()):
                    self.assertEqual(report, follow(path, None, 10, lambda row: None))
                row = actor_rows(report)[0]
                self.assertEqual((row['name'], row['damage_dealt'], row['hits_dealt']), ('Demo', damage, hits))
                self.assertEqual(report['counts']['records'], records)
                self.assertEqual(row['damage_taken'], 0)
                rates = row['hit_rates']
                self.assertEqual((rates['ja_hits'], rates['ja_eligible_hits'], rates['ja_excluded_hits'],
                                  rates['crit_hits'], rates['crit_eligible_hits']),
                                 (ja, eligible, excluded, crit, hits))
                self.assertEqual(row['ja'], ja * 100 / eligible)
                self.assertEqual(sum(a['hits_dealt'] for a in attack_rows(report, row)), hits)
                self.assertEqual(report['quality']['flags'], [])
                if number == 2:
                    self.assertEqual(row['scene'], 3)
                    self.assertEqual((row['ja'], row['crit']), (100, 100))


if __name__ == '__main__':
    unittest.main()
