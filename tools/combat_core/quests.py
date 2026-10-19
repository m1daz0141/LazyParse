"""Logical quest measurements over an uninterrupted, published damage stream.

Rules have limited live validation on JP Classic. Raw segment totals remain the
authority; inferred boundaries never delete records or issue capture commands.
"""
from bisect import bisect_right

from .engine import CombatEngine
from .metadata import MetadataTimeline
from .model import ObjectRef
from .quest_probe import inspect_notice

SUPPORTED_CLASSIC = 'b2a87ec9000caa7667677d22f51859c449f4bb686f10b876370be7b861243332'
RULE_VERSION = 2
TIMED_GAP_FLAGS = {'binding_gap', 'identity_context_binding_reset'}


class QuestBucket:
    def __init__(self, owner, number, start, kind, world=None, quest=None, partial=False):
        self.owner, self.number, self.start, self.kind = owner, number, start, kind
        self.quest_number = 1 + sum(b.kind == 'quest' for b in owner.buckets) if kind == 'quest' else None
        self.world, self.quest, self.partial = world, quest, partial
        self.end = self.reason = self.result_qpc = None
        self.start_sequence = self.end_sequence = self.result_sequence = None
        self.clear_pair_sequences = None
        self.local_flags = set()
        self.engine = CombatEngine(owner.idle_seconds)
        # The parent stream validates global sequence continuity; a logical
        # interval legitimately begins at a record number greater than one.
        self.engine.begin_segment(owner.capture_id, first_sequence=None)
        self.timeline = MetadataTimeline()
        self.timeline.events = owner.identities
        self.timeline.through(start, owner.frequency, self.engine)

    @property
    def key(self):
        return f'{self.owner.capture_id}#quest-{self.number}'

    def feed(self, event):
        self.timeline.events = self.owner.identities
        self.timeline.before(event, self.engine)
        self.engine.feed(event)

    def report(self):
        through = min(self.owner.through, self.end) if self.end is not None else self.owner.through
        self.engine.observe_time(through, self.owner.frequency)
        result = self.engine.snapshot()
        timing = result['timing']
        # Do not advance the attribution timeline or finish the engine here:
        # late-published damage must still use identities at the hit's own QPC.
        for span in timing['spans']:
            span['end_qpc'] = max(span['first_damage_qpc'], through)
            span['end_evidence'] = self.reason if self.end is not None else 'latest_observed_qpc'
        elapsed = sum((s['end_qpc'] - s['first_damage_qpc']) / s['qpc_frequency'] for s in timing['spans'])
        timing.update(elapsed_seconds=elapsed, dps=result['counts']['positive_sum'] / elapsed if elapsed else None,
                      quest_end_detection=self.reason or 'quest_measurement_in_progress')
        for group in [*result['actors'], *result['attacks'], *result['roles'].values()]:
            group['dps'] = group['damage_dealt'] / elapsed if elapsed else None
        result['quest_measurement'] = {'id': self.key, 'number': self.quest_number or self.number,
            'interval_number': self.number, 'kind': self.kind,
            'world_candidate': self.world, 'quest_candidate': self.quest, 'start_qpc': self.start,
            'end_qpc': self.end, 'end_reason': self.reason, 'result_qpc': self.result_qpc,
            'started_mid_quest': self.partial, 'rule_version': RULE_VERSION,
            'experimental': True, 'all_quest_types_verified': False,
            'rules_suspended': self.owner.hard_suspended or bool(self.owner.gap_qpc is not None and
                (self.end is None or self.end >= self.owner.gap_qpc)),
            'observation_gap_qpc': self.owner.gap_qpc if self.owner.gap_qpc is not None and
                (self.end is None or self.end >= self.owner.gap_qpc) else None,
            'source_sha256': self.owner.source_sha256,
            'start_notice_sequence': self.start_sequence, 'end_notice_sequence': self.end_sequence,
            'clear_pair_sequences': self.clear_pair_sequences, 'result_notice_sequence': self.result_sequence,
            'stream_finalized': self.owner.finalized, 'capture_id': self.owner.capture_id}
        result['quality']['flags'] = sorted(set(result['quality']['flags']) | self.owner.flags | self.local_flags)
        result['quality']['per_capture'][self.owner.capture_id] = sorted(
            set(result['quality']['per_capture'].get(self.owner.capture_id, [])) | self.owner.flags | self.local_flags)
        return result


class QuestSession:
    def __init__(self, capture_id, start_qpc, frequency, source_sha256, idle_seconds=10):
        self.capture_id, self.start, self.frequency = capture_id, start_qpc, frequency
        self.idle_seconds = idle_seconds
        self.source_sha256 = source_sha256
        self.supported = source_sha256 == SUPPORTED_CLASSIC
        self.suspended = False
        self.hard_suspended = self.timed_suspended = False
        self.gap_qpc = None
        self.identities = []
        self.position = 0
        self.through = start_qpc
        self.finalized = False
        self.flags = set()
        self.raw_flags = set()
        self.buckets = []
        self.seeded = False
        self.active = self.last_quest = None
        self.last_clear_prefix = None

    @staticmethod
    def level(event):
        if event.packet_id != (3, 0x24) or len(event.prefix) < 120:
            return None
        value = inspect_notice(event)
        fields = [value[k] for k in ('initial_map_object_candidate', 'world_object_candidate',
                                     'quest_object_candidate', 'party_object_candidate')]
        if [entry['entity_type'] for entry in fields] != [5, 16, 11, 13]:
            return None
        if ObjectRef.from_packet(event.prefix, 20).entity_type != 4:
            return None
        world, quest = fields[1]['object_id'], fields[2]['object_id']
        if bool(world) != bool(fields[0]['object_id']):
            return None  # Mixed zero/nonzero world and map: unknown layout/state.
        return (world, quest) if quest else None

    def _new(self, qpc, kind, world=None, quest=None, partial=False):
        bucket = QuestBucket(self, len(self.buckets) + 1, qpc, kind, world, quest, partial)
        self.buckets.append(bucket)
        self.active = bucket
        if kind == 'quest':
            self.last_quest = bucket
        return bucket

    def _close(self, qpc, reason, sequence=None):
        if self.active is not None and self.active.end is None:
            self.active.end, self.active.reason = qpc, reason
            self.active.end_sequence = sequence

    def _notice(self, event):
        if self.supported and event.packet_id == (0xff, 1):
            # This locally generated journal marker timestamps the lost
            # binding. Do not invalidate already completed earlier quests.
            self.timed_suspended = self.suspended = True
            if self.gap_qpc is None:
                self.gap_qpc = event.qpc
            self.active.local_flags.update(TIMED_GAP_FLAGS)
            self._close(event.qpc, 'observation_gap', event.sequence)
            self._new(event.qpc, 'outside_quest').local_flags.update(TIMED_GAP_FLAGS)
            return
        if not self.supported or self.suspended:
            return
        level = self.level(event)
        if level is not None:
            world, quest = level
            self.last_clear_prefix = None
            if world:
                if self.active.kind == 'quest' and (self.active.world, self.active.quest) == level:
                    return  # Repeated notification in the same live instance.
                if (self.active.kind == 'after_clear' and self.last_quest is not None
                        and (self.last_quest.world, self.last_quest.quest) == level):
                    return  # A same-world load after clear is not a new attempt.
                self._close(event.qpc, 'new_world', event.sequence)
                self._new(event.qpc, 'quest', world, quest).start_sequence = event.sequence
            else:
                self._close(event.qpc, 'left_quest_without_clear' if self.active.kind == 'quest' else 'outside_interval_ended', event.sequence)
                self._new(event.qpc, 'outside_quest').start_sequence = event.sequence
            return
        if event.packet_id == (0xb, 0x26) and event.packet_size == 24:
            self.last_clear_prefix = event
        elif event.packet_id == (0xb, 0x11):
            prefix, self.last_clear_prefix = self.last_clear_prefix, None
            # Require the observed pair, its sizes, order and tight QPC spacing.
            # Header-only evidence remains explicitly experimental even if a
            # later result corroborates it; this is not a universal clear rule.
            if (prefix is not None and event.packet_size == 48 and self.active.kind == 'quest'
                    and event.sequence == prefix.sequence + 1
                    and 0 <= event.qpc - prefix.qpc <= self.frequency // 20):
                world, quest = self.active.world, self.active.quest
                self.active.clear_pair_sequences = [prefix.sequence, event.sequence]
                self._close(event.qpc, 'clear_candidate', event.sequence)
                self._new(event.qpc, 'after_clear', world, quest).start_sequence = event.sequence
        elif event.packet_id == (0xb, 0x14) and self.last_quest is not None:
            if self.last_quest.reason == 'clear_candidate' and self.active.kind == 'after_clear':
                self.last_quest.result_qpc = event.qpc
                self.last_quest.result_sequence = event.sequence
                self.last_quest.reason = 'clear_candidate_with_result'
            # No matching clear pair: keep timing open instead of inventing
            # an exact clear instant from a delayed return-to-campship result.

    def update(self, identities, damages, through, *, end_qpc=None, flags=()):
        if through < self.through:
            raise ValueError('quest publication clock decreased')
        self.identities = identities
        self.raw_flags.update(flags)
        localized = any(event.packet_id == (0xff, 1) for event in identities)
        self.flags = self.raw_flags - TIMED_GAP_FLAGS if localized else self.raw_flags.copy()
        self.hard_suspended = any(flag != 'zero_source_or_attack_id' for flag in self.flags)
        self.suspended = self.hard_suspended or self.timed_suspended
        if not self.seeded:
            level = None
            prefix = None
            cleared = False
            while self.position < len(identities) and identities[self.position].qpc <= self.start:
                event = identities[self.position]
                if event.packet_id == (0xff, 1):
                    level, prefix, cleared = None, None, False
                    self.timed_suspended = self.suspended = True
                elif self.supported and not self.hard_suspended and (candidate := self.level(event)) is not None:
                    level = candidate
                    cleared, prefix = False, None
                    self.timed_suspended = False
                    self.suspended = self.hard_suspended
                elif event.packet_id == (0xb, 0x26) and event.packet_size == 24:
                    prefix = event
                elif event.packet_id == (0xb, 0x11):
                    if (prefix and event.packet_size == 48 and event.sequence == prefix.sequence + 1
                            and 0 <= event.qpc - prefix.qpc <= self.frequency // 20):
                        cleared = True
                    prefix = None
                self.position += 1
            if level and level[0]:
                self._new(self.start, 'after_clear' if cleared else 'quest', *level, partial=True)
            else:
                self._new(self.start, 'outside_quest')
            self.seeded = True
        upper = min(through, end_qpc) if end_qpc is not None else through
        while self.position < len(identities) and identities[self.position].qpc <= upper:
            self._notice(identities[self.position])
            self.position += 1
        self.through = through
        starts = [bucket.start for bucket in self.buckets]
        for event in damages:
            if event.frequency != self.frequency or event.qpc < self.start or event.qpc > upper:
                raise ValueError('quest damage lies outside published capture bounds')
            self.buckets[bisect_right(starts, event.qpc) - 1].feed(event)
        if end_qpc is not None:
            self._close(end_qpc, 'capture_stopped')
            self.finalized = True

    def reports(self):
        return [bucket.report() for bucket in self.buckets]

    def view(self):
        reports = self.reports()
        quests = [r for r in reports if r['quest_measurement']['kind'] == 'quest']
        return {'supported': self.supported, 'suspended': self.suspended, 'experimental': True,
                'current': quests[-1] if quests else None,
                'history': [r for r in reports if r['quest_measurement']['end_qpc'] is not None
                            and (r['quest_measurement']['kind'] == 'quest' or r['counts']['records'])],
                'outside_records': sum(r['counts']['records'] for r in reports if r['quest_measurement']['kind'] != 'quest'),
                'all_records': sum(r['counts']['records'] for r in reports)}
