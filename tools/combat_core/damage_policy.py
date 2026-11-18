"""Damage selection independent of editable attack display names."""

# Classic Burn tick IDs, levels 1 through 5. Keep player/owner-attributed ticks;
# source ID zero cannot identify who inflicted the effect.
BURN_ATTACK_IDS = frozenset({2505928570, 2505928505, 2505928696, 2505928584, 2505927753})
SOURCE_ZERO_BURN = 'source_zero_burn_unattributable'
ZERO_ATTACK_ID = 'zero_attack_id'


def exclusion_reason(event):
    if event.value <= 0:
        return 'non_positive_value'
    if event.attack_id == 0:
        return ZERO_ATTACK_ID
    if event.source.object_id == 0 and event.attack_id in BURN_ATTACK_IDS:
        return SOURCE_ZERO_BURN
    return None


def included_totals(report):
    """Old JSON lacks selection totals; only its original OPCAP can be refiltered."""
    return report.get('damage_totals', report.get('counts', {}))
