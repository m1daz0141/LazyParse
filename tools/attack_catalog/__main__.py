"""Audit saved Classic captures against config/skills.csv."""
import argparse
import csv
import json
from pathlib import Path
import sys

from .audit import audit_report
from .catalog import get_catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('captures', nargs='+', type=Path, help='.opcap or exported combat report .json')
    parser.add_argument('--output', type=Path, help='write an audit JSON to a new file')
    parser.add_argument('--csv', type=Path, help='write ID/name/count/damage rows to a new UTF-8 BOM CSV')
    args = parser.parse_args()
    try:
        outputs = [path.resolve() for path in (args.output, args.csv) if path]
        if len(outputs) != len(set(outputs)):
            raise ValueError('Output paths must be different')
        if any(path.exists() for path in outputs):
            raise ValueError('Output already exists; choose a new filename')
        results = []
        for capture in args.captures:
            if capture.suffix.casefold() == '.json':
                report = json.loads(capture.read_text(encoding='utf-8-sig'))
                if not isinstance(report, dict) or report.get('schema_version') != 1 or 'counts' not in report or 'attacks' not in report:
                    raise ValueError('Not a supported combat report')
            else:
                # Lazy import keeps the catalog independent from capture decoding.
                from ..combat_core.__main__ import replay
                report = replay(capture)
            results.append(audit_report(report, capture.resolve()))
        result = {'schema_version': 1, 'attack_catalog': get_catalog().metadata(), 'captures': results}
        content = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        if args.output:
            with args.output.open('x', encoding='utf-8') as stream:
                stream.write(content)
        if args.csv:
            with args.csv.open('x', encoding='utf-8-sig', newline='') as stream:
                fields = ['capture_path', 'attack_id', 'attack_id_hex', 'attack_name', 'attack_kind', 'attack_note',
                          'attack_name_matched', 'positive_hits', 'positive_damage', 'negative_records', 'negative_signed_sum']
                writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
                writer.writeheader()
                for capture in results:
                    writer.writerows(capture['attacks'])
        if not args.output:
            print(content, end='')
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
