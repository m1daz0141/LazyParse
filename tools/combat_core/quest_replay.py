"""Replay logical quest measurements from a finalized OPCAP and its run journal."""
import argparse
import json
from pathlib import Path

from .__main__ import replay
from .identity_context import IdentityContext
from .quests import QuestSession
from .reader import CaptureDecoder


def replay_quests(capture):
    capture = Path(capture).resolve()
    base = replay(capture)
    if not base.get('identity_context'):
        return {'capture_report': base, 'quest_view': None}
    metadata, ref, status, through, flags = IdentityContext(capture).read(base['capture_diagnostics'])
    if ref['end_qpc'] is None:
        return {'capture_report': base, 'quest_view': None,
                'quest_pending': 'incomplete capture: preserve unsegmented totals'}
    session = QuestSession(str(capture), ref['start_qpc'], ref['qpc_frequency'], ref['source_sha256'])
    decoder = CaptureDecoder(str(capture))
    damages = decoder.feed(capture.read_bytes())
    decoder.finish()
    session.update(metadata, damages, through, end_qpc=ref['end_qpc'], flags=base['quality']['flags'])
    view = session.view()
    if view['all_records'] != base['counts']['records']:
        raise ValueError('quest intervals did not account for every captured record')
    return {'capture_report': base, 'quest_view': view}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    text = json.dumps(replay_quests(args.capture), ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if args.output:
        with args.output.open('x', encoding='utf-8') as stream:
            stream.write(text)
    else:
        print(text, end='')


if __name__ == '__main__':
    main()
