"""Exact unsigned-ID lookup, independent from combat classification."""
import csv
from dataclasses import dataclass
from functools import lru_cache
import io
from pathlib import Path
import re
from types import MappingProxyType
from ..paths import CONFIG

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
    def __init__(self, entries=None, error=None, *, path=None):
        self.entries = MappingProxyType(dict(entries or {}))
        self.error = error
        self.path = str(path) if path is not None else None

    @classmethod
    def load(cls, path=None):
        path = Path(path) if path is not None else EDITABLE_CSV
        try:
            raw = path.read_bytes()
            entries = parse_csv(raw.decode('utf-8-sig'))
        except (OSError, ValueError, csv.Error) as error:
            return cls(error=str(error), path=path)
        return cls(entries, path=path)

    def labels(self, attack_id):
        entry = self.entries.get(attack_id)
        return {'attack_name': entry.name if entry else None,
            'attack_kind': entry.kind if entry else None,
            'attack_note': entry.note if entry else None,
            'attack_name_matched': entry is not None,
            'attack_name_source': {'path': self.path, 'line': entry.line} if entry and self.path else None}

    def metadata(self):
        return {'schema_version': 1, 'available': self.error is None and self.path is not None,
                'entries': len(self.entries), 'path': self.path, 'error': self.error}


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
