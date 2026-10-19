"""UI-independent Classic capture decoding, attribution, and combat aggregation."""
from .engine import CombatEngine
from .model import DamageEvent, IdentityRegistry, ObjectRef
from .reader import CaptureDecoder

__all__ = ["CombatEngine", "DamageEvent", "IdentityRegistry", "ObjectRef", "CaptureDecoder"]
