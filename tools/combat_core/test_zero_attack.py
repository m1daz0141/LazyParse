from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from . import damage_policy
from .damage_policy import CONFIGURED_EXCLUSION, parse_exclusions, read_damage_exclusions
from .engine import CombatEngine
from .test_combat_core import SELF, OTHER, ENEMY, PET, ObjectRef, registry, packet, events, engine
from ..gui.timing import with_dps_mode
from ..gui.text_export import render_text
from ..gui.viewmodel import actor_rows


class ZeroAttackTests(unittest.TestCase):
    def test_zero_attack_excluded_for_all_sources_without_losing_raw_records(self):
        identities = registry()
        identities.register(PET, 'pet', 'fixture', owner=SELF)
        sources = [SELF, OTHER, ENEMY, PET, ObjectRef(0, 0)]
        rows = [(i + 1, i * 1000, packet(source=source, value=9999, attack=0))
                for i, source in enumerate(sources)]
        e = CombatEngine()
        e.begin_segment('fixture', identities)
        audit = [e.feed(event) for event in events(rows)]
        result = e.finish()
        self.assertEqual(result['counts']['records'], 5)
        self.assertEqual(result['counts']['positive_sum'], 49995)
        self.assertEqual(result['damage_totals']['positive_sum'], 0)
        self.assertEqual(result['damage_totals']['excluded_positive_records'], 5)
        self.assertFalse(result['actors'])
        self.assertFalse(result['attacks'])
        self.assertFalse(result['encounters'])
        self.assertFalse(result['timing']['spans'])
        self.assertTrue(all(event['exclusion_reason'] == CONFIGURED_EXCLUSION for event in audit))
        self.assertTrue(all(not event['included_in_damage'] for event in audit))
        self.assertEqual([event['packet_hex'] for event in audit], [row[2].hex() for row in rows])
        self.assertFalse(result['quality']['sequence_gaps'])

    def test_damage_hits_details_and_both_dps_modes_use_only_valid_attacks(self):
        rows = [(1, 0, packet(value=5000, attack=0)),
                (2, 1000, packet(value=100, attack=99)),
                (3, 2000, packet(source=ENEMY, target=SELF, value=9000, attack=0)),
                (4, 3000, packet(value=300, attack=99)),
                (5, 9000, packet(value=7000, attack=0))]
        result = engine(rows, registry()).finish(10000, 1000)
        before = deepcopy(result)
        self.assertEqual(result['damage_totals']['positive_sum'], 400)
        self.assertEqual(result['damage_totals']['excluded_positive_sum'], 21000)
        self.assertEqual(result['roles']['self']['damage_dealt'], 400)
        self.assertEqual(result['roles']['self']['hits_dealt'], 2)
        self.assertEqual(result['roles']['self']['damage_taken'], 0)
        self.assertEqual(result['roles']['self']['hits_taken'], 0)
        self.assertEqual(result['roles']['self']['max_hit'], 300)
        self.assertEqual([a['attack_id'] for a in result['attacks']], [99])
        self.assertEqual(result['attacks'][0]['min_hit'], 100)
        self.assertEqual(result['encounters'][0]['elapsed_seconds'], 2)
        self.assertEqual(result['timing']['spans'][0]['first_damage_qpc'], 1000)
        for mode, elapsed in [('quest', 9), ('damage', 2)]:
            with self.subTest(mode=mode):
                view = with_dps_mode(result, mode)
                own = actor_rows(view)[0]
                self.assertEqual(own['damage_dealt'], 400)
                self.assertAlmostEqual(own['dps'], 400 / elapsed)
                self.assertNotIn('0x00000000', render_text(result, dps_mode=mode))
        self.assertEqual(result, before)


class AttackExclusionCsvTests(unittest.TestCase):
    def test_bundled_default_lists_only_attack_zero_with_headers(self):
        text = damage_policy.DEFAULT_EXCLUSIONS_PATH.read_text(encoding='utf-8-sig')
        self.assertTrue(text.startswith('(comments),(attack_id)'))
        self.assertEqual(parse_exclusions(text), [0])

    def test_parse_formats_and_errors(self):
        text = ('(comments),(attack_id)\n\n攻撃ID 0,0\n"備考, カンマ入り",0x0000002A\nギミック,123456789,\n')
        self.assertEqual(parse_exclusions(text), [0, 42, 123456789])
        for bad in ('名前,abc\n', '名前,1,2\n', '名前,4294967296\n', '0\n', '名前,-1\n'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_exclusions(bad)

    def test_user_file_applies_to_every_source_and_broken_file_falls_back(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'skills_exclusions.csv'
            path.write_text('(comments),(attack_id)\nテスト除外,99\n', encoding='utf-8-sig')
            with patch.object(damage_policy, 'EXCLUSIONS_PATH', path):
                rows = [(1, 0, packet(value=100, attack=99)), (2, 1000, packet(source=OTHER, value=50, attack=99)),
                        (3, 2000, packet(value=7, attack=0)), (4, 3000, packet(value=30, attack=98))]
                result = engine(rows, registry()).finish(4000, 1000)
                self.assertEqual(result['damage_totals']['positive_sum'], 37)  # ID 0 no longer listed.
                self.assertEqual({(g['attack_id'], g['reason']) for g in result['damage_totals']['excluded_groups']},
                                 {(99, CONFIGURED_EXCLUSION)})
                policy = result['aggregation_policy']['configured_attack_exclusions']
                self.assertEqual((policy['attack_ids'], policy['error']), ([99], None))
                path.write_text('壊れた行,1,2\n', encoding='utf-8')
                ids, metadata = read_damage_exclusions()
                self.assertEqual(ids, frozenset({0}))
                self.assertEqual(metadata['fallback'], 'bundled_default')
                self.assertIn('2列', metadata['error'])


if __name__ == '__main__':
    unittest.main()
