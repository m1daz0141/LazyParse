"""Saved-data coverage of exact name matches, including unknown IDs."""
from .catalog import get_catalog


def audit_report(report, capture_path):
    rows = {}
    catalog = get_catalog()

    def row(attack_id):
        return rows.setdefault(attack_id, {'capture_path': str(capture_path), 'attack_id': attack_id,
            'attack_id_hex': f'0x{attack_id:08X}', **catalog.labels(attack_id),
            'positive_hits': 0, 'positive_damage': 0, 'negative_records': 0, 'negative_signed_sum': 0})

    for entry in report.get('attacks', []):
        target = row(entry['attack_id'])
        target['positive_hits'] += entry['hits_dealt']
        target['positive_damage'] += entry['damage_dealt']
    for entry in report.get('negative_values', []):
        target = row(entry['attack_id'])
        target['negative_records'] += entry['count']
        target['negative_signed_sum'] += entry['signed_sum']
    values = [rows[key] for key in sorted(rows)]
    positive = sum(entry['positive_hits'] for entry in values)
    known = sum(entry['positive_hits'] for entry in values if entry['attack_name_matched'])
    return {'capture_path': str(capture_path), 'counts': report['counts'],
        'positive_hits': positive, 'named_positive_hits': known,
        'named_positive_fraction': known / positive if positive else None,
        'unknown_positive_ids': [entry['attack_id'] for entry in values
                                 if not entry['attack_name_matched'] and entry['positive_hits']],
        'attacks': values}
