"""Take a read-only catalog snapshot without controlling the running game."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re

from tools.combat_core.shared_files import open_read, read_text
from .packets import join_names, read_catalog

ROOT = Path(__file__).resolve().parents[2]
RESULTS = Path(__file__).with_name('results')


def snapshot(label: str) -> dict:
    if not re.fullmatch('[a-z0-9_-]+', label):
        raise ValueError('Invalid snapshot label')
    destination = RESULTS / label
    destination.mkdir(exist_ok=False)
    pointer_path = RESULTS / 'current.json'
    current = json.loads(read_text(pointer_path))
    runtime = json.loads(read_text(Path(current['session']) / 'autostart-state.json'))
    run = Path(runtime['run_directory'])
    context_dir = run / 'identity-context'
    status = json.loads(read_text(context_dir / 'quest-catalog-status.json'))
    context = json.loads(read_text(context_dir / 'context-status.json'))
    if status['source_sha256'] != 'b2a87ec9000caa7667677d22f51859c449f4bb686f10b876370be7b861243332':
        raise ValueError('Unsupported catalog source')
    with open_read(context_dir / 'quest-catalog.opcat') as stream:
        raw = stream.read()
    (destination / 'quest-catalog.opcat').write_bytes(raw)
    (destination / 'received-state.json').write_text(json.dumps(
        {'runtime': runtime, 'context': context, 'catalog_status': status}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    records = read_catalog(raw, allow_partial=True)
    with (ROOT / 'QuestNames.csv').open(encoding='utf-8-sig', newline='') as stream:
        names = {int(row['name_id_candidate']): row['name'] for row in csv.DictReader(stream)}
    rows = join_names(records, names)
    with (ROOT / 'quests.csv').open(encoding='utf-8-sig', newline='') as stream:
        known = {int(row['quest_id']): row['name'] for row in csv.DictReader(stream)}
    anchors = []
    for row in rows:
        if row['quest_id'] in known:
            anchors.append({key: row[key] for key in ('quest_id', 'name_id', 'name', 'status')})
            if row['name'] != known[row['quest_id']]:
                raise ValueError(f"Known quest name disagrees with packet: {row['quest_id']}")
    fields = ('quest_id', 'quest_entity_type', 'quest_map_id', 'name_id', 'name', 'category_id', 'status', 'packet_ids', 'sequences')
    with (destination / 'quest-catalog.csv').open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader(); writer.writerows(rows)
    result = {
        'runtime': runtime, 'context': context, 'catalog_status': status,
        'records_in_snapshot': len(records), 'objects': len(rows),
        'matched': sum(row['status'] == 'matched' for row in rows),
        'category_ids': sorted({row['category_id'] for row in rows if row['category_id'] is not None}),
        'known_anchors': anchors,
        'packet_sizes': [{'sequence': r.sequence, 'bytes': len(r.packet), 'minor': r.packet[6]} for r in records],
        'snapshot_sha256': hashlib.sha256(raw).hexdigest(),
        'names_sha256': hashlib.sha256((ROOT / 'QuestNames.csv').read_bytes()).hexdigest(),
        'source': str(context_dir / 'quest-catalog.opcat'),
        'partial_tail_allowed': True,
    }
    (destination / 'snapshot.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    current.update(pid=runtime['pid'], run_directory=str(run), latest_snapshot=str(destination / 'snapshot.json'))
    pointer_path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('label')
    result = snapshot(parser.parse_args().label)
    print(json.dumps({key: result[key] for key in ('objects', 'matched', 'category_ids', 'known_anchors')}, ensure_ascii=False))
