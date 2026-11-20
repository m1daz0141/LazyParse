"""Presentation only: preserve backend totals and identity evidence."""
from collections import defaultdict
from ..attack_catalog.catalog import attack_label, get_catalog
from ..combat_core.hit_rates import HitRates
from ..combat_core.damage_policy import dps_damage, included_totals
from .control import preparation_error

COLUMNS = [('name', 'Name', 112), ('share', '%', 38), ('damage_dealt', 'Damage', 76),
    ('damage_taken', 'Taken', 48), ('dps', 'DPS', 64), ('ja', 'JA %', 40),
    ('crit', 'Crit %', 42), ('max_hit', 'Max Hit', 65), ('attack', 'Attack', 97), ('hits_dealt', 'Hits', 44)]
DEFAULT_COLUMNS = [key for key, _, _ in COLUMNS if key != 'hits_dealt']


def number(value, decimals=0):
    return '—' if value is None else f'{value:,.{decimals}f}'


def percentage(value, decimals=2):
    return '—' if value is None else f'{number(value, decimals)}%'


def display_elapsed(report):
    timing = report.get('timing', {})
    if (timing.get('definition') == 'sum_of_first_to_last_positive_event_spans_per_encounter' and
            report.get('dps_mode') != 'damage'):
        return None  # Old aggregate JSON cannot recover a continuous end time.
    return timing.get('elapsed_seconds', 0)


def elapsed_label(elapsed, decimals=True):
    # Whole seconds show completed seconds; formatting must not round Time up.
    return number(elapsed, 2) if decimals else number(int(elapsed) if elapsed is not None else None)


def actor_rows(report, *, players_only=True, only_me=False, anonymize=False, sort='damage_dealt', descending=True,
               current_identities=None):
    groups = {}
    for actor in report.get('actors', []):
        if players_only and actor['kind'] != 'player':
            continue
        key = f"player:{actor['object_id']}" if actor['kind'] == 'player' else actor['actor_key']
        if key not in groups:
            groups[key] = {**actor, 'key': key, 'actor_keys': [], 'damage_dealt': 0, 'damage_taken': 0,
                           'hits_dealt': 0, 'hits_taken': 0, 'max_hit': 0, 'hit_rates': HitRates(),
                           'hp_unlinked_damage': 0, 'hp_unlinked_hits': 0}
        row = groups[key]
        row['actor_keys'].append(actor['actor_key'])
        if actor.get('name'):
            row['name'] = actor['name']
        for field in ('damage_dealt', 'damage_taken', 'hits_dealt', 'hits_taken'):
            row[field] += actor[field]
        for field in ('hp_unlinked_damage', 'hp_unlinked_hits'):
            row[field] += actor.get(field, 0)
        row['max_hit'] = max(row['max_hit'], actor.get('rated_max_hit', actor['max_hit']))
        row['hit_rates'].merge(HitRates.from_totals(actor))
    # The list shows only damage that lowered the fought enemy's HP; the
    # detail window keeps every hit (total_damage_dealt, attack_rows).
    for row in groups.values():
        row['total_damage_dealt'] = row['damage_dealt']
        row['damage_dealt'] = dps_damage(row)
    total = sum(row['damage_dealt'] for row in groups.values())
    elapsed = display_elapsed(report)
    rows = []
    names = {(entry['object_id'], entry['entity_type'], entry['map_id']): entry
             for entry in (current_identities or {}).get('entities', []) if entry.get('name')}
    # Anonymous labels are numbered by first appearance and never contain the
    # character ID, which would identify the player as well as the name.
    anonymous = {key: f'Player {number}' for number, key in enumerate(
        (key for key, row in groups.items() if row['kind'] == 'player' and row['role'] != 'self'), 1)}
    for key, row in groups.items():
        if only_me and row['role'] != 'self':
            continue
        row['share'] = row['damage_dealt'] * 100 / total if total else 0
        row['dps'] = row['damage_dealt'] / elapsed if elapsed else None
        current = names.get((row['object_id'], row['entity_type'], row['map_id']))
        if current and row['kind'] == 'player':
            row['name'] = current['name']
            row['name_evidence'] = current.get('name_evidence')
        row['name'] = row.get('name') or f"#{row['object_id']}"
        if anonymize:
            row['name'] = '自分' if row['role'] == 'self' else anonymous.get(key, row['name'])
        row['hit_rates'] = row['hit_rates'].as_dict()
        row['ja'] = row['hit_rates']['ja_percent']
        row['crit'] = row['hit_rates']['crit_percent']
        attacks = attack_rows(report, row)
        maximum = max(attacks, key=lambda attack: attack['rated_max_hit'], default=None)
        row['max_attack_id'] = maximum['attack_id'] if maximum and maximum['rated_max_hit'] else None
        row['attack'] = attack_label(row['max_attack_id']) if row['max_attack_id'] is not None else '—'
        rows.append(row)
    rows.sort(key=lambda row: (row.get(sort) is not None, row.get(sort) or 0, row['name']), reverse=descending)
    return rows


def attack_rows(report, actor):
    merged = {}
    for attack in report.get('attacks', []):
        if attack['credited_source_key'] not in actor['actor_keys']:
            continue
        key = attack['attack_id']
        row = merged.setdefault(key, {'attack_id': key, **get_catalog().labels(key),
                                     'damage_dealt': 0, 'hits_dealt': 0, 'max_hit': 0, 'min_hit': None,
                                     'min_complete': True, 'hit_rates': HitRates(), 'hp_unlinked_damage': 0,
                                     'rated_max_hit': 0})
        row['damage_dealt'] += attack['damage_dealt']
        row['hp_unlinked_damage'] += attack.get('hp_unlinked_damage', 0)
        row['hits_dealt'] += attack['hits_dealt']
        row['max_hit'] = max(row['max_hit'], attack['max_hit'])
        row['rated_max_hit'] = max(row['rated_max_hit'], attack.get('rated_max_hit', attack['max_hit']))
        if attack['hits_dealt']:
            if attack.get('min_hit') is None:
                row['min_complete'] = False
            else:
                row['min_hit'] = min(row['min_hit'], attack['min_hit']) if row['min_hit'] is not None else attack['min_hit']
        row['hit_rates'].merge(HitRates.from_totals(attack))
    for row in merged.values():
        if not row.pop('min_complete'):
            row['min_hit'] = None
        row['hit_rates'] = row['hit_rates'].as_dict()
        row['ja'] = row['hit_rates']['ja_percent']
        row['crit'] = row['hit_rates']['crit_percent']
    return sorted(merged.values(), key=lambda row: row['damage_dealt'], reverse=True)


def source_rows(report, actor):
    names = {entry['actor_key']: entry.get('name') for entry in report.get('actors', [])}
    names.update({entry['actor_key']: entry.get('name') for entry in report.get('identity_state', {}).get('entities', [])})
    merged = defaultdict(int)
    for source in report.get('raw_source_damage', []):
        if source['credited_source_key'] in actor['actor_keys']:
            merged[source['source_key']] += source['damage']
    return [{'source_key': key, 'name': names.get(key) or key, 'damage': value}
            for key, value in sorted(merged.items(), key=lambda item: item[1], reverse=True)]


def clipboard_summary(report, rows, *, time_decimals=True):
    elapsed = display_elapsed(report)
    mode = {'quest': 'クエスト中', 'damage': 'ダメージ計測'}.get(report.get('dps_mode'))
    lines = [f"Time {elapsed_label(elapsed, time_decimals)}s / Damage {sum(row['damage_dealt'] for row in rows):,}" +
             (f' / DPS方式 {mode}' if mode else '')]
    lines.extend(f"{row['name']}  {row['damage_dealt']:,}  ({row['share']:.1f}%)  {number(row['dps'], 1)} DPS"
                 f"  JA {percentage(row['ja'])}  Crit {percentage(row['crit'])}" for row in rows)
    return '\n'.join(lines)


def rate_tooltip(row, metric):
    counts = row['hit_rates']
    title = 'JA' if metric == 'ja' else 'Crit'
    lines = [f"{title}: {percentage(row[metric])}",
             f"成立 {counts[metric + '_hits']} / 対象 {counts[metric + '_eligible_hits']} ヒット"]
    if metric == 'ja':
        lines.append(f"対象外 {counts['ja_excluded_hits']}（ペット・タクト通常・設定した除外ID）")
    lines.append(f"未判定 {counts[metric + '_unknown_hits']}")
    lines.append('対象0件は—。古い集計JSONに判定情報がない場合は元OPCAPを開いて再集計できます。')
    return '\n'.join(lines)


def status_view(snapshot, *, saved=False):
    if saved:
        return '保存ログ', '#c5ac73'
    if snapshot.get('game_version_error'):
        return 'ゲームDLLの確認が必要', '#ef8b87'
    if preparation_error(snapshot):
        return '識別情報の取得エラー', '#ef8b87'
    if snapshot.get('effective_recording'):
        if snapshot.get('auto_save_error'):
            return '自動保存エラー・記録中', '#ef8b87'
        quest = (snapshot.get('measurement') or {}).get('report', {}).get('quest_measurement')
        if quest:
            if quest.get('rules_suspended'):
                return '自動判定保留・記録中', '#ddb577'
            return ('待機中' if quest.get('end_reason') else '計測中'), '#69c9b0'
        if snapshot.get('auto_quests_enabled'):
            view = snapshot.get('quest_view') or {}
            if not view.get('supported'):
                return '自動判定対象外・記録中', '#ddb577'
            if view.get('suspended'):
                return '自動判定保留・記録中', '#ddb577'
            counts = included_totals((snapshot.get('measurement') or {}).get('report', {}))
            return ('計測中' if counts.get('positive_records') else '待機中'), '#69c9b0'
        return '計測中', '#69c9b0'
    state = snapshot.get('state')
    if state == 'paused':
        return '待機中', '#c5ac73'
    if state == 'binding_ready_waiting_for_start':
        return '待機中', '#82aeb6'
    if state in {'read_error', 'writer_error', 'identity_context_error', 'capture_start_failed'}:
        return '取得エラー', '#ef8b87'
    if state in {'stale', 'identity_unavailable', 'waiting_for_binding', 'waiting_for_publication'}:
        return '取得確認中', '#ddb577'
    return '待機中', '#a4acb2'
