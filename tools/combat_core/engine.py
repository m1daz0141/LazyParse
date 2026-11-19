"""Deterministic event aggregation. No UI, game access, or wall clock dependency."""
from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction
import math

from .model import IdentityRegistry
from .hit_flags import read_ja_exclusions
from .hit_rates import HitRates, classify_hit, rate_policy
from .damage_policy import BURN_ATTACK_IDS, SOURCE_ZERO_BURN, CONFIGURED_EXCLUSION, exclusion_reason, read_damage_exclusions
from ..attack_catalog.catalog import get_catalog, with_attack_names


@dataclass
class Totals:
    damage_dealt: int = 0
    hits_dealt: int = 0
    damage_taken: int = 0
    hits_taken: int = 0
    max_hit: int = 0
    min_hit: int | None = None
    hit_rates: HitRates = field(default_factory=HitRates)

    def as_dict(self, elapsed, *, include_min=False):
        values = dict(vars(self))
        if not include_min:
            values.pop('min_hit')
        return {**values, "hit_rates": self.hit_rates.as_dict(),
                "dps": self.damage_dealt / elapsed if elapsed else None}


@dataclass
class Encounter:
    number: int
    capture_id: str
    first_qpc: int
    last_qpc: int
    frequency: int
    damage: int = 0
    hits: int = 0
    closed_reason: str | None = None
    quality_flags: set = field(default_factory=set)

    @property
    def duration(self):
        return Fraction(self.last_qpc - self.first_qpc, self.frequency)

    def as_dict(self):
        duration = float(self.duration)
        return {"number": self.number, "capture_id": self.capture_id,
                "first_qpc": self.first_qpc, "last_qpc": self.last_qpc, "qpc_frequency": self.frequency,
                "elapsed_seconds": duration, "positive_damage": self.damage, "hits": self.hits,
                "dps": self.damage / duration if duration else None,
                "closed_reason": self.closed_reason, "quality_flags": sorted(self.quality_flags)}


class CombatEngine:
    def __init__(self, idle_seconds=10):
        if not math.isfinite(idle_seconds) or idle_seconds <= 0:
            raise ValueError("idle_seconds must be finite and positive")
        self.idle_seconds = Fraction(str(idle_seconds))
        self.reset()

    def reset(self):
        """Clear one measurement, including de-duplication and encounter state."""
        self.capture_id = None
        self.registry = IdentityRegistry()
        self.last_event = None
        self.first_sequence = 1
        self.clock_qpc = None
        self.frequency = None
        self.current = None
        self.encounters = []
        self.actor_totals = {}
        self.actor_info = {}
        self.attack_totals = {}
        self.raw_sources = Counter()
        self.counts = Counter()
        self.directions = Counter()
        self.quality_flags = set()
        self.quality_by_capture = {}
        self.sequence_gaps = []
        self.negative_groups = {}
        self.excluded_groups = {}
        self.measurement_spans = []
        self.measurement_span = None
        self.measurement_now = None
        self.measurement_frequency = None
        self.measurement_finished = False
        self.first_event_filetime = None
        self.last_event_filetime = None
        self.ja_ignored_ids, self.ja_ignore_metadata = read_ja_exclusions()
        # Read once per measurement, so one interval never mixes two settings.
        self.excluded_attack_ids, self.attack_exclusion_metadata = read_damage_exclusions()

    def begin_segment(self, capture_id, registry=None, quality_flags=(), *, first_sequence=1):
        if not capture_id or capture_id == self.capture_id:
            raise ValueError("new segment requires a distinct, nonempty capture_id")
        if first_sequence is not None and (type(first_sequence) is not int or first_sequence <= 0):
            raise ValueError('first sequence must be positive or None for a validated logical interval')
        self.end_encounter("segment_boundary")
        self.measurement_span = None
        self.measurement_now = self.measurement_frequency = None
        self.measurement_finished = False
        self.capture_id = capture_id
        self.registry = registry if registry is not None else IdentityRegistry()
        self.last_event = None
        self.first_sequence = first_sequence
        self.clock_qpc = None
        self.frequency = None
        self.quality_by_capture[capture_id] = set(quality_flags)
        self.quality_flags.update(quality_flags)

    def mark_gap(self, reason="binding_gap"):
        self.quality_flags.add(reason)
        self.quality_by_capture.setdefault(self.capture_id, set()).add(reason)
        if self.current:
            self.current.quality_flags.add(reason)
        self.end_encounter(reason)

    def end_encounter(self, reason="manual"):
        if self.current:
            self.current.closed_reason = reason
            self.current = None

    def advance_clock(self, qpc, frequency):
        if frequency <= 0 or (self.frequency is not None and frequency != self.frequency):
            raise ValueError("clock frequency changed within a segment")
        if self.clock_qpc is not None and qpc < self.clock_qpc:
            raise ValueError("clock moved backwards")
        self.frequency, self.clock_qpc = frequency, qpc
        self.observe_time(qpc, frequency)
        if self.current and Fraction(qpc - self.current.last_qpc, frequency) >= self.idle_seconds:
            self.end_encounter("idle_timeout")

    def observe_time(self, qpc, frequency):
        """Advance DPS time without applying future identities to delayed hits."""
        if frequency <= 0 or (self.measurement_frequency is not None and frequency != self.measurement_frequency):
            raise ValueError("measurement clock frequency changed")
        self.measurement_frequency = frequency
        self.measurement_now = max(qpc, self.measurement_now if self.measurement_now is not None else qpc)
        if self.measurement_span is not None and not self.measurement_finished:
            self.measurement_span['end_qpc'] = self.measurement_now

    def _actor(self, ref):
        key = (self.capture_id, self.registry.actor_key(ref))
        self.actor_info[key] = self.registry.describe(ref)
        return self.actor_totals.setdefault(key, Totals())

    @staticmethod
    def direction(source, target, raw_self_hit):
        if raw_self_hit:
            return "self_damage"
        if source["kind"] == "player":
            if target["kind"] == "enemy":
                return "player_to_enemy"
            if target["kind"] == "player":
                return "player_to_player"
            return "player_to_object" if target["kind"] == "object" else "player_to_unresolved"
        if target["kind"] == "player":
            return "enemy_to_player" if source["kind"] == "enemy" else "unresolved_to_player"
        return "other_or_unresolved"

    def feed(self, event):
        if event.capture_id != self.capture_id:
            raise ValueError("call begin_segment before feeding a different capture")
        if event.sequence <= 0 or event.frequency <= 0:
            raise ValueError("invalid sequence or frequency")
        if self.last_event:
            if event.sequence == self.last_event.sequence and event == self.last_event:
                return None  # A repeated delivery of exactly this record, not a repeated hit.
            if event.sequence <= self.last_event.sequence:
                raise ValueError("out-of-order or conflicting duplicate record")
            if event.qpc < self.last_event.qpc or event.frequency != self.last_event.frequency:
                raise ValueError("capture clock changed or moved backwards")
        expected = (self.last_event.sequence + 1 if self.last_event else
                    self.first_sequence if self.first_sequence is not None else event.sequence)
        if event.sequence > expected:
            self.sequence_gaps.append({"capture_id": self.capture_id, "first_missing": expected,
                                       "last_missing": event.sequence - 1})
            self.mark_gap("sequence_gap")
        # An externally advanced idle clock may already exceed the last record
        # timestamp. Preserve record ordering, while allowing delayed delivery.
        self.advance_clock(max(event.qpc, self.clock_qpc or 0), event.frequency)
        self.last_event = event
        if self.first_event_filetime is None:
            self.first_event_filetime = event.filetime
        self.last_event_filetime = event.filetime
        self.counts["records"] += 1
        for ref in (event.receiver, event.source, event.target):
            self.registry.versions.setdefault(ref, 0)
        source = self.registry.describe(event.source)
        target = self.registry.describe(event.target)
        credit_ref = self.registry.credited_source(event.source)
        credit = self.registry.describe(credit_ref)
        hit_flags = classify_hit({'packet_hex': event.packet_hex, 'source': source,
                                 'attack_id': event.attack_id, 'signed_value': event.value}, self.ja_ignored_ids)
        direction = self.direction(credit, target, event.source == event.target)
        excluded = exclusion_reason(event, self.excluded_attack_ids)
        included = excluded is None
        if not event.source.object_id or not event.attack_id:
            self.quality_flags.add("zero_source_or_attack_id")
            self.quality_by_capture[self.capture_id].add("zero_source_or_attack_id")
        if event.value > 0:
            # Packet counts remain a complete audit of all signed records.
            self.counts['positive_records'] += 1
            self.counts['positive_sum'] += event.value
        if included:
            if credit['kind'] == 'player' and self.measurement_span is None:
                self.measurement_span = {'capture_id': self.capture_id, 'first_damage_qpc': event.qpc,
                    'first_damage_filetime': event.filetime,
                    'end_qpc': self.measurement_now, 'qpc_frequency': event.frequency,
                    'end_evidence': 'latest_observed_qpc'}
                self.measurement_spans.append(self.measurement_span)
            self.directions[direction] += event.value
            if self.current is None:
                self.current = Encounter(len(self.encounters) + 1, self.capture_id, event.qpc, event.qpc,
                    event.frequency, quality_flags=set(self.quality_by_capture[self.capture_id]))
                self.encounters.append(self.current)
            self.current.last_qpc = event.qpc
            self.current.damage += event.value
            self.current.hits += 1
            dealt = self._actor(credit_ref)
            dealt.damage_dealt += event.value
            dealt.hits_dealt += 1
            dealt.max_hit = max(dealt.max_hit, event.value)
            dealt.hit_rates.add_hit(hit_flags)
            taken = self._actor(event.target)
            taken.damage_taken += event.value
            taken.hits_taken += 1
            attack = self.attack_totals.setdefault((self.capture_id, self.registry.actor_key(credit_ref), event.attack_id), Totals())
            attack.damage_dealt += event.value
            attack.hits_dealt += 1
            attack.max_hit = max(attack.max_hit, event.value)
            attack.min_hit = min(attack.min_hit, event.value) if attack.min_hit is not None else event.value
            attack.hit_rates.add_hit(hit_flags)
            self.raw_sources[(self.capture_id, self.registry.actor_key(event.source), self.registry.actor_key(credit_ref), event.attack_id)] += event.value
        elif event.value > 0:
            self.counts['excluded_positive_records'] += 1
            self.counts['excluded_positive_sum'] += event.value
            key = (self.capture_id, event.attack_id, excluded)
            group = self.excluded_groups.setdefault(key, {'records': 0, 'positive_sum': 0})
            group['records'] += 1
            group['positive_sum'] += event.value
        elif event.value < 0:
            self.counts["negative_records"] += 1
            self.counts["negative_sum"] += event.value
            key = (self.capture_id, self.registry.actor_key(event.source), self.registry.actor_key(event.target), event.attack_id)
            group = self.negative_groups.setdefault(key, {"count": 0, "signed_sum": 0})
            group["count"] += 1
            group["signed_sum"] += event.value
        else:
            self.counts["zero_records"] += 1
        return {"capture_id": event.capture_id, "sequence": event.sequence,
                "filetime": event.filetime, "qpc": event.qpc, "qpc_frequency": event.frequency,
                "receiver": event.receiver.as_dict(), "source": source, "target": target,
                "credited_source": credit, "ownership_applied": credit_ref != event.source,
                "attack_id": event.attack_id, **get_catalog().labels(event.attack_id), "signed_value": event.value,
                "value_kind": "positive_damage" if event.value > 0 else "healing_candidate" if event.value < 0 else "zero",
                "included_in_damage": included,
                "exclusion_reason": excluded,
                "direction": direction if event.value > 0 else "not_classified_as_damage",
                "packet_hex": event.packet_hex, "hit_flags": hit_flags}

    def finish(self, end_qpc=None, frequency=None):
        if end_qpc is not None:
            if frequency != self.measurement_frequency and self.measurement_frequency is not None:
                raise ValueError('final measurement frequency differs')
            if self.measurement_span is not None:
                if end_qpc < self.measurement_span['first_damage_qpc']:
                    raise ValueError('measurement ends before first damage')
                self.measurement_span['end_qpc'] = end_qpc
                self.measurement_span['end_evidence'] = 'capture_stop_qpc'
        self.measurement_finished = True
        self.end_encounter("end_of_input")
        return self.snapshot()

    def snapshot(self):
        elapsed = float(sum((Fraction(s['end_qpc'] - s['first_damage_qpc'], s['qpc_frequency'])
                             for s in self.measurement_spans), Fraction()))
        actors = []
        role_totals = {}
        for key, totals in sorted(self.actor_totals.items()):
            info = self.actor_info[key]
            role = info["role"]
            aggregate = role_totals.setdefault(role, Totals())
            for field_name in ("damage_dealt", "hits_dealt", "damage_taken", "hits_taken"):
                setattr(aggregate, field_name, getattr(aggregate, field_name) + getattr(totals, field_name))
            aggregate.max_hit = max(aggregate.max_hit, totals.max_hit)
            aggregate.hit_rates.merge(totals.hit_rates)
            actors.append({"capture_id": key[0], **info, **totals.as_dict(elapsed)})
        counts = {name: self.counts[name] for name in ("records", "positive_records", "positive_sum",
                  "negative_records", "negative_sum", "zero_records")}
        selected = {'positive_records': counts['positive_records'] - self.counts['excluded_positive_records'],
                    'positive_sum': counts['positive_sum'] - self.counts['excluded_positive_sum'],
                    'excluded_positive_records': self.counts['excluded_positive_records'],
                    'excluded_positive_sum': self.counts['excluded_positive_sum'],
                    'excluded_groups': [{'capture_id': capture, 'attack_id': attack,
                        'reason': reason, **value} for (capture, attack, reason), value in sorted(self.excluded_groups.items())]}
        return with_attack_names({"schema_version": 1, "counts": counts,
                "recording_time": {"first_event_filetime": self.first_event_filetime,
                                   "last_event_filetime": self.last_event_filetime},
                "damage_totals": selected,
                "hit_rate_policy": rate_policy(self.ja_ignore_metadata),
                "aggregation_policy": {"non_positive_values": "excluded_before_aggregation",
                    "negative_value_classification": "healing_candidate", "raw_signed_values_preserved": True,
                    "configured_attack_exclusions": dict(self.attack_exclusion_metadata),
                    "configured_attack_exclusion_reason": CONFIGURED_EXCLUSION,
                    "source_zero_burn": "excluded_before_aggregation",
                    "source_zero_burn_ids": sorted(BURN_ATTACK_IDS),
                    "source_zero_burn_exclusion_reason": SOURCE_ZERO_BURN,
                    "counts": "all_raw_signed_records"},
                "identity_state": {"self": self.registry.describe(self.registry.self_ref) if self.registry.self_ref else None,
                    "scene": self.registry.scene,
                    "entities": [self.registry.describe(ref) for ref in sorted(self.registry.entries)]},
                "timing": {"idle_timeout_seconds": float(self.idle_seconds),
                    "elapsed_seconds": elapsed, "dps": selected["positive_sum"] / elapsed if elapsed else None,
                    "definition": "continuous_from_first_player_damage_including_idle_and_map_time",
                    "spans": [dict(span) for span in self.measurement_spans],
                    "quest_end_detection": "not_yet_available_use_manual_stop",
                    "zero_duration_dps": None},
                "roles": {key: value.as_dict(elapsed) for key, value in sorted(role_totals.items())},
                "actors": actors,
                "attacks": [{"capture_id": capture, "credited_source_key": source, "attack_id": attack,
                             **value.as_dict(elapsed, include_min=True)} for (capture, source, attack), value in sorted(self.attack_totals.items())],
                "raw_source_damage": [{"capture_id": capture, "source_key": source, "credited_source_key": credit,
                    "attack_id": attack, "damage": damage} for (capture, source, credit, attack), damage in sorted(self.raw_sources.items())],
                "negative_values": [{"capture_id": capture, "source_key": source, "target_key": target,
                    "attack_id": attack, **value} for (capture, source, target, attack), value in sorted(self.negative_groups.items())],
                "damage_by_direction": dict(sorted(self.directions.items())),
                "encounters": [e.as_dict() for e in self.encounters],
                "quality": {"flags": sorted(self.quality_flags), "sequence_gaps": self.sequence_gaps,
                    "per_capture": {key: sorted(value) for key, value in sorted(self.quality_by_capture.items())},
                    "object_type_meanings_current_client_fully_verified": False,
                    "negative_value_meanings_verified": False}})
