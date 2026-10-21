"""Exercise publication races, identity changes and runtime switches without PSO2."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from tools.combat_core.__main__ import replay
from tools.combat_core.live import RuntimeMonitor
from tools.combat_core.process_state import utc_filetime, probe_process
from tools.combat_core import test_combat_core as fixtures
from tools.combat_core.test_combat_core import (SELF, OTHER, ENEMY, PET,
    capture_bytes, identity_packet, metadata_bytes, packet)


class RuntimeFixture:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.now = utc_filetime()
        self.dead = False
        self.state = None
        self.start_run(100)

    def start_run(self, pid):
        self.process_started = self.now - 10_000
        self.run = self.root / f'run-{self.now}-{pid}-0'
        self.context = self.run / 'identity-context'
        self.context.mkdir(parents=True)
        self.identities = []
        self.status = {'schema_version': 1, 'pid': pid, 'source_sha256': 'fixture', 'start_filetime': 1,
            'written': 0, 'published_through_qpc': 100, 'stopped': False, 'io_error': 0}
        self.state = {'control_abi': 1, 'run_name': self.run.name, 'run_directory': str(self.run), 'pid': pid,
            'classic_hash_required': 'fixture', 'state_filetime': self.now, 'segment_number': 0,
            'capture_directory': '', 'state': 'binding_ready_waiting_for_start', 'recording': False,
            'identity_observing': True, 'identity_context_error': 0}
        self.write_context()
        self.write_state()

    def write_state(self):
        self.state['state_filetime'] = self.now
        (self.root / 'autostart-state.json').write_text(json.dumps(self.state))

    def write_context(self, through=None, written=None):
        if through is not None:
            self.status['published_through_qpc'] = through
        if written is None:
            written = len(self.identities)
        self.status['written'] = written
        (self.context / 'identity.opmeta').write_bytes(metadata_bytes(self.identities, version=2))
        (self.context / 'context-status.json').write_text(json.dumps(self.status))

    def begin(self, number, start=900):
        segment = self.run / f'segment-{number:06d}'
        segment.mkdir()
        self.state.update(segment_number=number, capture_directory=str(segment), state='recording', recording=True)
        self.ref = {'schema_version': 1, 'run_name': self.run.name, 'pid': self.state['pid'], 'source_sha256': 'fixture',
            'context_start_filetime': 1, 'context_path': '../identity-context/identity.opmeta', 'qpc_frequency': 1000,
            'start_qpc': start, 'end_qpc': None}
        self.segment = segment
        self.ref_path = segment / 'identity-context.json'
        self.ref_path.write_text(json.dumps(self.ref))
        self.damage_status = {'stopped': False, 'written': 0, 'source_sha256': 'fixture'}
        self.damage([])
        self.write_state()
        return segment

    def damage(self, rows, *, written=None, partial=b''):
        (self.segment / 'capture.opcap').write_bytes(capture_bytes(rows) + partial)
        self.damage_status['written'] = len(rows) if written is None else written
        (self.segment / 'capture-status.json').write_text(json.dumps(self.damage_status))

    def stop(self, end):
        self.ref['end_qpc'] = end
        self.ref_path.write_text(json.dumps(self.ref))
        self.damage_status['stopped'] = True
        (self.segment / 'capture-status.json').write_text(json.dumps(self.damage_status))
        self.state.update(recording=False, state='paused')
        self.write_state()

    def probe(self, pid):
        return {'alive': not self.dead, 'name': 'pso2', 'started_filetime': self.process_started}

    def monitor(self, **kwargs):
        return RuntimeMonitor(self.root, process_probe=self.probe, clock=lambda: self.now, **kwargs)


class LiveMonitorTests(unittest.TestCase):
    def test_startup_failure_keeps_runtime_freshness_separate_from_missing_journal(self):
        from tools.gui.control import preparation_error
        with tempfile.TemporaryDirectory() as root:
            fixture = RuntimeFixture(root)
            fixture.state.update(state='identity_context_error', identity_observing=False,
                                 identity_context_error=5, guard_error=5, guard_stage=3)
            fixture.write_state()
            (fixture.context/'context-status.json').unlink()
            monitor = fixture.monitor()
            current = monitor.poll()
            self.assertTrue(current['runtime_heartbeat_fresh'])
            self.assertTrue(current['runtime_worker_verified'])
            self.assertFalse(current['worker_verified'])
            self.assertFalse(current['heartbeat_fresh'])
            self.assertIn('Win32=5', preparation_error(current))
            fixture.now += 60_000_000  # Previous run has stopped publishing.
            old = monitor.poll()
            self.assertFalse(old['runtime_heartbeat_fresh'])
            self.assertIsNone(preparation_error(old))
            fixture.dead = True
            fixture.write_state()
            self.assertIsNone(preparation_error(monitor.poll()))
            fixture.dead = False
            fixture.process_started = fixture.now + 1  # Reused PID belongs to a newer process.
            reused = monitor.poll()
            self.assertTrue(reused['runtime_heartbeat_fresh'])
            self.assertFalse(reused['runtime_worker_verified'])
            self.assertIsNone(preparation_error(reused))
            (fixture.root/'autostart-state.json').unlink()
            missing = monitor.poll()
            self.assertFalse(missing['runtime_heartbeat_fresh'])
            self.assertIsNone(preparation_error(missing))

    def test_wait_before_files_exist_and_context_read_error_recovers(self):
        with tempfile.TemporaryDirectory() as root:
            monitor = RuntimeMonitor(root)
            empty = monitor.poll()
            self.assertFalse(empty['effective_recording'])
            self.assertIsNone(empty['measurement'])
            self.assertIn('history', empty)
            fixture = RuntimeFixture(root)
            monitor = fixture.monitor()
            self.assertEqual(monitor.poll()['state'], 'binding_ready_waiting_for_start')
            (fixture.context / 'context-status.json').write_text('{')
            bad = monitor.poll()
            self.assertFalse(bad['identity']['is_live'])
            self.assertFalse(bad['worker_verified'])
            fixture.write_context()
            self.assertTrue(monitor.poll()['identity']['is_live'])

    def test_previous_run_final_tail_is_drained_and_crash_is_incomplete(self):
        for stopped in (True, False):
            with self.subTest(stopped=stopped), tempfile.TemporaryDirectory() as root:
                fixture = RuntimeFixture(root)
                fixture.write_context(through=3000)
                fixture.begin(1)
                fixture.damage([(1, 1000, packet())])
                monitor = fixture.monitor()
                monitor.poll()
                fixture.damage([(1, 1000, packet()), (2, 2000, packet(value=250))])
                if stopped:
                    fixture.stop(2500)
                fixture.start_run(200)  # No poll between the final damage and relaunch.
                new = monitor.poll()
                self.assertIsNone(new['measurement'])
                self.assertEqual(new['history'][-1]['counts']['positive_sum'], 350)
                self.assertEqual(new['history'][-1]['complete'], stopped)

    def test_current_names_change_without_damage_but_delayed_damage_uses_old_identity(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = RuntimeFixture(root)
            factory = fixtures.NameOwnershipTests()
            fixture.identities = [(1, 100, identity_packet(6, 0, SELF)),
                (2, 200, factory.spawn(4, SELF, 'First')),
                (3, 300, factory.spawn(13, ENEMY, 'Enemy'))]
            fixture.write_context(through=1000)
            monitor = fixture.monitor()
            before = monitor.poll()
            self.assertEqual(before['identity']['state']['self']['name'], 'First')
            self.assertIsNone(before['measurement'])
            fixture.begin(1)
            initial = monitor.poll()
            self.assertTrue(initial['effective_recording'])
            self.assertEqual(initial['measurement']['report']['counts']['records'], 0)
            fixture.identities.append((4, 2000, factory.spawn(4, SELF, 'Second')))
            fixture.write_context(through=3000)
            current = monitor.poll()
            self.assertEqual(current['identity']['state']['self']['name'], 'Second')
            fixture.damage([(1, 1500, packet())])  # Written after the later name notification.
            delayed = monitor.poll()
            actor = next(actor for actor in delayed['measurement']['report']['actors'] if actor['role'] == 'self')
            self.assertEqual(actor['name'], 'First')
            self.assertEqual(delayed['identity']['state']['self']['name'], 'Second')
            fixture.stop(1800)
            final = monitor.poll()
            self.assertTrue(final['measurement']['complete'])
            follower = monitor.segments[1]
            self.assertEqual(follower.report(full=True), replay(fixture.segment / 'capture.opcap'))
            final['measurement']['report']['counts']['positive_sum'] = -1
            self.assertEqual(monitor.poll()['measurement']['report']['counts']['positive_sum'], 100)

    def test_partial_writes_identity_lag_and_idle_clock_do_not_change_damage_order(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = RuntimeFixture(root)
            fixture.identities = [(1, 100, identity_packet(6, 0, SELF)),
                (2, 200, fixtures.NameOwnershipTests().spawn(13, ENEMY, 'Enemy'))]
            fixture.write_context(through=1000)
            fixture.begin(1)
            monitor = fixture.monitor()
            monitor.poll()
            rows = [(1, 1500, packet())]
            fixture.damage([], partial=capture_bytes(rows)[32:70])
            self.assertEqual(monitor.poll()['measurement']['report']['counts']['records'], 0)
            fixture.damage(rows)
            pending = monitor.poll()
            self.assertEqual(pending['measurement']['pending_records'], 1)
            self.assertEqual(pending['measurement']['report']['counts']['records'], 0)
            fixture.write_context(through=2000)
            self.assertEqual(monitor.poll()['measurement']['report']['counts']['positive_sum'], 100)
            fixture.write_context(through=15000)
            idle = monitor.poll()
            self.assertEqual(idle['measurement']['activity']['reason'], 'idle_timeout')
            self.assertEqual(idle['measurement']['report']['timing']['elapsed_seconds'], 13.5)
            fixture.damage(rows + [(2, 2000, packet())])
            updated = monitor.poll()
            self.assertEqual(updated['measurement']['report']['counts']['positive_sum'], 200)
            self.assertEqual(updated['measurement']['report']['timing']['elapsed_seconds'], 13.5)
            self.assertEqual(len(updated['measurement']['report']['encounters']), 1)
            fixture.stop(2100)
            self.assertTrue(monitor.poll()['measurement']['complete'])
            before_bytes = monitor.segments[1].reader.bytes_read
            monitor.poll()
            self.assertEqual(monitor.segments[1].reader.bytes_read, before_bytes)

    def test_map_change_without_damage_updates_activity_and_stale_context_is_not_live(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = RuntimeFixture(root)
            fixture.write_context(through=2000)
            fixture.begin(1)
            fixture.damage([(1, 1000, packet())])
            monitor = fixture.monitor()
            self.assertTrue(monitor.poll()['measurement']['activity']['active'])
            fixture.identities.append((1, 1500, identity_packet(3, 0, SELF)))
            fixture.write_context(through=2500)
            snapshot = monitor.poll()
            self.assertEqual(snapshot['measurement']['activity']['reason'], 'map_transfer')
            fixture.now += 60_000_000
            fixture.write_state()  # Worker heartbeat advances; identity publication does not.
            self.assertFalse(monitor.poll()['identity']['is_live'])

    def test_pause_pet_change_fast_segments_relaunch_and_manifest_switch(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = RuntimeFixture(root)
            factory = fixtures.NameOwnershipTests()
            fixture.identities = [(1, 100, identity_packet(6, 0, SELF)),
                (2, 200, factory.spawn(13, ENEMY, 'Enemy')),
                (3, 300, factory.spawn(17, PET, 'OldPet', SELF.object_id))]
            fixture.write_context(through=1000)
            fixture.begin(1)
            fixture.damage([(1, 950, packet(PET))])
            monitor = fixture.monitor()
            self.assertEqual(monitor.poll()['measurement']['report']['roles']['self']['damage_dealt'], 100)
            fixture.stop(1100)
            fixture.write_context(through=1200)
            self.assertTrue(monitor.poll()['measurement']['complete'])
            new_pet = type(PET)(78, 6, 8)
            fixture.identities.append((4, 1300, factory.spawn(17, new_pet, 'NewPet', SELF.object_id)))
            fixture.write_context(through=1400)
            paused = monitor.poll()
            self.assertFalse(paused['effective_recording'])
            self.assertIn('NewPet', [entity['name'] for entity in paused['identity']['state']['entities']])
            fixture.begin(2, 1500)
            fixture.damage([(1, 1600, packet(new_pet, value=300))])
            fixture.stop(1700)
            fixture.write_context(through=1800)
            fixture.begin(3, 1900)  # Segment 2 started and stopped between polls.
            fixture.damage([(1, 2000, packet(new_pet, value=500))])
            fixture.write_context(through=2100)
            latest = monitor.poll()
            self.assertEqual(latest['measurement']['report']['roles']['self']['damage_dealt'], 500)
            self.assertEqual([entry['segment_number'] for entry in latest['history']], [1, 2])
            fixture.stop(2200)
            fixture.write_context(through=2300)
            monitor.poll()
            fixture.start_run(200)
            restarted = monitor.poll()
            self.assertIsNone(restarted['measurement'])
            self.assertIsNone(restarted['identity']['state']['self'])
            self.assertEqual(restarted['identity']['state']['entities'], [])
            with tempfile.TemporaryDirectory() as other:
                other_fixture = RuntimeFixture(other)
                manifest = Path(root) / 'build.json'
                manifest.write_text(json.dumps({'session': root}))
                automatic = RuntimeMonitor(manifest=manifest, process_probe=fixture.probe, clock=lambda: fixture.now)
                self.assertEqual(automatic.poll()['pid'], 200)
                manifest.write_text(json.dumps({'session': other}))
                switched = automatic.poll()
                self.assertEqual(switched['pid'], 100)
                self.assertIsNone(switched['identity']['state']['self'])

    def test_stale_heartbeat_dead_worker_and_pid_reuse_never_report_recording(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = RuntimeFixture(root)
            fixture.write_context(through=1000)
            fixture.begin(1)
            monitor = fixture.monitor()
            self.assertTrue(monitor.poll()['effective_recording'])
            fixture.now += 60_000_000
            self.assertFalse(monitor.poll()['effective_recording'])
            fixture.write_state()
            fixture.dead = True
            dead = monitor.poll()
            self.assertEqual(dead['state'], 'worker_unavailable')
            self.assertFalse(dead['identity']['is_live'])
            fixture.dead = False
            wrong_process = RuntimeMonitor(root, process_probe=lambda pid: {'alive': True, 'name': 'pso2',
                'started_filetime': fixture.now + 1}, clock=lambda: fixture.now)
            self.assertFalse(wrong_process.poll()['effective_recording'])

    def test_paths_corruption_and_truncation_fail_closed(self):
        for failure in ('path', 'truncate', 'sequence', 'count'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as root:
                fixture = RuntimeFixture(root)
                fixture.write_context(through=5000)
                fixture.begin(1)
                fixture.damage([(1, 1000, packet())])
                monitor = fixture.monitor()
                self.assertTrue(monitor.poll()['effective_recording'])
                if failure == 'path':
                    fixture.state['capture_directory'] = str(Path(root).parent / 'escape')
                    fixture.write_state()
                elif failure == 'truncate':
                    (fixture.segment / 'capture.opcap').write_bytes(capture_bytes([]))
                elif failure == 'sequence':
                    fixture.damage([(1, 1000, packet()), (1, 2000, packet())])
                else:
                    fixture.damage_status['written'] = 0
                    (fixture.segment / 'capture-status.json').write_text(json.dumps(fixture.damage_status))
                bad = monitor.poll()
                self.assertEqual(bad['state'], 'read_error')
                self.assertFalse(bad['effective_recording'])

    def test_saved_real_run_matches_replay_and_is_historical_after_shutdown(self):
        saved_root = Path(__file__).resolve().parents[1] / 'native_capture/captures/classic-auto-fixture-001'
        saved_run = saved_root / 'run-100000000000000001-1-0'
        if not (saved_run / 'segment-000001/capture.opcap').exists():
            self.skipTest('private Maron capture is not present')
        # The build's latest-run pointer changes during real tests. Copy the
        # specific completed recording and give only that copy a runtime index.
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        root = Path(work.name)
        run = root / saved_run.name
        shutil.copytree(saved_run, run)
        context = json.loads((run / 'identity-context/context-status.json').read_text(encoding='utf-8-sig'))
        state = {'control_abi': 1, 'pid': 10552, 'run_name': run.name, 'run_directory': str(run),
            'classic_hash_required': context['source_sha256'], 'state_filetime': utc_filetime(),
            'segment_number': 1, 'capture_directory': str(run / 'segment-000001'),
            'state': 'shutdown', 'recording': False, 'identity_observing': False, 'identity_context_error': 0}
        (root / 'autostart-state.json').write_text(json.dumps(state), encoding='utf-8')
        monitor = RuntimeMonitor(root, process_probe=lambda pid: {'alive': False})
        snapshot = monitor.poll()
        self.assertEqual(snapshot['state'], 'shutdown')
        self.assertTrue(snapshot['measurement']['complete'])
        self.assertFalse(snapshot['effective_recording'])
        self.assertFalse(snapshot['identity']['is_live'])
        self.assertEqual(snapshot['measurement']['report']['roles']['self']['damage_dealt'], 92341)
        capture = Path(snapshot['measurement']['capture_path'])
        self.assertEqual(monitor.segments[1].report(full=True), replay(capture))
        digest = hashlib.sha256(capture.read_bytes()).hexdigest()
        result = subprocess.run([sys.executable, '-m', 'tools.combat_core.monitor', '--root', str(root), '--once'],
            capture_output=True, encoding='utf-8', timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['measurement']['report']['counts']['records'], 15)
        self.assertEqual(hashlib.sha256(capture.read_bytes()).hexdigest(), digest)


HOST = Path(__file__).resolve().parents[1] / 'native_capture/bin/AutoRuntimeTestHost.exe'
PWSH = shutil.which('pwsh')


@unittest.skipUnless(os.name == 'nt' and HOST.exists() and PWSH, 'Windows runtime fixture and PowerShell required')
class NativeMonitorTests(unittest.TestCase):
    def wait_snapshot(self, monitor, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = monitor.poll()
            if predicate(snapshot):
                return snapshot
            time.sleep(0.05)
        self.fail(f'monitor did not reach expected state: {snapshot}')

    def control(self, root, action):
        result = subprocess.run([PWSH, '-NoProfile', '-File', str(HOST.parent.parent / 'runtime_client.ps1'),
            '-Root', str(root), '-Action', action, '-ExpectedProcessName', 'AutoRuntimeTestHost'],
            capture_output=True, text=True, timeout=18, creationflags=0x08000000)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_real_process_probe_control_worker_pause_resume_and_relaunch(self):
        with tempfile.TemporaryDirectory(prefix='monitor-') as root:
            process = subprocess.Popen([str(HOST), root], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                       creationflags=0x08000000)
            monitor = RuntimeMonitor(root, expected_process='AutoRuntimeTestHost')
            try:
                ready = self.wait_snapshot(monitor, lambda snapshot: snapshot['state'] == 'binding_ready_waiting_for_start')
                self.assertTrue(ready['worker_verified'])
                self.assertTrue(ready['identity']['is_live'])
                self.assertTrue(probe_process(process.pid)['alive'])
                self.control(root, 'Start')
                self.wait_snapshot(monitor, lambda snapshot: snapshot['effective_recording'])
                self.control(root, 'Stop')
                self.wait_snapshot(monitor, lambda snapshot: snapshot['measurement'] and snapshot['measurement']['complete'])
                self.control(root, 'Start')
                self.wait_snapshot(monitor, lambda snapshot: snapshot['measurement'] and
                    snapshot['measurement']['segment_number'] == 2 and snapshot['effective_recording'])
                self.control(root, 'Shutdown')
                process.wait(timeout=5)
                ended = self.wait_snapshot(monitor, lambda snapshot: snapshot['state'] == 'shutdown')
                self.assertFalse(ended['effective_recording'])
                self.assertTrue(ended['measurement']['complete'])
                process.stderr.close()
                process = subprocess.Popen([str(HOST), root], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                           creationflags=0x08000000)
                restarted = self.wait_snapshot(monitor, lambda snapshot: snapshot.get('run_name') != ready['run_name'] and
                                               snapshot['state'] == 'binding_ready_waiting_for_start')
                self.assertIsNone(restarted['measurement'])
                self.control(root, 'Shutdown')
                process.wait(timeout=5)
                self.assertEqual(process.returncode, 0)
            finally:
                if process.poll() is None:
                    process.kill()  # This owned fixture only, never the game.
                    process.wait(timeout=5)
                process.stderr.close()


if __name__ == '__main__':
    unittest.main()
