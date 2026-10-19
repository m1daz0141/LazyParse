"""Inspect historical JA/Crit masks in saved Classic OPCAP files, without game access."""
import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import sys

from .__main__ import replay
from .hit_flags import LEGACY_SOURCE, inspect_event, read_ja_exclusions

CSV_FIELDS = ['capture_path', 'sequence', 'filetime', 'qpc', 'source_id', 'source_kind', 'source_name',
    'credited_source_id', 'credited_role', 'target_id', 'target_kind', 'attack_id', 'attack_name',
    'signed_value', 'value_kind', 'included_in_damage', 'flags_hex', 'flags_binary',
    'legacy_ja_bit', 'legacy_crit_bit', 'other_bits_raw', 'ja_eligible_candidate', 'ja_scope_reason',
    'ja_candidate', 'crit_candidate', 'ja_current_verified', 'crit_current_verified']


def audit_capture(path, ignored_ids):
    path = Path(path).resolve()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    rows = []

    def on_event(event):
        rows.append({'capture_path': str(path), 'sequence': event['sequence'], 'filetime': event['filetime'],
            'qpc': event['qpc'], 'source_id': event['source']['object_id'], 'source_kind': event['source']['kind'],
            'source_name': event['source'].get('name'), 'credited_source_id': event['credited_source']['object_id'],
            'credited_role': event['credited_source']['role'], 'target_id': event['target']['object_id'],
            'target_kind': event['target']['kind'], 'attack_id': event['attack_id'],
            'attack_name': event['attack_name'], 'signed_value': event['signed_value'],
            'value_kind': event['value_kind'], 'included_in_damage': event['included_in_damage'],
            **inspect_event(event, ignored_ids)})

    report = replay(path, on_event=on_event)
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    if before != after:
        raise ValueError('Capture changed during offline audit; finalize recording first')
    return {'capture_path': str(path), 'capture_sha256': before, 'source_unchanged': True,
            'counts': report['counts'], 'roles': report['roles'], 'summary': summarize(rows)}, rows


def summarize(rows):
    histogram = Counter()
    scopes = Counter()
    bits = [0] * 8
    groups = defaultdict(Counter)
    for row in rows:
        histogram[row['flags_hex']] += 1
        scopes[row['ja_scope_reason']] += 1
        for bit in range(8):
            bits[bit] += bool(row['flags_raw'] & (1 << bit))
        key = (row['value_kind'], row['source_kind'], row['attack_id'], row['attack_name'], row['flags_hex'])
        groups[key]['records'] += 1
        groups[key]['signed_sum'] += row['signed_value']
    return {'records': len(rows), 'flags_histogram': dict(sorted(histogram.items())),
        'bit_set_counts': {f'0x{1 << bit:02X}': value for bit, value in enumerate(bits)},
        'ja_scope_counts': dict(sorted(scopes.items())),
        'ja_eligible_candidate_set': sum(row['ja_candidate'] is True for row in rows),
        'ja_eligible_candidate_unset': sum(row['ja_candidate'] is False for row in rows),
        'crit_positive_candidate_set': sum(row['crit_candidate'] is True for row in rows),
        'crit_positive_candidate_unset': sum(row['crit_candidate'] is False for row in rows),
        'groups': [{'value_kind': key[0], 'source_kind': key[1], 'attack_id': key[2],
            'attack_name': key[3], 'flags_hex': key[4], **value}
            for key, value in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1], item[0][2], item[0][4]))]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('captures', nargs='+', type=Path)
    parser.add_argument('--output', type=Path, help='new JSON summary file')
    parser.add_argument('--csv', type=Path, help='new event-level UTF-8 BOM CSV')
    args = parser.parse_args()
    try:
        outputs = [path.resolve() for path in (args.output, args.csv) if path]
        if len(outputs) != len(set(outputs)) or any(path.exists() for path in outputs):
            raise ValueError('Output paths must be distinct new files')
        ignored_ids, policy = read_ja_exclusions()
        captures, all_rows = [], []
        for path in args.captures:
            capture, rows = audit_capture(path, ignored_ids)
            captures.append(capture)
            all_rows.extend(rows)
        result = {'schema_version': 1, 'purpose': 'offline_legacy_flag_candidates_only',
            'legacy_definition': LEGACY_SOURCE, 'ja_exclusions': policy,
            'policy': {'pet_ja': 'not_applicable_before_owner_credit',
                'takt_normal_ja': 'excluded_as_pet_related', 'attack_id_zero_ja': 'unknown',
                'non_positive_values': 'excluded_from_ja_and_crit_candidates',
                'crit_counts_include_enemy_sources': True,
                'current_client_verified': False, 'production_gui_percentages_enabled': False},
            'summary': summarize(all_rows), 'captures': captures}
        content = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        if args.output:
            with args.output.open('x', encoding='utf-8') as stream:
                stream.write(content)
        if args.csv:
            with args.csv.open('x', encoding='utf-8-sig', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(all_rows)
        if not args.output:
            print(content, end='')
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
