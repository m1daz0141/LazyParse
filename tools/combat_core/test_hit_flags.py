from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

from .audit_hit_flags import audit_capture, summarize
from .hit_flags import JA_MASK, CRIT_MASK, inspect_event, legacy_flags, read_legacy_ignore, read_ja_exclusions, RULES_PATH

ROOT = Path(__file__).resolve().parents[2]
MARON = ROOT / 'tools/native_capture/captures/classic-auto-fixture-001/run-100000000000000001-1-0/segment-000001/capture.opcap'
FIRST = ROOT / 'tools/native_capture/captures/classic-auto-fixture-004/capture.opcap'
HERO = ROOT / 'tools/native_capture/captures/classic-auto-fixture-006/run-100000000000000005-1-0'


def event(flags=0, source='player', value=100, attack=99):
    data = bytearray(84)
    struct.pack_into('<I4B', data, 0, 84, 64, 4, 0x52, 0)
    struct.pack_into('<Ii', data, 0x2C, attack, value)
    data[0x44] = flags
    return {'packet_hex': data.hex(), 'attack_id': attack, 'signed_value': value,
            'source': {'kind': source}, 'credited_source': {'kind': 'player', 'role': 'self'}}


class CandidateTests(unittest.TestCase):
    def test_historical_masks_are_independent_and_not_bit_two(self):
        self.assertEqual((JA_MASK, CRIT_MASK), (0x01, 0x40))
        for flags, ja, crit in [(0, False, False), (1, True, False), (0x40, False, True),
                                (0x41, True, True), (2, False, False), (0xFF, True, True)]:
            row = inspect_event(event(flags), frozenset())
            self.assertEqual((row['ja_candidate'], row['crit_candidate']), (ja, crit))
            self.assertEqual(row['flags_raw'], flags)
            self.assertFalse(row['ja_current_verified'])
            self.assertFalse(row['crit_current_verified'])

    def test_pet_has_no_ja_even_with_owner_credit_and_raw_ja_bit_set(self):
        original = event(0x41, source='pet')
        before = deepcopy(original)
        row = inspect_event(original, frozenset())
        self.assertFalse(row['ja_eligible_candidate'])
        self.assertEqual(row['ja_scope_reason'], 'pet_has_no_ja')
        self.assertIsNone(row['ja_candidate'])
        self.assertTrue(row['legacy_ja_bit'])
        self.assertTrue(row['crit_candidate'])
        self.assertEqual(original, before)

    def test_takt_and_legacy_ignore_do_not_count_as_failed_ja(self):
        for attack, ignored, reason in [(4065469038, frozenset(), 'takt_normal_pet_related_no_ja_policy'),
                                        (99, frozenset({99}), 'configured_no_ja_id')]:
            row = inspect_event(event(attack=attack), ignored)
            self.assertFalse(row['ja_eligible_candidate'])
            self.assertIsNone(row['ja_candidate'])
            self.assertEqual(row['ja_scope_reason'], reason)

    def test_unknown_zero_nonplayer_and_nonpositive_keep_separate_states(self):
        self.assertIsNone(inspect_event(event(attack=0), frozenset())['ja_eligible_candidate'])
        self.assertIsNone(inspect_event(event(), None)['ja_eligible_candidate'])
        self.assertEqual(inspect_event(event(source='enemy'), frozenset())['ja_scope_reason'], 'non_player_source')
        for value in (-539, 0):
            row = inspect_event(event(0x41, value=value), frozenset())
            self.assertIsNone(row['ja_candidate'])
            self.assertIsNone(row['crit_candidate'])
            self.assertEqual(row['flags_raw'], 0x41)

    def test_invalid_layout_or_mismatched_event_is_rejected(self):
        with self.assertRaises(ValueError):
            legacy_flags(bytes.fromhex(event()['packet_hex'])[:80])
        wrong = event()
        wrong['signed_value'] = 99
        with self.assertRaisesRegex(ValueError, 'values differ'):
            inspect_event(wrong, frozenset())

    def test_ignore_policy_provenance_and_unavailable_are_explicit(self):
        ignored, policy = read_ja_exclusions(RULES_PATH)
        self.assertEqual(ignored, frozenset({4065469038}))
        self.assertEqual(policy['rows'], 1)
        self.assertTrue(policy['available'])
        self.assertFalse(policy['requires_overparse_files'])
        with tempfile.TemporaryDirectory() as work:
            ignored, policy = read_ja_exclusions(Path(work) / 'missing.csv')
            self.assertIsNone(ignored)
            self.assertFalse(policy['available'])
            configured = Path(work) / 'configured.csv'
            configured.write_text('attack_id,reason\n99,independent_test\n100,another_rule\n', encoding='utf-8')
            ignored, policy = read_ja_exclusions(configured)
            self.assertEqual(ignored, frozenset({99, 100}))
            configured.write_text('4294967296,bad\n', encoding='utf-8')
            ignored, policy = read_ja_exclusions(configured)
            self.assertIsNone(ignored)
            self.assertIn('uint32', policy['error'])


@unittest.skipUnless(MARON.exists() and FIRST.exists(), 'private Classic recordings required')
class RecordingTests(unittest.TestCase):
    def test_maron_ja_exclusion_and_crit_candidates_preserve_owner_totals(self):
        ignored, _ = read_legacy_ignore()
        report, rows = audit_capture(MARON, ignored)
        pet = [row for row in rows if row['source_kind'] == 'pet' and row['signed_value'] > 0]
        self.assertEqual(len(pet), 10)
        self.assertTrue(all(row['ja_candidate'] is None for row in pet))
        self.assertEqual(sum(row['crit_candidate'] is True for row in pet), 1)
        self.assertEqual(next(row['signed_value'] for row in pet if row['crit_candidate']), 14007)
        self.assertEqual(report['summary']['ja_eligible_candidate_set'], 0)
        self.assertEqual(report['summary']['ja_eligible_candidate_unset'], 0)
        self.assertEqual(report['roles']['self']['damage_dealt'], 92341)
        self.assertEqual(report['counts']['negative_sum'], -539)
        self.assertEqual(report['capture_sha256'], '79f87f5cd632c61d6d311df08372bdb29025ae77ebc1a8508ea0fa34d1724ff6')

    def test_prior_screenshot_values_have_both_crit_candidate_states(self):
        ignored, _ = read_legacy_ignore()
        _, rows = audit_capture(FIRST, ignored)
        positives = {row['signed_value']: row for row in rows if row['signed_value'] > 0}
        for value, expected in [(30529, True), (30535, True), (30547, True), (56652, False)]:
            self.assertEqual(positives[value]['crit_candidate'], expected)
            self.assertTrue(positives[value]['ja_candidate'])
        summary = summarize(rows)
        self.assertEqual((summary['ja_eligible_candidate_set'], summary['ja_eligible_candidate_unset']), (6, 0))

    def test_real_cli_csv_null_pet_ja_and_existing_output_guard(self):
        with tempfile.TemporaryDirectory() as work:
            output, csv_path = Path(work) / 'audit.json', Path(work) / 'events.csv'
            args = [sys.executable, '-X', 'utf8', '-m', 'tools.combat_core.audit_hit_flags', str(MARON),
                    '--output', str(output), '--csv', str(csv_path)]
            process = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=15)
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(output.read_text(encoding='utf-8'))
            self.assertFalse(result['policy']['current_client_verified'])
            with csv_path.open(encoding='utf-8-sig', newline='') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 15)
            self.assertTrue(all(row['ja_candidate'] == '' for row in rows if row['source_kind'] == 'pet'))
            saved_hash = hashlib.sha256(output.read_bytes()).hexdigest()
            again = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=15)
            self.assertNotEqual(again.returncode, 0)
            self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), saved_hash)


@unittest.skipUnless((HERO / 'segment-000003/capture.opcap').exists(), 'private controlled Hero recording required')
class ControlledHeroTests(unittest.TestCase):
    def test_same_later_combo_ids_clear_and_set_ja_with_unchanged_damage(self):
        ignored, _ = read_legacy_ignore()
        expected = [(2, False, 859372, '2338b31f03c3d8c0804bf9db8e239cd410f45036afd42d4e801f55fa546fc695'),
                    (3, True, 1102251, 'ae1040da2e688ef3614004126c6eeb2e67075eeaf201698a7ddf7dde155997a1')]
        for number, ja, total, digest in expected:
            with self.subTest(segment=number):
                path = HERO / f'segment-{number:06d}/capture.opcap'
                report, rows = audit_capture(path, ignored)
                self.assertEqual(report['capture_sha256'], digest)
                self.assertEqual(report['counts']['records'], 13)
                self.assertEqual(report['roles']['self']['damage_dealt'], total)
                compared = [row for row in rows if row['attack_id'] in (2441204394, 3756260250, 3756260700)]
                self.assertEqual(len(compared), 9)
                self.assertTrue(all(row['ja_candidate'] is ja for row in compared))
                self.assertTrue(all(row['flags_raw'] == (0x49 if ja else 0x48) for row in compared))
                if ja:
                    self.assertEqual([row['signed_value'] for row in rows if row['signed_value'] > 0][-1], 157021)


if __name__ == '__main__':
    unittest.main()
