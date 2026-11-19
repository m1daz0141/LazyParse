"""Damage selection independent of editable attack display names."""
import csv
import hashlib
import io
from pathlib import Path

from ..paths import CONFIG

# Classic Burn tick IDs, levels 1 through 5. Keep player/owner-attributed ticks;
# source ID zero cannot identify who inflicted the effect.
BURN_ATTACK_IDS = frozenset({2505928570, 2505928505, 2505928696, 2505928584, 2505927753})
SOURCE_ZERO_BURN = 'source_zero_burn_unattributable'
CONFIGURED_EXCLUSION = 'skills_exclusions_csv'
# Attack IDs excluded from aggregation for every source, edited by the user.
# The bundled default lists attack ID 0; the runtime copy lives in config/.
DEFAULT_EXCLUSIONS_PATH = Path(__file__).resolve().parents[1] / 'attack_catalog/skills_exclusions.csv'
EXCLUSIONS_PATH = CONFIG / 'skills_exclusions.csv'
HEADER = ['(comments)', '(attack_id)']


def parse_exclusions(text):
    """Rows are ``comment,attack_id``; the ID is decimal or 0x hexadecimal."""
    ids = []
    reader = csv.reader(io.StringIO(text, newline=''), strict=True)
    for row in reader:
        values = [value.strip() for value in row]
        while values and not values[-1]:
            values.pop()
        if not values or [v.casefold() for v in values] == HEADER:
            continue
        if len(values) != 2:
            raise ValueError(f'{reader.line_num}行目: 「備考,攻撃ID」の2列で指定してください'
                             '（備考にカンマを含める場合は "" で囲んでください）。')
        text_id = values[1]
        try:
            attack_id = int(text_id, 16) if text_id[:2].lower() == '0x' else int(text_id, 10)
        except ValueError:
            raise ValueError(f'{reader.line_num}行目: 攻撃IDが数値ではありません: {text_id}') from None
        if not 0 <= attack_id <= 0xFFFFFFFF:
            raise ValueError(f'{reader.line_num}行目: 攻撃IDが範囲外です: {text_id}')
        ids.append(attack_id)
    return ids


def _load(path):
    raw = Path(path).read_bytes()
    ids = parse_exclusions(raw.decode('utf-8-sig'))
    return frozenset(ids), {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(),
                            'rows': len(ids), 'attack_ids': sorted(set(ids))}


def read_damage_exclusions(path=None):
    """Return (ids, metadata). A broken user file falls back to the bundled default."""
    path = Path(path) if path is not None else EXCLUSIONS_PATH if EXCLUSIONS_PATH.is_file() else DEFAULT_EXCLUSIONS_PATH
    try:
        ids, metadata = _load(path)
        return ids, {**metadata, 'available': True, 'error': None, 'fallback': None}
    except (OSError, ValueError, UnicodeDecodeError, csv.Error) as error:
        message = f'{path}: {error}'
    try:
        ids, metadata = _load(DEFAULT_EXCLUSIONS_PATH)
        return ids, {**metadata, 'available': False, 'error': message, 'fallback': 'bundled_default'}
    except (OSError, ValueError, UnicodeDecodeError, csv.Error) as error:
        return frozenset(), {'path': str(path), 'available': False, 'error': f'{message}; {error}',
                             'fallback': None, 'attack_ids': []}


def exclusion_reason(event, excluded_ids=frozenset()):
    if event.value <= 0:
        return 'non_positive_value'
    if event.attack_id in excluded_ids:
        return CONFIGURED_EXCLUSION
    if event.source.object_id == 0 and event.attack_id in BURN_ATTACK_IDS:
        return SOURCE_ZERO_BURN
    return None


def included_totals(report):
    """Old JSON lacks selection totals; only its original OPCAP can be refiltered."""
    return report.get('damage_totals', report.get('counts', {}))
