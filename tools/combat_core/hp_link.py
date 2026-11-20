"""Detect damage that does not lower the HP of the enemy being fought.

The 04:52 packet carries the target's HP after the hit (0x34) and a small
per-target part index (0x38). Two cases are left out of DPS:

* HP-unlinked parts: a positive hit that leaves HP unchanged was not applied
  to the enemy's HP. One such hit can be a delivery-order artefact, so a part
  counts only after two consecutive non-lowering hits; the first of them is
  then credited retroactively.
* Partless objects (part index 255 on direct hits, e.g. the rings of a boss):
  separate entities with their own HP. Status ticks such as Burn also carry 255
  on normal enemies, so they never establish this, and an enemy once hit on a
  real part is never treated as partless.
"""
import struct

from .damage_policy import BURN_ATTACK_IDS

NEW_HP_OFFSET = 0x34
PART_OFFSET = 0x38
NO_PART = 255
UNLINKED_STREAK = 2
PARTLESS_STREAK = 2


def hp_and_part(packet_hex):
    packet = bytes.fromhex(packet_hex)
    if len(packet) <= PART_OFFSET:
        return None, None
    return struct.unpack_from('<I', packet, NEW_HP_OFFSET)[0], packet[PART_OFFSET]


class _Target:
    __slots__ = ('hp', 'parted', 'partless', 'parts')

    def __init__(self):
        self.hp = None
        self.parted = False      # Hit on a real part at least once.
        self.partless = []       # Pending credits, or True once established.
        self.parts = {}          # part -> pending credits, or True once unlinked.


class HpLinkTracker:
    def __init__(self):
        self.targets = {}

    def observe(self, target_key, packet_hex, credits, attack_id=None):
        """Record one event; return the credit lists now known to be left out of DPS.

        ``credits`` is None for events that are not included damage (they still
        update the target's HP) or a ``(value, groups)`` pair to credit.
        """
        new_hp, part = hp_and_part(packet_hex)
        if new_hp is None:
            return []
        target = self.targets.get(target_key)
        if target is None or (target.hp == 0 and new_hp > 0):
            # Object IDs are reused once an enemy is defeated.
            target = self.targets[target_key] = _Target()
        previous, target.hp = target.hp, new_hp
        if credits is None:
            return []
        if part == NO_PART:
            return self._partless(target, credits, attack_id)
        target.parted, target.partless = True, []
        if previous is None or new_hp == 0:
            return []
        if new_hp < previous:
            target.parts.pop(part, None)
            return []
        state = target.parts.get(part)
        if state is True:
            return [credits]
        pending = [*(state or []), credits]
        if len(pending) < UNLINKED_STREAK:
            target.parts[part] = pending
            return []
        target.parts[part] = True
        return pending

    @staticmethod
    def _partless(target, credits, attack_id):
        if target.partless is True:
            return [credits]
        if target.parted or attack_id in BURN_ATTACK_IDS:
            return []
        target.partless.append(credits)
        if len(target.partless) < PARTLESS_STREAK:
            return []
        pending, target.partless = target.partless, True
        return pending
