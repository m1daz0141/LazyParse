"""Inspect receive-side quest candidates. Never resets or controls recording."""
import argparse
import json
from pathlib import Path

from .metadata import read_metadata
from .model import ObjectRef

REFERENCE = 'https://github.com/PhantasyServer/pso2-protocol-lib/tree/master/src/protocol'


def inspect_notice(event):
    major, minor = event.packet_id
    if (major, minor) != (3, 0x24) and major != 0x0b:
        return None
    result = {'sequence': event.sequence, 'qpc': event.qpc, 'qpc_frequency': event.frequency,
              'filetime': event.filetime, 'packet_id': f'{major:02x}:{minor:02x}',
              'packet_size': event.packet_size, 'stored_size': len(event.prefix),
              'interpretation_live_verified': False, 'safe_for_automatic_reset': False,
              'quest_object_candidate': None, 'world_object_candidate': None,
              'initial_map_object_candidate': None, 'party_object_candidate': None}
    if major == 3:
        result['meaning_candidate'] = 'load_level_world_and_quest_headers'
        # Reference layout: header8 + map12 + host12 + settings52 + world12.
        for name, offset in [('initial_map_object_candidate', 8),
                             ('world_object_candidate', 84),
                             ('quest_object_candidate', 96),
                             ('party_object_candidate', 108)]:
            if len(event.prefix) >= offset + 12:
                result[name] = ObjectRef.from_packet(event.prefix, offset).as_dict()
    else:
        result['meaning_candidate'] = 'quest_result_notice' if minor == 0x14 else 'quest_family_notice'
    return result


def survey(path):
    notices = [notice for event in read_metadata(Path(path)) if (notice := inspect_notice(event)) is not None]
    return {'schema_version': 1, 'input': str(Path(path).resolve()), 'reference': REFERENCE,
            'mode': 'diagnostic_only', 'automatic_reset_enabled': False, 'notices': notices,
            'notes': ['Existing identity-only recordings do not retain these notifications.',
                      'AcceptQuest 0b:20/2f/cd is client-to-server; this receiver does not capture it.',
                      'QuestResult may arrive on return to campship, not at the actual clear instant.',
                      'Map transfers alone are not quest boundaries. Compare same-quest round trips and new quests.',
                      'The quest header can repeat across attempts. World/map headers are additional candidates, not reset authority.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('journal', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = survey(args.journal)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with args.output.open('x', encoding='utf-8') as stream:
            stream.write(text + '\n')
    else:
        print(text)


if __name__ == '__main__':
    main()
