"""Hit-count rates using Classic masks, with explicit applicability and evidence."""
from dataclasses import dataclass

from .hit_flags import OFFSET, JA_MASK, CRIT_MASK, inspect_event


COUNT_FIELDS = ('ja_hits', 'ja_eligible_hits', 'ja_excluded_hits', 'ja_unknown_hits',
                'crit_hits', 'crit_eligible_hits', 'crit_unknown_hits')


@dataclass
class HitRates:
    ja_hits: int = 0
    ja_eligible_hits: int = 0
    ja_excluded_hits: int = 0
    ja_unknown_hits: int = 0
    crit_hits: int = 0
    crit_eligible_hits: int = 0
    crit_unknown_hits: int = 0

    def add_hit(self, flags):
        if flags['ja_eligible'] is True:
            self.ja_eligible_hits += 1
            self.ja_hits += int(flags['ja'])
        elif flags['ja_eligible'] is False:
            self.ja_excluded_hits += 1
        else:
            self.ja_unknown_hits += 1
        if flags['crit'] is None:
            self.crit_unknown_hits += 1
        else:
            self.crit_eligible_hits += 1
            self.crit_hits += int(flags['crit'])

    def merge(self, other):
        for key in COUNT_FIELDS:
            setattr(self, key, getattr(self, key) + getattr(other, key))

    def as_dict(self):
        return {**vars(self),
                'ja_percent': self.ja_hits * 100 / self.ja_eligible_hits if self.ja_eligible_hits else None,
                'crit_percent': self.crit_hits * 100 / self.crit_eligible_hits if self.crit_eligible_hits else None}

    @classmethod
    def from_totals(cls, totals):
        """Old aggregate JSON has no flags; its hits remain unknown, never zero %."""
        counts = totals.get('hit_rates')
        if counts is None:
            return cls(ja_unknown_hits=totals['hits_dealt'], crit_unknown_hits=totals['hits_dealt'])
        # Rates are always derived from counts, including when several lives merge.
        return cls(**{key: counts[key] for key in COUNT_FIELDS})


def classify_hit(event, ignored_ids):
    inspected = inspect_event(event, ignored_ids)
    return {'flags_raw': inspected['flags_raw'], 'flags_hex': inspected['flags_hex'],
            'ja_eligible': inspected['ja_eligible_candidate'], 'ja_scope_reason': inspected['ja_scope_reason'],
            'ja': inspected['ja_candidate'], 'crit': inspected['crit_candidate']}


def rate_policy(ignore_metadata):
    return {'version': 2, 'interpretation': 'classic_masks_with_independent_ja_rules',
            'offset': OFFSET, 'ja_mask': JA_MASK, 'crit_mask': CRIT_MASK,
            'denominator': 'eligible_positive_hits', 'merge': 'sum_counts_then_divide',
            'ja_scope': 'original_source_before_owner_credit',
            'pet_ja': 'not_applicable', 'crit_owner_credit': 'includes_pet_hits',
            'zero_denominator_percent': None, 'ja_exclusions': dict(ignore_metadata),
            'validation': {
                'all_classes_and_attacks_verified': False,
                'ja_controlled_attack_ids': [2441204394, 3756260250, 3756260700],
                'classic_sha256': 'b2a87ec9000caa7667677d22f51859c449f4bb686f10b876370be7b861243332',
                'ja_record': 'tools/combat_core/JA-LIVE-2026-10-19.md',
                'crit_evidence': 'four_prior_blue_white_screenshots',
                'crit_record': 'tools/combat_core/HIT-FLAGS-2026-10-18.md'}}
