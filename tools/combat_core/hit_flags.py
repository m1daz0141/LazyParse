"""Historical hit-flag candidates for offline inspection, not verified metrics."""
import hashlib
import csv
import io
from pathlib import Path

from ..binary_analysis.probe_damage_packet import decode

OFFSET = 0x44
JA_MASK = 0x01
CRIT_MASK = 0x40
TACT_NORMAL_ATTACK = 4065469038
RULES_PATH = Path(__file__).resolve().parent / 'data/ja_exclusions.csv'
LOCAL_RULES_PATH = Path(__file__).resolve().parent / 'ja_exclusions.local.csv'
LEGACY_SOURCE = {
    'repository': 'https://github.com/approved/PSO2DamageDump',
    'commit': '4cafb9fce2b8f172b2c050d90713663aa1f09dd2',
    'definitions': 'PSO2DamageDump/gamedata.h',
    'layout': 'PSO2DamageDump/packetdef.h',
    'offset': OFFSET, 'ja_mask': JA_MASK, 'crit_mask': CRIT_MASK,
    'current_client_verified': False,
}


def read_ja_exclusions(path=None):
    """Load LazyParse rules or an explicit local override, without old UI files."""
    if path is None:
        path = LOCAL_RULES_PATH if LOCAL_RULES_PATH.is_file() else RULES_PATH
    try:
        raw = Path(path).read_bytes()
        values = []
        for row in csv.reader(io.StringIO(raw.decode('utf-8-sig')), strict=True):
            if not row or not row[0].strip() or row[0].strip().startswith('#'):
                continue
            if row[0].strip() == 'attack_id':
                continue
            values.append(int(row[0].strip()))
        if any(not 0 <= value <= 0xFFFFFFFF for value in values):
            raise ValueError('JA exclusion ID is outside uint32')
        return frozenset(values), {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(),
            'rows': len(values), 'distinct_ids': len(set(values)), 'available': True, 'error': None,
            'policy': 'local_override' if Path(path) == LOCAL_RULES_PATH else 'independent_rules',
            'requires_overparse_files': False}
    except (OSError, ValueError, csv.Error) as error:
        return None, {'path': str(path), 'available': False, 'error': str(error)}


# Compatibility for older callers; this never opens an OverParse resource.
read_legacy_ignore = read_ja_exclusions


def legacy_flags(packet):
    """Keep the raw bits even where JA does not apply, such as a pet source."""
    fields = decode(packet, 'current')
    if not fields['matches_known_definition_size']:
        raise ValueError('Hit-flag audit requires an 84-byte current Classic packet')
    value = packet[OFFSET]
    return {'flags_offset': OFFSET, 'flags_raw': value, 'flags_hex': f'0x{value:02X}',
            'flags_binary': f'{value:08b}', 'legacy_ja_bit': bool(value & JA_MASK),
            'legacy_crit_bit': bool(value & CRIT_MASK),
            'other_bits_raw': value & ~(JA_MASK | CRIT_MASK)}


def ja_scope(event, ignored_ids):
    """Use the original source, before owner credit; absence is never a failed JA."""
    if event['signed_value'] <= 0:
        return False, 'non_positive_value'
    if event['source']['kind'] == 'pet':
        return False, 'pet_has_no_ja'
    if event['attack_id'] == TACT_NORMAL_ATTACK:
        return False, 'takt_normal_pet_related_no_ja_policy'
    if event['source']['kind'] != 'player':
        return False, 'non_player_source'
    if event['attack_id'] == 0:
        return None, 'unknown_attack_id_zero'
    if ignored_ids is None:
        return None, 'ja_exclusion_policy_unavailable'
    if event['attack_id'] in ignored_ids:
        return False, 'configured_no_ja_id'
    return True, 'legacy_eligible_candidate'


def inspect_event(event, ignored_ids):
    packet = bytes.fromhex(event['packet_hex'])
    # Prevent auditing a packet under unrelated attribution or numeric values.
    fields = decode(packet, 'current')
    if fields['attack_id'] != event['attack_id'] or fields['damage'] != event['signed_value']:
        raise ValueError('Packet and event values differ')
    flags = legacy_flags(packet)
    eligible, reason = ja_scope(event, ignored_ids)
    return {**flags, 'ja_eligible_candidate': eligible, 'ja_scope_reason': reason,
            'ja_candidate': flags['legacy_ja_bit'] if eligible is True else None,
            'crit_candidate': flags['legacy_crit_bit'] if event['signed_value'] > 0 else None,
            'ja_current_verified': False, 'crit_current_verified': False}
