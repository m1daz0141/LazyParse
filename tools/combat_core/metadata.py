"""Read bounded identity prefixes and apply them at their position in the stream.

Body interpretations are candidates until checked against this client's live
traffic. Raw prefixes and decision evidence are preserved for that check.
"""
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import struct

from .model import IdentityRegistry, ObjectRef
from .reader import HEADER
from .identity_fields import spawn_name, spawn_owner

META_RECORD = struct.Struct("<QQQQII128s")
META_RECORD_V2 = struct.Struct("<QQQQII192s")
META_FORMATS = {(b"OPMETA01", 1, META_RECORD.size): META_RECORD,
                (b"OPMETA02", 2, META_RECORD_V2.size): META_RECORD_V2}


@dataclass(frozen=True)
class IdentityEvent:
    sequence: int
    damage_sequence: int
    filetime: int
    qpc: int
    frequency: int
    packet_size: int
    prefix: bytes

    @property
    def packet_id(self):
        return self.prefix[5], struct.unpack_from("<H", self.prefix, 6)[0]

    def as_dict(self):
        return {"sequence": self.sequence, "damage_sequence": self.damage_sequence,
                "filetime": self.filetime, "qpc": self.qpc, "qpc_frequency": self.frequency,
                "packet_id": "%02x:%02x" % self.packet_id, "packet_size": self.packet_size,
                "stored_size": len(self.prefix), "prefix_hex": self.prefix.hex()}


def read_metadata(path, *, allow_partial=False, upto=None):
    return decode_metadata(path.read_bytes(), allow_partial=allow_partial, upto=upto)


def decode_metadata(data, *, allow_partial=False, upto=None):
    if len(data) < HEADER.size:
        if allow_partial:
            return []
        raise ValueError("incomplete identity header")
    magic, version, size, frequency, _ = HEADER.unpack_from(data)
    record = META_FORMATS.get((magic, version, size))
    if record is None or not frequency:
        raise ValueError("unsupported identity header")
    payload_size = len(data) - HEADER.size
    if payload_size % record.size and not allow_partial:
        raise ValueError("incomplete final identity record")
    count = payload_size // record.size
    if upto is not None:
        if upto < 0 or upto > count:
            raise ValueError("identity status watermark exceeds file length")
        count = upto
    result = []
    for index in range(count):
        seq, damage_seq, filetime, qpc, packet_size, stored, prefix = record.unpack_from(data, HEADER.size + index * record.size)
        if not seq or not 8 <= stored <= len(prefix) or packet_size < stored or packet_size > 0xFFFFFF or packet_size % 4:
            raise ValueError("invalid identity record sizes or sequence")
        if struct.unpack_from("<I", prefix)[0] != packet_size or any(prefix[stored:]):
            raise ValueError("identity prefix header/padding mismatch")
        if result and (seq <= result[-1].sequence or qpc < result[-1].qpc):
            raise ValueError("identity records are out of order")
        result.append(IdentityEvent(seq, damage_seq, filetime, qpc, frequency, packet_size, prefix[:stored]))
    return result


class MetadataTimeline:
    def __init__(self):
        self.events = []
        self.position = 0
        self.packet_counts = Counter()
        self.decisions = []
        self.pending_owners = {}

    def resolve_owners(self, engine):
        registry = engine.registry
        for ref, (owner, actor_key, evidence) in list(self.pending_owners.items()):
            entry = registry.entries.get(ref)
            if entry is None or registry.actor_key(ref) != actor_key:
                del self.pending_owners[ref]
                continue
            player = registry.entries.get(owner)
            if owner != registry.self_ref and (player is None or player.kind != "player"):
                continue
            registry.register(ref, entry.kind, evidence, entry.name, owner,
                              name_kind=entry.name_kind, name_evidence=entry.name_evidence)
            del self.pending_owners[ref]

    def update(self, events):
        if len(events) < len(self.events) or events[:len(self.events)] != self.events:
            raise ValueError("identity file was truncated or changed")
        self.events = events

    def before(self, damage, engine):
        self.through(damage.qpc, damage.frequency, engine)

    def through(self, qpc, frequency, engine):
        while self.position < len(self.events) and self.events[self.position].qpc <= qpc:
            event = self.events[self.position]
            if event.frequency != frequency:
                raise ValueError("identity and damage clocks differ")
            self.apply(event, engine)
            self.position += 1
        engine.observe_time(qpc, frequency)

    def finish(self, engine):
        while self.position < len(self.events):
            self.apply(self.events[self.position], engine)
            engine.observe_time(self.events[self.position].qpc, self.events[self.position].frequency)
            self.position += 1

    def apply(self, event, engine):
        expected = self.events[self.position - 1].sequence + 1 if self.position else 1
        if event.sequence != expected:
            engine.mark_gap("identity_sequence_gap")
            engine.registry.clear_scene()
            if (engine.registry.self_evidence or "").startswith("packet_"):
                engine.registry.self_ref = engine.registry.self_evidence = None
        major, minor = event.packet_id
        packet_id = f"{major:02x}:{minor:02x}"
        self.packet_counts[packet_id] += 1
        decision = {**event.as_dict(), "interpretation_live_verified": False, "action": "retained_only"}
        try:
            if (major, minor) == (0xff, 1):
                engine.mark_gap("identity_context_binding_reset")
                engine.registry.clear_scene()
                if (engine.registry.self_evidence or "").startswith("packet_"):
                    engine.registry.self_ref = engine.registry.self_evidence = None
                decision["action"] = "invalidate_context_after_binding_change"
                decision["synthetic"] = True
            elif (major, minor) == (6, 0):
                if event.packet_size != 20 or len(event.prefix) != 20:
                    raise ValueError("unexpected local player ID layout")
                ref = ObjectRef(struct.unpack_from("<I", event.prefix, 8)[0], 4)
                if engine.registry.self_ref and engine.registry.self_ref != ref:
                    if not (engine.registry.self_evidence or "").startswith("packet_"):
                        raise ValueError("metadata local ID conflicts with explicit identity")
                    engine.mark_gap("self_identity_changed")
                    engine.registry.clear_scene()
                engine.registry.set_self(ref, "packet_06_00_current_layout_candidate")
                decision["action"] = "set_self"
                decision["object"] = ref.as_dict()
            elif major == 8 and minor in {4, 0xB, 0xC, 0xD, 0x10, 0x11}:
                if len(event.prefix) < 20:
                    raise ValueError("short spawn prefix")
                ref = ObjectRef.from_packet(event.prefix, 8)
                kind = {4: "player", 0xB: "object", 0xC: "npc", 0xD: "enemy", 0x10: "object", 0x11: "pet"}[minor]
                if (minor == 4 and ref.entity_type != 4) or (minor == 0xD and ref.entity_type != 6):
                    raise ValueError("spawn object type differs from candidate layout")
                if minor in {0x10, 0x11} and ref.entity_type not in {6, 7}:
                    raise ValueError("owned object is not a supported object type")
                name, name_kind, name_status = spawn_name(event)
                evidence = f"packet_{packet_id}_current_layout_candidate"
                if ref in engine.registry.versions:
                    engine.registry.remove(ref)
                engine.registry.register(ref, kind, evidence, name, name_kind=name_kind)
                self.pending_owners.pop(ref, None)
                decision.update(action="register_entity", kind=kind, object=ref.as_dict())
                decision.update(name=name, name_kind=name_kind if name else None, name_status=name_status)
                if minor in {0x10, 0x11}:
                    owner = spawn_owner(event)
                    decision["owner_id_candidate"] = owner.object_id if owner else 0
                    decision["owner_object_header"] = owner.as_dict() if owner else None
                    if owner:
                        self.pending_owners[ref] = (owner, engine.registry.actor_key(ref), evidence)
                if minor == 4 and len(event.prefix) > 0x58:
                    decision["spawn_type_byte_0x58_candidate"] = event.prefix[0x58]
            elif major == 4 and minor in {6, 0x3B}:
                if len(event.prefix) < 32:
                    raise ValueError("short despawn prefix")
                ref = ObjectRef.from_packet(event.prefix, 20)
                engine.registry.remove(ref)
                decision.update(action="remove_entity", object=ref.as_dict())
            elif (major, minor) == (3, 0):
                engine.end_encounter("map_transfer")
                engine.registry.clear_scene()
                decision["action"] = "clear_scene_entities"
            # 04:15 action tags remain diagnostic; generic actions are not
            # evidence that the receiving player owns the target object.
            self.resolve_owners(engine)
            if major == 8 and minor in {0x10, 0x11}:
                decision["ownership_applied"] = engine.registry.describe(ref)["ownership_active"]
        except (ValueError, struct.error) as exc:
            engine.quality_flags.add("identity_layout_unrecognized")
            engine.quality_by_capture[engine.capture_id].add("identity_layout_unrecognized")
            decision.update(action="unrecognized_layout", error=str(exc))
        self.decisions.append(decision)

    def report(self):
        return {"records_applied": self.position, "packet_counts": dict(sorted(self.packet_counts.items())),
                "interpretations_live_verified": False, "decisions": self.decisions}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    args = parser.parse_args()
    print(json.dumps([event.as_dict() for event in read_metadata(args.capture)], indent=2))
