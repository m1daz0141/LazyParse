import json
from pathlib import Path
import tempfile
import unittest

from tools.gui import privacy
from tools.gui.__main__ import argument_parser
from tools.gui.backend import Backend
from tools.gui.text_export import render_text
from tools.gui.viewmodel import actor_rows, attack_rows, clipboard_summary, party_total, source_rows

SELF = {'object_id': 1, 'entity_type': 4, 'map_id': 0}
OTHER = {'object_id': 2, 'entity_type': 4, 'map_id': 0}


def actor(ref, role, name, damage, *, kind='player', unlinked=0):
    key = f"8/4:0:{ref['object_id']}#0"
    return {'capture_id': 'c', **ref, 'key': key, 'actor_key': key, 'kind': kind, 'role': role, 'name': name,
            'owner': None, 'damage_dealt': damage, 'hits_dealt': 10, 'damage_taken': 5, 'hits_taken': 1,
            'max_hit': damage // 10, 'rated_max_hit': damage // 10, 'hp_unlinked_damage': unlinked,
            'hp_unlinked_hits': 1 if unlinked else 0, 'dps': damage / 10,
            'hit_rates': {'ja_hits': 5, 'ja_eligible_hits': 10, 'ja_excluded_hits': 0, 'ja_unknown_hits': 0,
                          'crit_hits': 2, 'crit_eligible_hits': 10, 'crit_unknown_hits': 0}}


def report():
    me, other = actor(SELF, 'self', 'Me', 300), actor(OTHER, 'other_player', 'Someone', 700, unlinked=100)
    enemy = actor({'object_id': 9, 'entity_type': 8, 'map_id': 0}, 'enemy', 'Enemy', 0, kind='enemy')
    pet_of_other = {**OTHER, 'object_id': 3, 'actor_key': '8/9:0:3#0', 'kind': 'pet', 'role': 'pet',
                    'name': 'OtherPet', 'owner': OTHER}
    pet_of_me = {**SELF, 'object_id': 4, 'actor_key': '8/9:0:4#0', 'kind': 'pet', 'role': 'pet',
                 'name': 'MyPet', 'owner': SELF}
    attack = lambda who, aid: {'capture_id': 'c', 'credited_source_key': who['actor_key'], 'attack_id': aid,
        'damage_dealt': who['damage_dealt'], 'hits_dealt': 10, 'damage_taken': 0, 'hits_taken': 0,
        'max_hit': 1, 'min_hit': 1, 'rated_max_hit': 1, 'hit_rates': who['hit_rates']}
    return {'schema_version': 1, 'counts': {'records': 20, 'positive_sum': 1000},
            'timing': {'elapsed_seconds': 10, 'definition': 'quest'}, 'encounters': [],
            'quest_measurement': {'kind': 'quest', 'quest_candidate': 475},
            'actors': [me, other, enemy], 'attacks': [attack(me, 1), attack(other, 2)],
            'raw_source_damage': [
                {'source_key': me['actor_key'], 'credited_source_key': me['actor_key'], 'attack_id': 1, 'damage': 250},
                {'source_key': pet_of_me['actor_key'], 'credited_source_key': me['actor_key'], 'attack_id': 1, 'damage': 50},
                {'source_key': pet_of_other['actor_key'], 'credited_source_key': other['actor_key'], 'attack_id': 2, 'damage': 700}],
            'negative_values': [{'source_key': other['actor_key'], 'target_key': other['actor_key'], 'attack_id': 5}],
            'roles': {'self': {'damage_dealt': 300}, 'enemy': {'damage_dealt': 0},
                      'other_player': {'damage_dealt': 700, 'hits_dealt': 10, 'max_hit': 70, 'dps': 70.0,
                                       'hp_unlinked_damage': 100}},
            'identity_state': {'self': {**SELF, 'actor_key': me['actor_key']},
                               'entities': [me, other, pet_of_other, pet_of_me]}}


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        privacy.configure(False)
        self.addCleanup(privacy.configure, False)

    def test_other_players_fold_into_one_total_without_identities(self):
        restricted = privacy.restrict_report(report())
        text = json.dumps(restricted, ensure_ascii=False)
        for secret in ('Someone', 'OtherPet', '"attack_id": 2', '"max_hit": 70', '"dps": 70.0'):
            self.assertNotIn(secret, text)
        others = [a for a in restricted['actors'] if a['role'] == privacy.OTHERS_ROLE]
        self.assertEqual([(a['name'], a['damage_dealt'], a['hp_unlinked_damage'], a['max_hit']) for a in others],
                         [('その他合計', 700, 100, 0)])
        self.assertEqual(restricted['roles']['other_player']['damage_dealt'], 700)
        self.assertIn('MyPet', text)
        self.assertEqual(restricted['negative_values'], [])
        self.assertIs(privacy.restrict_report(restricted), restricted)

    def test_rows_text_and_clipboard_keep_the_party_total_and_own_share(self):
        full, restricted = report(), privacy.restrict_report(report())
        rows = actor_rows(restricted)
        self.assertEqual([row['name'] for row in rows], ['Me'])
        self.assertEqual(rows[0]['share'], next(r for r in actor_rows(full) if r['name'] == 'Me')['share'])
        self.assertEqual(party_total(restricted), party_total(full))
        self.assertEqual(sorted(s['damage'] for s in source_rows(restricted, rows[0])), [50, 250])
        self.assertEqual([a['attack_id'] for a in attack_rows(restricted, rows[0])], [1])
        text = render_text(restricted)
        self.assertIn('TotalDamage : 900', text)
        self.assertNotIn('標準偏差', text)
        self.assertNotIn('その他合計', text)
        self.assertIn('全体 900', clipboard_summary(restricted, rows))

    def test_overparse_keeps_everything(self):
        privacy.configure(True)
        original = report()
        self.assertIs(privacy.restrict_report(original), original)
        self.assertEqual(len(actor_rows(original)), 2)
        snapshot = {'measurement': {'report': original}}
        self.assertIs(privacy.restrict_snapshot(snapshot), snapshot)

    def test_snapshot_reports_are_restricted_everywhere(self):
        snapshot = {'measurement': {'report': report()}, 'capture_measurement': {'report': report()},
                    'quest_history': [{'report': report()}], 'quest_view': {'current': report(), 'history': [report()]},
                    'history': [{'report': report(), 'roles': report()['roles'], 'quest_reports': [report()]}],
                    'identity': {'state': report()['identity_state']}}
        text = json.dumps(privacy.restrict_snapshot(snapshot), ensure_ascii=False)
        self.assertNotIn('Someone', text)
        self.assertNotIn('"max_hit": 70', text)

    def test_auto_archive_saves_the_restricted_report(self):
        with tempfile.TemporaryDirectory() as work:
            backend = Backend(monitor=object(), controller=object(), log_directory=Path(work))
            saved = json.loads(Path(backend._auto_archive(report())).read_text(encoding='utf-8'))
            self.assertTrue(privacy.is_restricted(saved))
            self.assertNotIn('Someone', json.dumps(saved, ensure_ascii=False))
            privacy.configure(True)
            full = json.loads(Path(backend._auto_archive(report())).read_text(encoding='utf-8'))
            self.assertIn('Someone', json.dumps(full, ensure_ascii=False))

    def test_overparse_is_an_unlisted_option(self):
        parser = argument_parser()
        self.assertTrue(parser.parse_args(['--overparse']).overparse)
        self.assertFalse(parser.parse_args([]).overparse)
        self.assertNotIn('overparse', parser.format_help())


if __name__ == '__main__':
    unittest.main()
