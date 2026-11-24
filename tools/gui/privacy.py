"""Other players' details stay hidden unless LazyParse starts with --overparse.

A restricted report keeps the player's own figures and folds every other
player into one "その他合計" actor that only carries damage and hit totals, so the overall total and the player's share are
unchanged. Restricted reports are what gets saved, exported and shown.
"""
from ..combat_core.hit_rates import HitRates

MARK = 'others_merged'
OTHERS_ROLE = 'others_total'
OTHERS_NAME = 'その他合計'
TOTAL_FIELDS = ('damage_dealt', 'hits_dealt', 'damage_taken', 'hits_taken', 'hp_unlinked_damage', 'hp_unlinked_hits')
_full_access = False


def configure(full_access):
    global _full_access
    _full_access = bool(full_access)


def full_access():
    return _full_access


def is_restricted(report):
    return bool((report or {}).get('privacy', {}).get(MARK))


def _ref(entry):
    return (entry.get('object_id'), entry.get('entity_type'), entry.get('map_id'))


def _hidden(entry):
    return entry.get('kind') == 'player' and entry.get('role') != 'self'


def _owned_by_other(entry, own):
    owner = entry.get('owner')
    return bool(owner) and _ref(owner) != own


def _totals(entries):
    return {field: sum(entry.get(field, 0) or 0 for entry in entries) for field in TOTAL_FIELDS}


def _others_actor(entries):
    first = entries[0]
    return {'capture_id': first.get('capture_id'), 'key': OTHERS_ROLE, 'object_id': 0,
            'entity_type': first.get('entity_type'), 'map_id': 0, 'actor_key': OTHERS_ROLE,
            'scene': None, 'instance': 0, 'kind': 'player', 'role': OTHERS_ROLE, 'evidence': None,
            'self_evidence': None, 'name': OTHERS_NAME, 'name_kind': None, 'name_evidence': None,
            'owner': None, 'ownership_evidence': None, 'ownership_active': False, **_totals(entries),
            'max_hit': 0, 'rated_max_hit': 0, 'hit_rates': HitRates().as_dict(), 'dps': None}


def restrict_report(report):
    """Copy of report without other players' identities, attacks or rates."""
    if not isinstance(report, dict) or _full_access or is_restricted(report) or 'actors' not in report:
        return report
    state = report.get('identity_state') or {}
    own = _ref(state['self']) if state.get('self') else None
    hidden = [actor for actor in report['actors'] if _hidden(actor)]
    keys = {actor['actor_key'] for actor in hidden}
    entities = []
    for entry in state.get('entities', []):
        if _hidden(entry) or _owned_by_other(entry, own):
            keys.add(entry.get('actor_key'))
        else:
            entities.append(entry)
    actors = [actor for actor in report['actors'] if not _hidden(actor)]
    if hidden:
        actors.append(_others_actor(hidden))
    roles = {name: ({field: value.get(field, 0) for field in TOTAL_FIELDS} if name not in {'self', 'enemy'} else value)
             for name, value in (report.get('roles') or {}).items()}
    result = {**report, 'actors': actors, 'roles': roles,
              'attacks': [a for a in report.get('attacks', []) if a.get('credited_source_key') not in keys],
              'raw_source_damage': [r for r in report.get('raw_source_damage', [])
                                    if r.get('credited_source_key') not in keys and r.get('source_key') not in keys],
              'negative_values': [n for n in report.get('negative_values', [])
                                  if n.get('source_key') not in keys and n.get('target_key') not in keys],
              'privacy': {MARK: True}}
    if state:
        result['identity_state'] = {**state, 'entities': entities}
    return result


def restrict_identity(state):
    if not isinstance(state, dict) or _full_access:
        return state
    own = _ref(state['self']) if state.get('self') else None
    return {**state, 'entities': [entry for entry in state.get('entities', [])
                                  if not _hidden(entry) and not _owned_by_other(entry, own)]}


def restrict_snapshot(snapshot):
    """Restrict every report a GUI snapshot carries (live, history, quest view)."""
    if not isinstance(snapshot, dict) or _full_access:
        return snapshot
    result = dict(snapshot)
    for key in ('measurement', 'capture_measurement'):
        if isinstance(result.get(key), dict) and result[key].get('report'):
            result[key] = {**result[key], 'report': restrict_report(result[key]['report'])}
    if result.get('quest_history'):
        result['quest_history'] = [{**entry, 'report': restrict_report(entry.get('report'))}
                                   for entry in result['quest_history']]
    view = result.get('quest_view')
    if isinstance(view, dict):
        view = dict(view)
        if view.get('current'):
            view['current'] = restrict_report(view['current'])
        if view.get('history'):
            view['history'] = [restrict_report(report) for report in view['history']]
        result['quest_view'] = view
    if result.get('history'):
        history = []
        for entry in result['history']:
            entry = dict(entry)
            if entry.get('report'):
                entry['report'] = restrict_report(entry['report'])
                entry['roles'] = entry['report'].get('roles', {})
            elif entry.get('roles'):
                entry['roles'] = restrict_report({'actors': [], 'roles': entry['roles']})['roles']
            if entry.get('quest_reports'):
                entry['quest_reports'] = [restrict_report(report) for report in entry['quest_reports']]
            history.append(entry)
        result['history'] = history
    if isinstance(result.get('identity'), dict) and result['identity'].get('state'):
        result['identity'] = {**result['identity'], 'state': restrict_identity(result['identity']['state'])}
    return result
