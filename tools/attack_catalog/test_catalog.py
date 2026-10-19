from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from .catalog import AttackCatalog, DATA, attack_label, get_catalog, parse_csv, with_attack_names
from .audit import audit_report

CAPTURE = Path(__file__).resolve().parents[1] / 'native_capture/captures/classic-auto-fixture-001/run-100000000000000001-1-0/segment-000001/capture.opcap'


class CatalogTests(unittest.TestCase):
    def test_editable_csv_overrides_adds_and_preserves_pinned_source(self):
        with tempfile.TemporaryDirectory() as work:
            editable = Path(work) / 'skills.csv'
            pinned_hash = hashlib.sha256((DATA / 'skills_ja.csv').read_bytes()).hexdigest()
            editable.write_text('Type,ID,Kind,Note\n手動名,115059488,PA,注記\n追加名,4294967295,Special\n', encoding='utf-8-sig')
            catalog = AttackCatalog.load(editable=editable)
            self.assertIsNone(catalog.error)
            self.assertEqual(catalog.labels(115059488)['attack_name'], '手動名')
            self.assertEqual(catalog.labels(115059488)['attack_name_source'], {'path': str(editable), 'line': 2})
            self.assertEqual(catalog.labels(0xFFFFFFFF)['attack_name'], '追加名')
            self.assertEqual(catalog.labels(2944355330)['attack_name'], '通常攻撃(マロン)')
            self.assertTrue(catalog.metadata()['editable']['manually_editable'])
            self.assertEqual(hashlib.sha256((DATA / 'skills_ja.csv').read_bytes()).hexdigest(), pinned_hash)
            editable.write_text('Bad,7,PA\nConflict,7,Tech\n', encoding='utf-8')
            fallback = AttackCatalog.load(editable=editable)
            self.assertIn('Conflicting', fallback.error)
            self.assertEqual(fallback.labels(115059488)['attack_name'], 'マロンブレイク')
            self.assertFalse(fallback.labels(7)['attack_name_matched'])
            self.assertFalse(fallback.metadata()['editable']['available'])

    def test_pinned_bytes_count_and_comment_are_not_an_attack(self):
        catalog = get_catalog()
        self.assertIsNone(catalog.error)
        self.assertEqual(len(catalog.entries), 1900)
        self.assertEqual(hashlib.sha256((DATA / 'skills_ja.csv').read_bytes()).hexdigest(), catalog.source['sha256'])
        self.assertNotIn(32, catalog.entries)
        self.assertEqual(catalog.labels(115059488)['attack_name'], 'マロンブレイク')
        self.assertEqual(catalog.labels(115059488)['attack_name_source']['line'], 986)
        self.assertEqual(attack_label(4065469038), '通常攻撃(タクト)')

    def test_unsigned_id_unknown_and_zero_are_preserved(self):
        self.assertEqual(get_catalog().labels(2944355330)['attack_name'], '通常攻撃(マロン)')
        for attack_id in (0, 0xFFFFFFFF):
            self.assertFalse(get_catalog().labels(attack_id)['attack_name_matched'])
            self.assertIn(f'0x{attack_id:08X}', attack_label(attack_id))
        self.assertIn(0xFFFFFFFF, parse_csv('Upper,4294967295,PA\n'))
        for attack_id in ('-1', '4294967296', '0xFFFF'):
            with self.assertRaises(ValueError):
                parse_csv(f'Invalid,{attack_id},PA\n')

    def test_csv_quotes_short_rows_freeform_notes_and_duplicates(self):
        entries = parse_csv('Type,ID,Kind,Name\n"Name, with comma",7,PA,"Line one\nline two"\nShort,8\nStage,9,1段目,詳細\nStage,9,1段目,詳細\n')
        self.assertEqual(entries[7].name, 'Name, with comma')
        self.assertEqual(entries[7].kind, 'PA')
        self.assertEqual(entries[7].note, 'Line one\nline two')
        self.assertEqual(entries[8].line, 4)
        self.assertIsNone(entries[8].kind)
        self.assertIsNone(entries[9].kind)
        self.assertEqual(entries[9].note, '1段目 / 詳細')
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            parse_csv('First,7,PA\nSecond,7,PA\n')

    def test_missing_or_changed_catalog_falls_back_without_touching_totals(self):
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work)
            missing = AttackCatalog.load(directory)
            self.assertTrue(missing.error)
            source = json.loads((DATA / 'source.json').read_text())
            (directory / 'source.json').write_text(json.dumps(source))
            (directory / 'skills_ja.csv').write_text('Unexpected,7,PA\n')
            changed = AttackCatalog.load(directory)
            self.assertIn('SHA-256', changed.error)
            self.assertFalse(changed.labels(115059488)['attack_name_matched'])
            self.assertEqual(changed.entries, {})

    def test_label_projection_keeps_id_groups_and_every_numeric_value(self):
        report = {'counts': {'records': 5}, 'attacks': [
            {'attack_id': 2944355330, 'damage_dealt': 100, 'hits_dealt': 2},
            {'attack_id': 2944352598, 'damage_dealt': 200, 'hits_dealt': 1},
            {'attack_id': 0, 'damage_dealt': 6, 'hits_dealt': 2}],
            'negative_values': [{'attack_id': 0, 'count': 3, 'signed_sum': -539}]}
        original = deepcopy(report)
        named = with_attack_names(report)
        self.assertEqual(report, original)
        self.assertEqual(len(named['attacks']), 3)
        self.assertEqual(named['attacks'][0]['attack_name'], named['attacks'][1]['attack_name'])
        audit = audit_report(named, 'fixture')
        self.assertEqual((audit['positive_hits'], audit['named_positive_hits']), (5, 3))
        self.assertEqual(audit['unknown_positive_ids'], [0])
        self.assertEqual(sum(row['positive_damage'] for row in audit['attacks']), 306)
        self.assertEqual(audit['attacks'][0]['negative_signed_sum'], -539)


@unittest.skipUnless(CAPTURE.exists(), 'private saved Classic recording required')
class LiveRecordingTests(unittest.TestCase):
    def test_saved_maron_events_report_and_original_numbers_match(self):
        from ..combat_core.__main__ import replay
        events = []
        report = replay(CAPTURE, on_event=events.append)
        previous = json.loads(CAPTURE.with_name('combat-report.json').read_text(encoding='utf-8'))
        for field in ('counts', 'damage_by_direction'):
            self.assertEqual(report[field], previous[field])
        # This preserved artifact predates the continuous-time DPS change.
        # Compare its original time/DPS through the explicit legacy option.
        from ..gui.timing import with_dps_mode
        legacy = with_dps_mode(report, 'damage')
        self.assertEqual(legacy['timing'], previous['timing'])
        for role, totals in previous['roles'].items():
            self.assertEqual({key: legacy['roles'][role][key] for key in totals}, totals)
        fields = ('attack_id', 'damage_dealt', 'hits_dealt', 'max_hit', 'credited_source_key')
        self.assertEqual([{key: row[key] for key in fields} for row in report['attacks']],
                         [{key: row[key] for key in fields} for row in previous['attacks']])
        names = {row['attack_id']: row['attack_name'] for row in report['attacks']}
        self.assertEqual(names[115059488], 'マロンブレイク')
        self.assertEqual(names[2944355330], '通常攻撃(マロン)')
        self.assertEqual(names[2944352598], '通常攻撃(マロン)')
        self.assertEqual(names[4065469038], '通常攻撃(タクト)')
        self.assertEqual(report['roles']['self']['damage_dealt'], 92341)
        self.assertTrue(all(event['attack_name_matched'] for event in events if event['signed_value'] > 1))
        self.assertEqual(report['counts']['negative_sum'], -539)
        self.assertEqual(hashlib.sha256(CAPTURE.read_bytes()).hexdigest(),
                         '0000000000000000000000000000000000000000000000000000000000000000')


if __name__ == '__main__':
    unittest.main()
