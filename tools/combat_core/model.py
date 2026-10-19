"""Preserve wire identities and the evidence used to classify them."""
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import struct


@dataclass(frozen=True, order=True)
class ObjectRef:
    object_id: int
    entity_type: int
    map_id: int = 0

    def __post_init__(self):
        for value, maximum in [(self.object_id, 0xFFFFFFFF), (self.entity_type, 0xFFFF), (self.map_id, 0xFFFF)]:
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError("object identity contains an invalid integer")

    @property
    def key(self):
        return f"{self.entity_type}:{self.map_id}:{self.object_id}"

    def as_dict(self):
        return {"key": self.key, "object_id": self.object_id,
                "entity_type": self.entity_type, "map_id": self.map_id}

    @classmethod
    def from_dict(cls, value):
        return cls(value["object_id"], value["entity_type"], value.get("map_id", 0))

    @classmethod
    def from_packet(cls, packet, offset):
        object_id, _, entity_type, map_id = struct.unpack_from("<IIHH", packet, offset)
        return cls(object_id, entity_type, map_id)


@dataclass(frozen=True)
class DamageEvent:
    capture_id: str
    sequence: int
    filetime: int
    qpc: int
    frequency: int
    receiver: ObjectRef
    source: ObjectRef
    target: ObjectRef
    attack_id: int
    value: int
    packet_hex: str


@dataclass(frozen=True)
class Identity:
    kind: str
    evidence: str
    name: str | None = None
    owner: ObjectRef | None = None
    name_kind: str | None = None
    name_evidence: str | None = None
    owner_instance: int | None = None
    owner_scene: int | None = None


@dataclass
class IdentityRegistry:
    self_ref: ObjectRef | None = None
    self_evidence: str | None = None
    entries: dict[ObjectRef, Identity] = field(default_factory=dict)
    scene: int = 0
    versions: dict[ObjectRef, int] = field(default_factory=dict)

    def actor_key(self, ref):
        version = self.versions.get(ref, 0)
        return ref.key if not self.scene and not version else f"{self.scene}/{ref.key}#{version}"

    def remove(self, ref):
        self.entries.pop(ref, None)
        self.versions[ref] = self.versions.get(ref, 0) + 1

    def clear_scene(self):
        self.entries.clear()
        self.versions.clear()
        self.scene += 1

    def set_self(self, ref, evidence):
        if ref.entity_type != 4 or not ref.object_id or not evidence:
            raise ValueError("self requires a nonzero player identity and evidence")
        identity = self.entries.get(ref)
        if identity and identity.kind != "player":
            raise ValueError("self conflicts with a non-player identity")
        self.self_ref, self.self_evidence = ref, evidence

    def register(self, ref, kind, evidence, name=None, owner=None, *, name_kind=None, name_evidence=None):
        if kind not in {"player", "enemy", "npc", "object", "pet", "unknown"} or not evidence or not ref.object_id:
            raise ValueError("invalid identity kind, ID, or evidence")
        if (ref.entity_type == 4 and kind != "player") or (kind == "player" and ref.entity_type != 4):
            raise ValueError("identity conflicts with protocol player type")
        if owner is not None and kind not in {"pet", "object"}:
            raise ValueError("ownership is supported only for pets and objects")
        if ref == self.self_ref and kind != "player":
            raise ValueError("identity conflicts with self")
        if name is not None and (not isinstance(name, str) or not name or any(ord(c) < 32 for c in name)):
            raise ValueError("invalid identity name")
        if owner is not None and not owner.object_id:
            raise ValueError("owner must have a nonzero identity")
        prior = self.entries.get(ref)
        self.entries[ref] = Identity(kind, evidence, name, owner, name_kind if name else None,
            (name_evidence or evidence) if name else None,
            self.versions.get(owner, 0) if owner else None, self.scene if owner else None)
        self.versions.setdefault(ref, 0)
        try:
            self.credited_source(ref)
        except ValueError:
            if prior is None:
                del self.entries[ref]
            else:
                self.entries[ref] = prior
            raise

    def credited_source(self, ref):
        seen = set()
        while True:
            if ref in seen:
                raise ValueError("ownership cycle")
            seen.add(ref)
            entry = self.entries.get(ref)
            if entry is None or entry.owner is None:
                return ref
            if entry.owner_scene != self.scene or entry.owner_instance != self.versions.get(entry.owner, 0):
                return ref
            ref = entry.owner

    def describe(self, ref):
        entry = self.entries.get(ref)
        if not ref.object_id:
            kind, evidence = "unknown", "zero_id"
        elif entry:
            kind, evidence = entry.kind, entry.evidence
        elif ref.entity_type == 4:
            kind, evidence = "player", "protocol_object_type_4"
        elif ref.entity_type in {6, 7}:
            kind, evidence = "object", f"protocol_object_type_{ref.entity_type}_enemy_not_established"
        elif ref.entity_type == 22:
            kind, evidence = "npc", "protocol_object_type_22_partner"
        else:
            kind, evidence = "unknown", "unclassified_object_type"
        role = kind
        if kind == "player":
            role = "self" if ref == self.self_ref else "other_player" if self.self_ref else "player_unresolved"
        return {**ref.as_dict(), "actor_key": self.actor_key(ref), "scene": self.scene,
                "instance": self.versions.get(ref, 0), "kind": kind, "role": role, "evidence": evidence,
                "self_evidence": self.self_evidence if role == "self" else None,
                "name": entry.name if entry else None,
                "name_kind": entry.name_kind if entry else None,
                "name_evidence": entry.name_evidence if entry else None,
                "owner": entry.owner.as_dict() if entry and entry.owner else None,
                "ownership_evidence": entry.evidence if entry and entry.owner else None,
                "ownership_active": bool(entry and entry.owner and self.credited_source(ref) != ref)}

    @classmethod
    def from_file(cls, path: Path, capture: Path):
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if data.get("schema_version") != 1:
            raise ValueError("unsupported identity file")
        # Saved annotations never apply silently to a different capture.
        digest = hashlib.sha256(capture.read_bytes()).hexdigest()
        if data.get("capture_sha256") != digest:
            raise ValueError("identity annotations do not match the capture SHA-256")
        registry = cls()
        for entry in data.get("entities", []):
            owner = ObjectRef.from_dict(entry["owner"]) if entry.get("owner") else None
            registry.register(ObjectRef.from_dict(entry), entry["kind"], entry["evidence"], entry.get("name"), owner)
        if data.get("self"):
            registry.set_self(ObjectRef.from_dict(data["self"]), data["self"]["evidence"])
        return registry
