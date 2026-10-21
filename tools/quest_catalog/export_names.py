"""Export Japanese quest titles from locally extracted text members.

Filename numbers are name-ID candidates until joined to a captured quest list.
Never updates quests.csv or the user's Quest.csv review sheet.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from .local_text import read_text

ROOT = Path(__file__).resolve().parents[2]
FIELDS = ('name_id_candidate', 'name', 'language', 'category', 'text_key',
          'source_archive', 'source_member', 'known_quest_id', 'status')


def export(directory: Path, destination: Path) -> dict:
    if not directory.is_dir():
        raise FileNotFoundError(f'Extracted text directory is absent: {directory}')
    if destination.exists():
        raise FileExistsError(f'Preserving existing file: {destination}')
    protected = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in (ROOT / 'Quest.csv', ROOT / 'quests.csv') if p.exists()}
    known = defaultdict(list)
    with (ROOT / 'quests.csv').open(encoding='utf-8-sig', newline='') as stream:
        for row in csv.DictReader(stream):
            known[row['name']].append(row['quest_id'])
    rows, members = [], {}
    for path in sorted(directory.rglob('quest_*.text')):
        number = path.stem.removeprefix('quest_')
        if not number.isascii() or not number.isdecimal():
            continue
        raw = path.read_bytes()
        members[str(path.relative_to(directory))] = hashlib.sha256(raw).hexdigest()
        for entry in read_text(raw):
            if entry.category != 'Basic' or entry.key != 'Title' or entry.language != 0:
                continue
            if not entry.text.strip():
                raise ValueError(f'Empty title: {path.name}')
            rows.append(dict(zip(FIELDS, (
                int(number), entry.text, entry.language, entry.category, entry.key,
                path.parent.name, path.name, '|'.join(known.get(entry.text, [])),
                'name_match_only' if entry.text in known else 'unverified_name_id'))))
    keys = [r['name_id_candidate'] for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError('Duplicate filename name-ID candidate; inspect archives before merging')
    rows.sort(key=lambda row: row['name_id_candidate'])
    if not rows:
        raise ValueError('No Japanese quest titles found; refusing an empty export')
    with destination.open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    if any(hashlib.sha256(p.read_bytes()).hexdigest() != digest for p, digest in protected.items()):
        raise ValueError('An existing dictionary changed while exporting')
    return {'output': str(destination.relative_to(ROOT)), 'title_count': len(rows),
            'unique_names': len({r['name'] for r in rows}),
            'known_name_matches': [r for r in rows if r['known_quest_id']],
            'name_id_verified_against_packets': False,
            'input_member_sha256': members,
            'protected_dictionaries_sha256': {p.name: digest for p, digest in protected.items()},
            'output_sha256': hashlib.sha256(destination.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--extracted', type=Path, default=Path(__file__).with_name('research') / 'extracted')
    parser.add_argument('--output', type=Path, default=ROOT / 'QuestNames.csv')
    parser.add_argument('--proof', type=Path, default=Path(__file__).with_name('results') / 'local-names.json')
    args = parser.parse_args()
    proof = export(args.extracted, args.output)
    args.proof.parent.mkdir(parents=True, exist_ok=True)
    args.proof.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: proof[key] for key in ('output', 'title_count', 'unique_names')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
