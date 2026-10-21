"""Exact unsigned-ID lookup, independent from combat classification."""
from copy import deepcopy
import csv
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import io
import json
from pathlib import Path
import re
from types import MappingProxyType
from ..paths import CONFIG

DATA = Path(__file__).resolve().parent / 'data'
EDITABLE_CSV = CONFIG / 'skills.csv'
KINDS = frozenset({'Melee', 'Ranged', 'Tech', 'PA', 'Special', 'Status', 'Damage', 'AISV', 'Unknown'})


@dataclass(frozen=True)
class AttackName:
    name: str
    kind: str | None
    note: str | None
    line: int
    extra_fields: tuple


def parse_csv(text):
    """Column 3 also contains free-form notes in this source; do not guess kinds."""
    entries = {}
    reader = csv.reader(io.StringIO(text, newline=''), strict=True)
    previous_line = 0
    for row in reader:
        line = previous_line + 1
        previous_line = reader.line_num
        if not row or not any(value.strip() for value in row):
            continue
        name = row[0].strip()
        if name.casefold() == '(comment)' or row[:2] == ['Type', 'ID']:
            continue
        if len(row) < 2 or not name or not re.fullmatch(r'[0-9]+', row[1].strip()):
            raise ValueError(f'Invalid attack mapping row: {row!r}')
        attack_id = int(row[1])
        if not 0 <= attack_id <= 0xFFFFFFFF:
            raise ValueError('Attack ID is outside unsigned 32-bit range')
        extra = tuple(value.strip() for value in row[2:])
        kind = extra[0] if extra and extra[0] in KINDS else None
        notes = extra[1:] if kind else extra
        entry = AttackName(name, kind, ' / '.join(value for value in notes if value) or None, line, extra)
        # Conflicting duplicates must not silently rename an attack.
        previous = entries.get(attack_id)
        if previous and (previous.name, previous.extra_fields) != (entry.name, entry.extra_fields):
            raise ValueError(f'Conflicting names for attack ID {attack_id}')
        if previous is None:
            entries[attack_id] = entry
    return entries


class AttackCatalog:
    def __init__(self, entries=None, source=None, error=None, *, entry_sources=None, editable=None):
        self.entries = MappingProxyType(dict(entries or {}))
        self.source = deepcopy(source)
        self.error = error
        self.entry_sources = dict(entry_sources or {})
        self.editable = deepcopy(editable)

    @classmethod
    def load(cls, directory=DATA, *, editable=None):
        directory = Path(directory)
        result = cls._load_pinned(directory)
        editable = Path(editable) if editable is not None else (
            EDITABLE_CSV if directory == DATA else directory / 'skills.csv')
        try:
            raw = editable.read_bytes()
            entries = parse_csv(raw.decode('utf-8-sig'))
        except FileNotFoundError:
            return result  # Optional; retain the pinned dictionary when absent.
        except (OSError, ValueError, csv.Error) as error:
            result.error = '; '.join(filter(None, [result.error, f'{editable}: {error}']))
            result.editable = {'path': str(editable), 'available': False}
            return result
        metadata = {'path': str(editable), 'available': True, 'entries': len(entries),
                    'sha256': hashlib.sha256(raw).hexdigest(), 'manually_editable': True}
        return cls({**result.entries, **entries}, result.source, result.error,
                   entry_sources={key: {'path': str(editable), 'line': entry.line} for key, entry in entries.items()},
                   editable=metadata)

    @classmethod
    def _load_pinned(cls, directory):
        """A missing/corrupt label file must not stop capture or change totals."""
        try:
            source = json.loads((directory / 'source.json').read_text(encoding='utf-8'))
            if (source.get('schema_version') != 1 or source.get('path') != 'skills_ja.csv' or
                    source.get('language') != 'ja'):
                raise ValueError('Unsupported attack catalog manifest')
            raw = (directory / source['path']).read_bytes()
            if hashlib.sha256(raw).hexdigest() != source['sha256']:
                raise ValueError('Attack catalog SHA-256 differs from the pinned source')
            text = raw.decode('utf-8-sig')
            entries = parse_csv(text)
            if len(entries) != source['entries']:
                raise ValueError('Attack catalog entry count differs from its manifest')
            return cls(entries, source)
        except (OSError, ValueError, TypeError, AttributeError, KeyError, csv.Error) as error:
            return cls(error=str(error))

    def labels(self, attack_id):
        entry = self.entries.get(attack_id)
        return {'attack_name': entry.name if entry else None,
            'attack_kind': entry.kind if entry else None,
            'attack_note': entry.note if entry else None,
            'attack_name_matched': entry is not None,
            'attack_name_source': (deepcopy(self.entry_sources.get(attack_id)) or
                                  ({'path': self.source['path'], 'line': entry.line} if self.source else None)) if entry else None}

    def metadata(self):
        return {'schema_version': 1, 'available': bool(self.entries), 'entries': len(self.entries),
                'source': deepcopy(self.source), 'editable': deepcopy(self.editable), 'error': self.error,
                'match_method': 'exact_uint32_id', 'current_client_all_entries_verified': False}


@lru_cache(maxsize=1)
def get_catalog():
    return AttackCatalog.load()


def attack_label(attack_id):
    return get_catalog().labels(attack_id)['attack_name'] or f'不明 (0x{attack_id:08X})'


def with_attack_names(report):
    """Add labels to a detached report; retain every ID, row, and numeric value."""
    catalog = get_catalog()
    return {**report, 'attack_catalog': catalog.metadata(), **{
        table: [{**entry, **catalog.labels(entry['attack_id'])} for entry in report[table]]
        for table in ('attacks', 'raw_source_damage', 'negative_values') if table in report}}
