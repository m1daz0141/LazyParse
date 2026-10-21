"""Offline reader for the optional quest catalog stream, never a sender.

Candidate Classic layout: pso2-protocol-lib commit
330d957be9244a31ffff074966baf5c16f0f8c92, questlist.rs and read_magic.
The present JP client still needs a real category response for validation.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import struct
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from tools.combat_core.shared_files import open_read

HEADER = struct.Struct('<8sIIQQ')
RECORD = struct.Struct('<QQQII')
MAX_PACKET = 262144


@dataclass(frozen=True)
class CatalogRecord:
    sequence: int
    filetime: int
    qpc: int
    packet: bytes


def read_catalog(data: bytes, *, allow_partial: bool = False) -> list[CatalogRecord]:
    if len(data) < HEADER.size:
        if allow_partial:
            return []
        raise ValueError('Truncated catalog header')
    magic, version, size, frequency, _ = HEADER.unpack_from(data)
    if (magic, version, size) != (b'QCAT0001', 1, RECORD.size) or not frequency:
        raise ValueError('Unsupported catalog header')
    pos, result, sequences = HEADER.size, [], set()
    while pos < len(data):
        if len(data) - pos < RECORD.size:
            if allow_partial:
                break
            raise ValueError('Truncated catalog record header')
        seq, filetime, qpc, size, reserved = RECORD.unpack_from(data, pos)
        if not seq or seq in sequences or reserved or not 8 <= size <= MAX_PACKET or size % 4:
            raise ValueError('Invalid catalog record')
        if len(data) - pos - RECORD.size < size:
            if allow_partial:
                break
            raise ValueError('Truncated catalog packet')
        packet = data[pos + RECORD.size:pos + RECORD.size + size]
        minor = struct.unpack_from('<H', packet, 6)[0]
        if struct.unpack_from('<I', packet)[0] != size or packet[5] != 11 or minor not in (24, 26, 27, 28):
            raise ValueError('Packet outside catalog allowlist or size mismatch')
        sequences.add(seq)
        result.append(CatalogRecord(seq, filetime, qpc, packet))
        pos += RECORD.size + size
    return result


def parse_quests(packet: bytes) -> list[dict]:
    if len(packet) < 8 or struct.unpack_from('<I', packet)[0] != len(packet) or packet[5] != 11:
        raise ValueError('Invalid quest packet header')
    minor = struct.unpack_from('<H', packet, 6)[0]
    if minor in (27, 28):
        if len(packet) != 8:
            raise ValueError('Unexpected stopper body')
        return []
    layout = {24: (1076, 0x1db0, 0xc5), 26: (404, 0x292c, 0x5b)}.get(minor)
    if not layout or len(packet) < 12:
        raise ValueError('Unsupported quest list packet')
    stride, xor, sub = layout
    count = ((struct.unpack_from('<I', packet, 8)[0] ^ xor) - sub) & 0xffffffff
    if count > (MAX_PACKET - 12) // stride or len(packet) != 12 + count * stride:
        raise ValueError(f'Quest list count/stride mismatch: {count} x {stride}, bytes={len(packet)}')
    rows = []
    for index in range(count):
        base = 12 + index * stride
        quest, reserved, entity_type, map_id, name_id = struct.unpack_from('<IIHHI', packet, base + 32)
        # A real ARKS list contains object ID 0 with type 11 and a valid name
        # ID. Preserve it for review; do not infer a gameplay mapping for it.
        rows.append({'quest_id': quest, 'quest_entity_type': entity_type, 'quest_map_id': map_id,
                     'quest_reserved': reserved, 'name_id': name_id,
                     'category_id': packet[base + 255] if minor == 24 else None,
                     'source_packet': f'0b:{minor:02x}', 'entry_index': index})
    return rows


def join_names(records: list[CatalogRecord], names: dict[int, str]) -> list[dict]:
    by_key = {}
    objects = defaultdict(set)
    for record in records:
        for entry in parse_quests(record.packet):
            key = (entry['quest_id'], entry['quest_entity_type'], entry['quest_map_id'], entry['name_id'])
            objects[key[:3]].add(entry['name_id'])
            if key not in by_key:
                by_key[key] = {**entry, 'name': names.get(entry['name_id'], ''), 'sequences': [], 'packet_ids': set()}
            merged = by_key[key]
            merged['sequences'].append(record.sequence)
            merged['packet_ids'].add(entry['source_packet'])
            if entry['category_id'] is not None:
                if merged['category_id'] is not None and merged['category_id'] != entry['category_id']:
                    raise ValueError('Conflicting quest categories')
                merged['category_id'] = entry['category_id']
    rows = []
    for key, entry in sorted(by_key.items()):
        status = ('conflicting_name_ids' if len(objects[key[:3]]) > 1 else
                  'unexpected_object_type' if entry['quest_entity_type'] != 11 or entry['quest_reserved'] else
                  'zero_quest_id_unverified' if entry['quest_id'] == 0 else
                  'missing_name_id' if entry['name_id'] == 0 else
                  'name_not_found' if not entry['name'] else 'matched')
        rows.append({**entry, 'status': status, 'sequences': '|'.join(map(str, entry['sequences'])),
                     'packet_ids': '|'.join(sorted(entry['packet_ids']))})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', type=Path)
    parser.add_argument('--names', type=Path, default=Path(__file__).resolve().parents[2] / 'QuestNames.csv')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--allow-partial', action='store_true')
    args = parser.parse_args()
    with args.names.open(encoding='utf-8-sig', newline='') as stream:
        names = {}
        for row in csv.DictReader(stream):
            number = int(row['name_id_candidate'])
            if number in names and names[number] != row['name']:
                raise ValueError('Conflicting names in local CSV')
            names[number] = row['name']
    with open_read(args.capture) as stream:
        raw = stream.read()
    records = read_catalog(raw, allow_partial=args.allow_partial)
    rows = join_names(records, names)
    fields = ('quest_id', 'quest_entity_type', 'quest_map_id', 'name_id', 'name', 'category_id', 'status', 'packet_ids', 'sequences')
    with args.output.open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader(); writer.writerows(rows)
    print(json.dumps({'records': len(records), 'quest_objects': len(rows),
                      'matched': sum(r['status'] == 'matched' for r in rows),
                      'capture_sha256': hashlib.sha256(raw).hexdigest()}, ensure_ascii=False))


if __name__ == '__main__':
    main()
