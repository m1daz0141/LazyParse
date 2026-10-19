"""Shared, read-only capture diagnostics used by replay and the live monitor."""
import json
from .shared_files import read_text


def capture_context(capture):
    flags = set()
    boundaries = []
    status_path = capture.with_name('capture-status.json')
    status = None
    if status_path.exists():
        status = json.loads(read_text(status_path))
        for name in ('dropped', 'invalid_damage_size', 'io_error', 'guard_error', 'identity_dropped', 'identity_invalid'):
            if status.get(name):
                flags.add(f'capture_{name}')
        if status.get('guard_fatal'):
            flags.add('capture_guard_fatal')
    else:
        flags.add('capture_status_unavailable')
    event_path = capture.parent.parent / 'continuity-events.jsonl'
    if capture.parent.name.startswith('segment-') and event_path.exists():
        segment = int(capture.parent.name.removeprefix('segment-'))
        for line in read_text(event_path).splitlines():
            event = json.loads(line)
            if event.get('segment') == segment and event.get('state') == 'waiting_for_binding':
                flags.add('binding_gap')
                boundaries.append(event['filetime'])
    elif status and status.get('connection_gaps'):
        flags.add('binding_gap_history_not_localized')
    return flags, sorted(set(boundaries)), status
