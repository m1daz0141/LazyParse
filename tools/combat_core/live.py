"""Read-only polling API for a GUI; current identities and damage use separate timelines."""
from collections import deque
from copy import deepcopy
import json
from pathlib import Path
import re

from .diagnostics import capture_context
from .engine import CombatEngine
from .identity_context import IdentityContext
from .metadata import META_FORMATS, MetadataTimeline, decode_metadata
from .process_state import FILETIME_EPOCH, probe_process, utc_filetime
from .reader import CaptureDecoder, HEADER, RECORD
from .quests import QuestSession
from .shared_files import open_read, read_text


class PendingData(Exception):
    """The next publication or file is not ready yet; retain the last valid view."""


def empty_snapshot():
    return {'schema_version': 1, 'state': 'waiting_for_runtime', 'root': None, 'run_name': None,
        'pid': None, 'worker_verified': False, 'worker_probe': None, 'heartbeat_fresh': False,
        'effective_recording': False, 'runtime': None, 'pending': [], 'measurement': None,
        'identity': None, 'history': []}


def read_json(path):
    return json.loads(read_text(path))


def local_path(value):
    if str(value).startswith(('\\\\', '//')) or '://' in str(value):
        raise ValueError('monitor paths must be local filesystem paths')
    result = Path(value).resolve()
    if str(result).startswith(('\\\\', '//')):
        raise ValueError('monitor paths must stay on the local filesystem')
    return result


class PublishedFile:
    """Read each committed byte once; validate header, identity and monotonic counts."""
    def __init__(self, path, formats):
        self.path, self.formats = path, formats
        self.header = None
        self.count = 0
        self.identity = None
        self.bytes_read = 0

    def poll(self, count):
        if type(count) is not int or count < self.count:
            raise ValueError('published record count decreased or is invalid')
        with open_read(self.path) as stream:
            stat = self.path.stat()
            identity = (stat.st_dev, stat.st_ino)
            header = stream.read(HEADER.size)
            if len(header) < HEADER.size:
                raise PendingData('header is not complete')
            magic, version, size, frequency, _ = HEADER.unpack(header)
            if (magic, version, size) not in self.formats or not frequency:
                raise ValueError('unsupported stream header')
            if self.header is not None and (header != self.header or identity != self.identity):
                raise ValueError('published stream was replaced or its header changed')
            target = HEADER.size + count * size
            if stat.st_size < HEADER.size + self.count * size:
                raise ValueError('published stream was truncated')
            if stat.st_size < target:
                raise PendingData('published records are not visible yet')
            stream.seek(HEADER.size + self.count * size)
            data = stream.read((count - self.count) * size)
            if len(data) != (count - self.count) * size:
                raise PendingData('record publication is incomplete')
        self.header, self.identity, self.count = header, identity, count
        self.bytes_read += len(data)
        return data

    def check_final_size(self):
        if self.path.stat().st_size != HEADER.size + self.count * HEADER.unpack(self.header)[2]:
            raise ValueError('finalized stream has uncommitted or partial trailing records')


class RunJournal:
    def __init__(self, run, pid, source, idle_seconds):
        self.path = local_path(run / 'identity-context/identity.opmeta')
        if self.path.parent != run / 'identity-context':
            raise ValueError('identity journal path escapes its run')
        self.reader = PublishedFile(self.path, META_FORMATS)
        self.pid, self.source = pid, source
        self.header = None
        self.status = None
        self.events = []
        self.through = 0
        self.engine = CombatEngine(idle_seconds)
        self.engine.begin_segment(str(run))
        self.timeline = MetadataTimeline()

    def poll(self):
        status = read_json(self.path.with_name('context-status.json'))
        data = self.reader.poll(status['written'])
        header = self.reader.header
        _, _, _, self.frequency, started = HEADER.unpack(header)
        if (status.get('schema_version') != 1 or status.get('pid') != self.pid or
            status.get('source_sha256') != self.source or status.get('start_filetime') != started):
            raise ValueError('run identity journal/process/version mismatch')
        through = status.get('published_through_qpc')
        if type(through) is not int or through < self.through:
            raise ValueError('identity publication time decreased or is invalid')
        additions = decode_metadata(header + data)
        if additions and self.events and (additions[0].sequence <= self.events[-1].sequence or
                                          additions[0].qpc < self.events[-1].qpc):
            raise ValueError('identity records are out of order across publications')
        self.events.extend(additions)
        self.header, self.status, self.through = header, status, through
        if status.get('stopped'):
            self.reader.check_final_size()
        for name in ('dropped', 'invalid', 'io_error'):
            if status.get(name):
                self.engine.mark_gap('identity_context_' + name)
        # Timeline.update's full-prefix check belongs to external file readers.
        # This list is owned here and can only be appended after validation.
        self.timeline.events = self.events
        self.timeline.through(through, self.frequency, self.engine)


class SegmentFollower:
    """Incremental aggregation for one segment, reusing the existing decoder/engine."""
    def __init__(self, path, idle_seconds=10):
        self.path = path
        self.reader = PublishedFile(path, {(b'OPCAP01\0', 1, RECORD.size): RECORD})
        self.decoder = CaptureDecoder(str(path.resolve()))
        self.context = IdentityContext(path)
        self.engine = CombatEngine(idle_seconds)
        self.engine.begin_segment(str(path.resolve()))
        self.timeline = MetadataTimeline()
        self.pending = deque()
        self.consumed = 0
        self.seeded = self.complete = False
        self.ref = self.status = self.journal = None
        self.boundaries = set()
        self.failure = None

        self.quests = None

    def poll(self, journal):
        if self.failure:
            raise ValueError(self.failure)
        try:
            return self._poll(journal)
        except ValueError as error:
            self.failure = str(error)
            raise

    def _poll(self, journal):
        flags, boundaries, status = capture_context(self.path)
        if status is None:
            raise PendingData('capture status is not present')
        _, ref, _, through, context_flags = self.context.read(status, journal=journal)
        self.journal, self.status, self.ref = journal, status, ref
        self.engine.quality_flags.update(flags | context_flags)
        self.engine.quality_by_capture[self.engine.capture_id].update(flags | context_flags)
        self.timeline.events = journal.events
        if not self.seeded:
            if through < ref['start_qpc']:
                raise PendingData('identity context has not reached the segment start')
            self.timeline.through(ref['start_qpc'], ref['qpc_frequency'], self.engine)
            self.quests = QuestSession(str(self.path.resolve()), ref['start_qpc'], ref['qpc_frequency'],
                                       ref['source_sha256'], self.engine.idle_seconds)
            self.seeded = True
        data = self.reader.poll(status['written'])
        if self.decoder.frequency is None:
            data = self.reader.header + data
        self.pending.extend(self.decoder.feed(data))
        if self.decoder.frequency != ref['qpc_frequency']:
            raise ValueError('identity and damage clocks differ')
        if self.complete:
            if data or self.pending:
                raise ValueError('damage was appended to a finalized segment')
            self.reader.check_final_size()
            return
        quest_damages = []
        while self.pending and self.pending[0].qpc <= through:
            event = self.pending.popleft()
            if event.qpc < ref['start_qpc'] or (ref['end_qpc'] is not None and event.qpc > ref['end_qpc']):
                raise ValueError('damage lies outside its segment time bounds')
            for boundary in boundaries:
                if boundary not in self.boundaries and boundary <= event.filetime:
                    self.engine.mark_gap('binding_gap')
                    self.boundaries.add(boundary)
            self.timeline.before(event, self.engine)
            self.engine.feed(event)
            quest_damages.append(event)
            self.consumed += 1
        if status.get('stopped'):
            if ref['end_qpc'] is None or through < ref['end_qpc']:
                if journal.status.get('stopped'):
                    raise ValueError('identity context stopped before the segment was finalized')
                raise PendingData('waiting for final identity publication')
            if self.pending:
                raise ValueError('unprocessed damage in finalized segment')
            self.reader.check_final_size()
            self.decoder.finish()
            for boundary in boundaries:
                if boundary not in self.boundaries:
                    self.engine.mark_gap('binding_gap')
                    self.boundaries.add(boundary)
            self.timeline.through(ref['end_qpc'], ref['qpc_frequency'], self.engine)
            self.engine.finish(ref['end_qpc'], ref['qpc_frequency'])
            self.complete = True
        else:
            # Only the measurement clock advances. Attribution remains at the
            # last consumed hit so a late damage write keeps its past identity.
            self.engine.observe_time(through, ref['qpc_frequency'])
        self.quests.update(journal.events, quest_damages, through,
                           end_qpc=ref['end_qpc'] if self.complete else None,
                           flags=self.engine.quality_flags)

    def report(self, *, full=False):
        result = self.engine.snapshot()
        result['capture_diagnostics'] = deepcopy(self.status)
        result['identity_context'] = {'reference': deepcopy(self.ref),
                                      'status': deepcopy(self.journal.status) if self.journal else None}
        result['identity_metadata'] = (self.timeline.report() if full else
            {'records_applied': self.timeline.position, 'packet_counts': dict(self.timeline.packet_counts)})
        return deepcopy(result)

    def activity(self, live):
        current = self.engine.current
        if self.complete:
            return {'active': False, 'reason': 'capture_stopped'}
        if not live:
            return {'active': False, 'reason': 'worker_unavailable'}
        if current is None:
            return {'active': False, 'reason': 'no_active_encounter'}
        if self.journal.engine.registry.scene != self.engine.registry.scene:
            return {'active': False, 'reason': 'map_transfer'}
        quiet = max(0, self.journal.through - current.last_qpc) / current.frequency
        return {'active': quiet < float(self.engine.idle_seconds),
                'reason': 'receiving' if quiet < float(self.engine.idle_seconds) else 'idle_timeout',
                'seconds_since_last_positive': quiet}


class RuntimeMonitor:
    """poll() returns a detached JSON-ready snapshot. It never sends control commands."""
    def __init__(self, root=None, *, manifest=None, idle_seconds=10, process_probe=probe_process,
                 clock=utc_filetime, expected_process='pso2'):
        if (root is None) == (manifest is None):
            raise ValueError('choose exactly one build root or build manifest')
        CombatEngine(idle_seconds)  # Validate configuration before reading anything.
        self.root = local_path(root) if root is not None else None
        self.manifest = local_path(manifest) if manifest is not None else None
        self.idle_seconds, self.process_probe, self.clock = idle_seconds, process_probe, clock
        self.expected_process = expected_process.casefold()
        self.key = None
        self.journal = None
        self.segments = {}
        self.history = deque(maxlen=20)
        self.archived = set()
        self.revision = 0
        self.failure = None
        self.last = empty_snapshot()

    def _archive(self, number, follower, run_name, error=None):
        if str(follower.path) in self.archived:
            return
        self.archived.add(str(follower.path))
        summary = follower.engine.snapshot()
        self.history.append({'run_name': run_name, 'segment_number': number,
            'capture_path': str(follower.path), 'complete': follower.complete,
            'counts': summary['counts'], 'roles': summary['roles'], 'error': error,
            'quest_reports': follower.quests.view()['history'] if follower.quests else []})

    def _retire_run(self):
        if self.journal is None:
            return
        error = None
        try:
            self.journal.poll()
        except (OSError, ValueError, KeyError, TypeError, PendingData) as caught:
            error = str(caught)
        run = self.journal.path.parent.parent
        for directory in sorted(run.glob('segment-*')):
            if re.fullmatch(r'segment-[0-9]+', directory.name) and local_path(directory) == run / directory.name:
                number = int(directory.name[8:])
                if number > 0 and number not in self.segments:
                    self.segments[number] = SegmentFollower(directory / 'capture.opcap', self.idle_seconds)
        for number, follower in self.segments.items():
            try:
                if error is None:
                    follower.poll(self.journal)
                self._archive(number, follower, self.key[1],
                              error or (None if follower.complete else 'previous run did not finalize'))
            except (OSError, ValueError, KeyError, TypeError, PendingData) as caught:
                self._archive(number, follower, self.key[1], str(caught))

    def _state(self):
        root = local_path(read_json(self.manifest)['session']) if self.manifest else self.root
        state = read_json(root / 'autostart-state.json')
        match = re.fullmatch(r'run-([0-9]+)-([0-9]+)-([0-9]+)', state['run_name'])
        if (state.get('control_abi') != 1 or not match or type(state.get('pid')) is not int or
            not 0 < state['pid'] <= 0xffffffff or int(match[2]) != state['pid']):
            raise ValueError('invalid runtime run/process identity')
        run = local_path(state['run_directory'])
        if run != root / state['run_name']:
            raise ValueError('runtime run path is outside its build root')
        number = state.get('segment_number')
        if type(number) is not int or number < 0:
            raise ValueError('invalid runtime segment number')
        if state.get('capture_directory'):
            if local_path(state['capture_directory']) != run / f'segment-{number:06d}' or not number:
                raise ValueError('runtime segment path is invalid')
        elif number:
            raise ValueError('runtime segment path is missing')
        now = self.clock()
        stamp = state['state_filetime']
        if type(stamp) is not int:
            raise ValueError('invalid runtime heartbeat')
        fresh = -10_000_000 <= now - stamp < 50_000_000
        process = self.process_probe(state['pid'])
        alive = (process.get('alive') is True and str(process.get('name', '')).casefold() == self.expected_process and
                 0 < process.get('started_filetime', 0) <= int(match[1]) <= stamp)
        return root, run, state, fresh, alive, process, now

    def poll(self):
        self.revision += 1
        try:
            root, run, state, fresh, alive, process, now = self._state()
            key = (str(root), state['run_name'], state['pid'])
            if key != self.key:
                self._retire_run()
                self.key, self.failure = key, None
                self.segments = {}
                self.journal = RunJournal(run, state['pid'], state['classic_hash_required'], self.idle_seconds)
                self.last = empty_snapshot()
            self.last.update(root=str(root), run_name=state['run_name'], pid=state['pid'], runtime=state)
            if self.failure:
                raise ValueError(self.failure)
            self.journal.poll()
            # A stopped journal remains useful for replay, never for a live indicator.
            context_stamp = FILETIME_EPOCH + self.journal.path.with_name('context-status.json').stat().st_mtime_ns // 100
            identity_live = (alive and fresh and state.get('identity_observing', False) and
                not state.get('identity_context_error') and not self.journal.status.get('io_error') and
                not self.journal.status.get('stopped') and -10_000_000 <= now - context_stamp < 50_000_000)
            problems = []
            # Enumerate segments so a fast Stop/Start between polls is not silently skipped.
            paths = sorted((path for path in run.glob('segment-*') if re.fullmatch(r'segment-[0-9]+', path.name)),
                           key=lambda path: int(path.name[8:]))
            for directory in paths:
                number = int(directory.name[8:])
                if not 0 < number <= state['segment_number']:
                    continue
                if local_path(directory) != run / directory.name:
                    raise ValueError('segment directory escapes its run')
                if number not in self.segments:
                    self.segments[number] = SegmentFollower(directory / 'capture.opcap', self.idle_seconds)
                follower = self.segments[number]
                try:
                    follower.poll(self.journal)
                except (PendingData, FileNotFoundError, PermissionError, json.JSONDecodeError) as error:
                    problems.append({'segment': number, 'pending': str(error)})
                if follower.complete and str(follower.path) not in self.archived:
                    self._archive(number, follower, state['run_name'])
            current = self.segments.get(state['segment_number'])
            if state['segment_number'] and current is None:
                problems.append({'segment': state['segment_number'], 'pending': 'segment directory is not present'})
            current_pending = any(problem['segment'] == state['segment_number'] for problem in problems)
            effective = bool(alive and fresh and identity_live and state.get('recording') and
                state.get('state') != 'shutdown' and not state.get('io_error') and not current_pending)
            measurement = None
            if current and current.seeded:
                report = current.report()
                report.pop('identity_state', None)
                measurement = {'capture_path': str(current.path), 'segment_number': state['segment_number'],
                    'complete': current.complete, 'pending_records': len(current.pending), 'report': report,
                    'activity': current.activity(effective)}
            mode = ('shutdown' if state['state'] == 'shutdown' else 'worker_unavailable' if not alive else
                'stale' if not fresh else 'waiting_for_publication' if current_pending else
                'identity_unavailable' if state.get('recording') and not identity_live else state['state'])
            self.last = {'schema_version': 1, 'state': mode, 'root': str(root), 'run_name': state['run_name'],
                'pid': state['pid'], 'worker_verified': alive, 'worker_probe': process, 'heartbeat_fresh': fresh,
                'effective_recording': effective, 'runtime': state, 'pending': problems, 'measurement': measurement,
                'identity': {'is_live': identity_live, 'through_qpc': self.journal.through,
                    'records_applied': self.journal.timeline.position,
                    'state': self.journal.engine.snapshot()['identity_state'],
                    'quality_flags': sorted(self.journal.engine.quality_flags)}, 'history': list(self.history)}
            self.last['quest_view'] = current.quests.view() if current and current.quests else None
        except (PendingData, OSError, ValueError, KeyError, TypeError) as error:
            if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError):
                self.failure = str(error)
            self.last = deepcopy(self.last)
            self.last.update(state='waiting_for_publication' if isinstance(error, (PendingData, FileNotFoundError)) else 'read_error',
                             error=str(error), effective_recording=False, worker_verified=False,
                             heartbeat_fresh=False, worker_probe=None)
            if self.last.get('identity'):
                self.last['identity']['is_live'] = False
            if self.last.get('measurement'):
                self.last['measurement']['activity'] = {'active': False, 'reason': 'reader_unavailable'}
        self.last['revision'] = self.revision
        return deepcopy(self.last)
