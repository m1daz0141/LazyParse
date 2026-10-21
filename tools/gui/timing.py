"""Select a DPS denominator without mutating capture or quest boundaries."""
from copy import deepcopy
from fractions import Fraction
from ..combat_core.damage_policy import included_totals

QUEST_DEFINITION = 'continuous_from_first_player_damage_including_idle_and_map_time'
DAMAGE_DEFINITION = 'sum_of_first_to_last_positive_event_spans_per_encounter'
DPS_MODES = ('quest', 'damage')


def with_dps_mode(report, mode):
    if mode not in DPS_MODES:
        raise ValueError('Unknown DPS mode')
    result = deepcopy(report)
    alternatives = result.get('timing_modes')
    if alternatives is None:
        original = deepcopy(result.get('timing', {}))
        quest = original if original.get('definition') != DAMAGE_DEFINITION else {
            'definition': QUEST_DEFINITION, 'elapsed_seconds': None,
            'unavailable_reason': 'open_original_opcap_to_recover_continuous_time'}
        if original.get('definition') == DAMAGE_DEFINITION:
            damage = original
        else:
            encounters = result.get('encounters')
            elapsed = (float(sum((Fraction(e['last_qpc'] - e['first_qpc'], e['qpc_frequency'])
                                  for e in encounters), Fraction())) if encounters is not None else None)
            damage = {'definition': DAMAGE_DEFINITION, 'elapsed_seconds': elapsed,
                      'idle_timeout_seconds': original.get('idle_timeout_seconds'),
                      'zero_duration_dps': None}
        alternatives = {'quest': quest, 'damage': damage}
    result['timing_modes'] = deepcopy(alternatives)
    result['dps_mode'] = mode
    result['timing'] = deepcopy(alternatives[mode])
    elapsed = result['timing'].get('elapsed_seconds')
    result['timing']['dps'] = included_totals(result).get('positive_sum', 0) / elapsed if elapsed else None
    for group in [*result.get('actors', []), *result.get('attacks', []), *result.get('roles', {}).values()]:
        group['dps'] = group['damage_dealt'] / elapsed if elapsed else None
    return result
