import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

from .deployment import Deployment, GameVersionMismatch, application_identity, PATH_MARKER, PATH_BYTES, RESOURCES, digest, patched_loader
from .files import exclusive_file
from .. import paths


class PortableTests(unittest.TestCase):
    def setUp(self):
        # General deployment tests never request UAC or mutate fixture ACLs.
        acl_patch = patch('tools.portable.deployment.grant_sub_access')
        self.acl = acl_patch.start()
        self.addCleanup(acl_patch.stop)

    def fixture(self, root):
        resources = root / 'resources'
        resources.mkdir()
        template = b'MZ' + PATH_MARKER.encode('utf-16le').ljust(PATH_BYTES, b'\0') + b'end'
        (resources / 'Loader.dll').write_bytes(template)
        (resources / 'Capture.dll').write_bytes(b'capture')
        build = {'core_sha256': digest(b'capture'), 'loader_sha256': digest(template),
            'classic_sha256': digest(b'classic'), 'bridge_sha256': digest(b'bridge')}
        (resources / 'build.json').write_text(json.dumps(build))
        folder = root / 'game/pso2_bin/sub'
        folder.mkdir(parents=True)
        (folder / 'pso2classic.dll').write_bytes(b'classic')
        (folder / 'pso2bridge.dll').write_bytes(b'bridge')
        deployment = Deployment(root / 'app', resources, running=lambda: False)
        deployment.prepare()
        return deployment, folder

    def test_bridge_and_classic_updates_are_named_and_block_start_without_blocking_stop(self):
        from ..gui.backend import Backend
        with tempfile.TemporaryDirectory() as work:
            d, folder = self.fixture(Path(work))
            d.install(folder)
            old_loader = (folder/'eos_gaming.dll').read_bytes()
            backend = Backend(deployment=d, controller=Mock())
            backend._execute('CheckLoader', None, folder)
            self.assertTrue(backend.events.empty())
            (folder/'pso2bridge.dll').write_bytes(b'new bridge')
            (folder/'pso2classic.dll').write_bytes(b'new classic')
            with self.assertRaises(GameVersionMismatch) as error:
                d.check(folder)
            for value in ('pso2bridge.dll', 'pso2classic.dll', digest(b'new bridge'), digest(b'new classic')):
                self.assertIn(value, str(error.exception))
            backend._watch_game(force=True)
            self.assertEqual(backend.events.get_nowait()['type'], 'game_version_error')
            backend._watch_game(force=True)
            self.assertTrue(backend.events.empty())  # No repeated modal every poll.
            for action in ('Start', 'Next', 'Reset'):
                with self.assertRaises(ValueError):
                    backend._execute(action, {}, None)
            backend.controller.invoke.assert_not_called()
            snapshot = {'root':'fixture', 'run_name':'run'}
            with patch.object(backend, '_poll', return_value=snapshot):
                backend._execute('Stop', snapshot, None)
            backend.controller.invoke.assert_called_once_with(snapshot, 'Stop')
            with self.assertRaises(GameVersionMismatch):
                d.install(folder)
            self.assertEqual((folder/'eos_gaming.dll').read_bytes(), old_loader)
            (folder/'pso2bridge.dll').write_bytes(b'bridge')
            (folder/'pso2classic.dll').write_bytes(b'classic')
            backend._watch_game(force=True)
            self.assertIsNone(backend.game_version_error)

    def test_background_game_check_hashes_only_after_stat_change_and_force_always_reads(self):
        with tempfile.TemporaryDirectory() as work:
            d, folder = self.fixture(Path(work))
            d.verify_game(folder)
            with patch('tools.portable.deployment.hashlib.file_digest', wraps=__import__('hashlib').file_digest) as hashing:
                d.verify_game(folder, force=False)
                hashing.assert_not_called()
                previous = (folder/'pso2classic.dll').stat()
                (folder/'pso2classic.dll').write_bytes(b'updated')
                # Equal-sized fixture writes can share a Windows timestamp tick.
                os.utime(folder/'pso2classic.dll', ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000))
                with self.assertRaises(GameVersionMismatch):
                    d.verify_game(folder, force=False)
                self.assertEqual(hashing.call_count, 2)
                with self.assertRaises(GameVersionMismatch):
                    d.verify_game(folder, force=False)
                self.assertEqual(hashing.call_count, 2)
                with self.assertRaises(GameVersionMismatch):
                    d.verify_game(folder, force=True)
                self.assertEqual(hashing.call_count, 4)

    def test_exe_only_update_requires_selection_until_successful_install_even_with_same_native_build(self):
        from ..gui.backend import Backend
        with tempfile.TemporaryDirectory() as work:
            d, folder = self.fixture(Path(work))
            d.app_identity = 'exe:old'
            self.assertTrue(d.needs_reselection())
            d.install(folder)
            self.assertFalse(d.needs_reselection())
            updated = Deployment(d.home, d.resources, running=lambda: False, app_identity='exe:new')
            updated.prepare()
            updated.check(folder)  # Identical native build alone cannot detect this GUI-only update.
            self.assertTrue(updated.needs_reselection())
            backend = Backend(deployment=updated)
            backend._execute('CheckLoader', None, folder)
            self.assertEqual(backend.events.get_nowait()['type'], 'loader_reselect')
            self.assertTrue(updated.needs_reselection())  # Cancelling selection is not acknowledgement.
            updated.running = lambda: True
            with self.assertRaises(ValueError):
                updated.install(folder)
            self.assertTrue(updated.needs_reselection())
            updated.running = lambda: False
            backend._execute('InstallLoader', None, folder)
            self.assertEqual(backend.events.get_nowait()['type'], 'loader_installed')
            self.assertFalse(updated.needs_reselection())
            self.assertFalse(Deployment(d.home, d.resources, app_identity='exe:new').needs_reselection())
            record = json.loads(updated.record.read_text(encoding='utf-8-sig'))
            record.pop('application_id')
            updated.record.write_text(json.dumps(record))
            self.assertTrue(updated.needs_reselection())  # Migrate pre-version ledgers once.

    def test_frozen_identity_tracks_exe_bytes_and_survives_move(self):
        with tempfile.TemporaryDirectory() as work:
            exe = Path(work)/'LazyParse.exe'
            exe.write_bytes(b'release 1')
            with patch('tools.portable.deployment.sys.frozen', True, create=True), patch('tools.portable.deployment.sys.executable', str(exe)):
                original = application_identity()
                exe.write_bytes(b'release 2')
                self.assertNotEqual(application_identity(), original)
                exe.write_bytes(b'release 1')
                moved = exe.with_name('moved.exe')
                exe.rename(moved)
                with patch('tools.portable.deployment.sys.executable', str(moved)):
                    self.assertEqual(application_identity(), original)

    def test_install_relaunch_missing_changed_and_owned_removal_preserve_game_files(self):
        with tempfile.TemporaryDirectory() as work:
            d, folder = self.fixture(Path(work))
            original = {p: p.read_bytes() for p in folder.glob('*.dll')}
            with self.assertRaises(ValueError):
                d.check(folder)
            self.assertEqual(d.install(folder), str(folder.resolve()))
            d.check(folder)
            record = json.loads(d.record.read_text(encoding='utf-8-sig'))
            self.assertEqual(record['sha256'], digest((folder / 'eos_gaming.dll').read_bytes()))
            Deployment(d.home, d.resources).prepare()  # Startup does not mutate game files.
            d.install(folder)
            (folder / 'eos_gaming.dll').write_bytes(b'foreign')
            for operation in (lambda: d.check(folder), lambda: d.install(folder), d.remove):
                with self.assertRaises(ValueError):
                    operation()
                self.assertEqual((folder / 'eos_gaming.dll').read_bytes(), b'foreign')
            (folder / 'eos_gaming.dll').write_bytes(Path(d.build['loader']).read_bytes())
            d.remove()
            self.assertFalse((folder / 'eos_gaming.dll').exists())
            self.assertTrue(all(p.read_bytes() == value for p, value in original.items()))

    def test_running_game_wrong_folder_version_and_locked_install_are_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            d, folder = self.fixture(Path(work))
            d.running = lambda: True
            with self.assertRaises(ValueError):
                d.install(folder)
            d.running = lambda: False
            with self.assertRaises(ValueError):
                d.install(folder.parent)
            (folder / 'pso2classic.dll').write_bytes(b'updated game')
            with self.assertRaises(ValueError):
                d.install(folder)
            (folder / 'pso2classic.dll').write_bytes(b'classic')
            with exclusive_file(d.config / 'deployment.lock'):
                with self.assertRaises(OSError):
                    d.install(folder)
            self.assertFalse((folder / 'eos_gaming.dll').exists())

    def test_failed_ledger_write_rolls_back_added_or_previously_owned_dll(self):
        with tempfile.TemporaryDirectory() as work:
            d, folder = self.fixture(Path(work))
            with patch('tools.portable.deployment.atomic_json', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    d.install(folder)
            self.assertFalse((folder / 'eos_gaming.dll').exists())
            d.install(folder)
            old = (folder / 'eos_gaming.dll').read_bytes()
            with patch('tools.portable.deployment.atomic_json', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    d.install(folder)
            self.assertEqual((folder / 'eos_gaming.dll').read_bytes(), old)

    def test_acl_finishes_before_any_game_directory_write_and_runs_on_reinstall(self):
        with tempfile.TemporaryDirectory() as work:
            d, folder = self.fixture(Path(work))
            target = folder / 'eos_gaming.dll'
            def grant(path):
                self.assertEqual(path, folder.resolve())
                self.assertFalse(list(folder.glob('LazyParse-*.tmp')))
                self.assertEqual(target.exists(), self.acl.call_count > 1)
            self.acl.side_effect = grant
            d.install(folder)
            d.install(folder)
            self.assertEqual(self.acl.call_count, 2)

    def test_acl_failure_or_uac_cancel_preserves_existing_loader_and_ledger(self):
        with tempfile.TemporaryDirectory() as work:
            d, folder = self.fixture(Path(work))
            target = folder / 'eos_gaming.dll'
            for installed in (False, True):
                if installed:
                    self.acl.side_effect = None
                    d.install(folder)
                old = target.read_bytes() if installed else None
                ledger = d.record.read_bytes() if installed else None
                for error in (OSError('ACL failed'), ValueError('UAC cancelled')):
                    self.acl.side_effect = error
                    with self.assertRaises(type(error)):
                        d.install(folder)
                    self.assertEqual(target.read_bytes() if target.exists() else None, old)
                    self.assertEqual(d.record.read_bytes() if d.record.exists() else None, ledger)
                    self.assertFalse(list(folder.glob('LazyParse-*.tmp')))

    def test_game_start_or_update_during_acl_blocks_dll_write(self):
        with tempfile.TemporaryDirectory() as work:
            d, folder = self.fixture(Path(work))
            def game_starts(_):
                d.running = lambda: True
            self.acl.side_effect = game_starts
            with self.assertRaises(ValueError):
                d.install(folder)
            self.assertFalse((folder / 'eos_gaming.dll').exists())
            d.running = lambda: False
            self.acl.side_effect = lambda _: (folder/'pso2bridge.dll').write_bytes(b'update')
            with self.assertRaises(GameVersionMismatch):
                d.install(folder)
            self.assertFalse((folder / 'eos_gaming.dll').exists())

    def test_rejected_foreign_dll_and_game_version_do_not_change_acl(self):
        with tempfile.TemporaryDirectory() as work:
            d, folder = self.fixture(Path(work))
            target = folder / 'eos_gaming.dll'
            target.write_bytes(b'foreign')
            with self.assertRaises(ValueError):
                d.install(folder)
            self.acl.assert_not_called()
            target.unlink()
            (folder/'pso2classic.dll').write_bytes(b'update')
            with self.assertRaises(GameVersionMismatch):
                d.install(folder)
            self.acl.assert_not_called()

    def test_relocation_rewrites_only_owned_loader_and_config_is_not_reset(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            d, folder = self.fixture(root)
            d.install(folder)
            old_loader = (folder / 'eos_gaming.dll').read_bytes()
            new_home = root / '移動先 with spaces'
            shutil.copytree(d.home, new_home)
            (new_home / 'config/native-install.json').unlink()  # Also support moving only the exe.
            moved = Deployment(new_home, d.resources, running=lambda: False)
            moved.prepare()
            with self.assertRaises(ValueError):
                moved.check(folder)
            moved.install(folder)
            self.assertNotEqual(old_loader, (folder / 'eos_gaming.dll').read_bytes())
            self.assertIn(str(Path(moved.build['core'])).encode('utf-16le'), (folder / 'eos_gaming.dll').read_bytes())
            self.assertEqual(moved.build['session'], str(new_home / 'logs/captures'))

    def test_first_start_preserves_edits_history_and_index_when_lowercasing_logs(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            (root / 'Quests.csv').write_text('quest_id,name\n1,test\n')
            (root / 'config').mkdir()
            edited = root / 'config/Quests.csv'
            edited.write_text('quest_id,name\n1,edited\n')
            (root / 'Logs').mkdir()
            index = b'{"schema_version":1,"files":{}}'
            (root / 'Logs/.auto-text-index.json').write_bytes(index)
            (root / 'Logs/old.txt').write_bytes(b'old text')
            with patch.multiple(paths, HOME=root, BUNDLE=root, CONFIG=root/'config', LOGS=root/'logs', HISTORY=root/'logs/history'):
                paths.initialize()
                paths.initialize()
            self.assertIn('edited', edited.read_text())
            self.assertEqual((root/'logs/.auto-text-index.json').read_bytes(), index)
            self.assertEqual((root/'logs/old.txt').read_bytes(), b'old text')
            self.assertIn('logs', [p.name for p in root.iterdir()])
            self.assertNotIn('Logs', [p.name for p in root.iterdir()])

    @unittest.skipUnless(os.name == 'nt', 'Windows desktop required')
    def test_gui_folder_selection_install_persistence_and_missing_loader_reselection(self):
        import tkinter as tk
        from ..gui.app import Application
        from ..gui.backend import Backend
        from ..gui.settings import Settings
        with tempfile.TemporaryDirectory() as work:
            folder = str(Path(work) / 'game/pso2_bin/sub')
            deployment = Mock()
            deployment.needs_reselection.return_value = False
            deployment.install.return_value = folder
            backend = Backend(deployment=deployment)
            root = tk.Tk()
            app = Application(root, backend, settings_path=Path(work)/'preferences.json', start_backend=False)
            try:
                with patch('tools.gui.app.filedialog.askdirectory', return_value=folder):
                    app.ensure_loader()
                action, requested, path = backend.requests.get_nowait()
                self.assertEqual((action, path), ('InstallLoader', folder))
                backend._execute(action, requested, path)
                root.after_cancel(app.consume_timer)
                app._consume()
                self.assertEqual(Settings.load(app.settings_path).game_directory, folder)
                app.ensure_loader()
                action, requested, path = backend.requests.get_nowait()
                self.assertEqual(action, 'CheckLoader')
                deployment.check.side_effect = ValueError('DLL missing')
                backend._execute(action, requested, path)
                with patch('tools.gui.app.messagebox.showerror') as error, patch('tools.gui.app.filedialog.askdirectory', return_value='') as choose:
                    root.after_cancel(app.consume_timer)
                    app._consume()
                    root.update()
                    error.assert_called_once()
                    choose.assert_called_once()
            finally:
                app.close()

    @unittest.skipUnless(os.name == 'nt', 'Windows desktop required')
    def test_gui_update_reopens_folder_dialog_and_cancel_does_not_accept_update(self):
        import tkinter as tk
        from ..gui.app import Application
        from ..gui.backend import Backend
        with tempfile.TemporaryDirectory() as work:
            d, folder = self.fixture(Path(work))
            d.app_identity = 'exe:old'
            d.install(folder)
            d.app_identity = 'exe:new'
            backend = Backend(deployment=d)
            root = tk.Tk()
            app = Application(root, backend, settings_path=Path(work)/'preferences.json', start_backend=False)
            app.settings.game_directory = str(folder)
            try:
                app.ensure_loader()
                action, requested, path = backend.requests.get_nowait()
                backend._execute(action, requested, path)
                with patch('tools.gui.app.messagebox.showinfo') as info, patch('tools.gui.app.filedialog.askdirectory', return_value='') as choose:
                    root.after_cancel(app.consume_timer)
                    app._consume()
                    root.update()
                    info.assert_called_once()
                    choose.assert_called_once()
                self.assertTrue(d.needs_reselection())
                self.assertTrue(backend.requests.empty())
            finally:
                app.close()

    @unittest.skipUnless((RESOURCES / 'LoaderTest.dll').exists(), 'portable native mock build required')
    def test_native_worker_survives_bootstrap_unload_unicode_relocation_and_relaunch(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work) / '移動後 folder'
            directory = root / 'config/native/test'
            directory.mkdir(parents=True)
            core = directory / 'Capture.dll'
            shutil.copyfile(RESOURCES / 'CaptureTest.dll', core)
            captures = root / 'logs/captures'
            captures.mkdir(parents=True)
            loader = root / 'eos_gaming.dll'
            loader.write_bytes(patched_loader((RESOURCES/'LoaderTest.dll').read_bytes(), core))
            for _ in range(2):
                result = subprocess.run([str(RESOURCES/'AutoCaptureTestHost.exe'), str(loader), str(captures)],
                    capture_output=True, text=True, timeout=15, creationflags=0x08000000)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(len(list(captures.glob('run-*'))), 2)
            state = json.loads((captures/'autostart-state.json').read_text(encoding='utf-8-sig'))
            for field in ('guard_owner_thread_id', 'guard_failed_thread_id', 'guard_failed_thread_exit_code',
                          'guard_failed_thread_query_error', 'guard_operation_error', 'guard_resume_error'):
                self.assertEqual(state[field], 0)

    @unittest.skipUnless((RESOURCES / 'AutoRuntimeTestHost.exe').exists(), 'current control-worker build required')
    def test_current_worker_keeps_maintenance_errors_visible_and_recovers(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            process = subprocess.Popen([str(RESOURCES/'AutoRuntimeTestHost.exe'), str(root)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000)
            def wait(predicate):
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    self.assertIsNone(process.poll())
                    try:
                        state = json.loads((root/'autostart-state.json').read_text(encoding='utf-8-sig'))
                        if predicate(state):
                            return state
                    except (OSError, ValueError):
                        pass
                    time.sleep(.02)
                self.fail('current control worker timed out')
            def command(state, number, action):
                target = Path(state['run_directory'])/'control.command'
                temporary = target.with_suffix('.tmp')
                temporary.write_bytes(f'OPC-CONTROL-1\n{number}\n{action}\n'.encode('ascii'))
                os.replace(temporary, target)
            try:
                ready = wait(lambda s: s['state']=='binding_ready_waiting_for_start')
                command(ready, 1, 'start')
                wait(lambda s: s['recording'])
                (root/'disconnect.fixture').touch()
                missing = wait(lambda s: s['state']=='waiting_for_binding')
                self.assertEqual(missing['win32_error'], 21)
                self.assertFalse(missing['recording'])
                self.assertTrue(missing['desired_recording'])
                (root/'disconnect.fixture').unlink()
                restored = wait(lambda s: s['recording'])
                command(restored, 2, 'stop')
                paused = wait(lambda s: s['state']=='paused')
                self.assertTrue(paused['identity_observing'])
                command(paused, 3, 'shutdown')
                self.assertEqual(process.wait(timeout=5), 0)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)

    @unittest.skipUnless((RESOURCES / 'CaptureBindingTest.dll').exists(), 'portable binding mock build required')
    def test_classic_module_reload_revalidates_and_preserves_observation_and_forwarding(self):
        from ..native_capture.read_capture import records
        with tempfile.TemporaryDirectory() as work:
            output = Path(work) / 'reload'
            result = subprocess.run([str(RESOURCES/'ProfileReloadTestHost.exe'),
                str(RESOURCES/'CaptureBindingTest.dll'), str(output)],
                capture_output=True, text=True, timeout=20, creationflags=0x08000000)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for part, damage in [('part-1', [1001]), ('part-2', [2002]), ('part-3', [3001, 3002])]:
                self.assertEqual([r['damage'] for r in records(output/part/'capture.opcap')], damage)
                status = json.loads((output/part/'capture-status.json').read_text(encoding='utf-8-sig'))
                self.assertEqual(status['io_error'], 0)
                self.assertEqual(status['dropped'], 0)
                self.assertEqual(status['virtual_protect_calls'], 0)
                self.assertTrue(status['stopped'])
            context = json.loads((output/'identity-context/context-status.json').read_text(encoding='utf-8-sig'))
            self.assertEqual(context['connection_gaps'], 3)
            self.assertEqual(context['binding_generation'], 4)
            self.assertTrue(context['stopped'])
            self.assertEqual(context['io_error'], 0)

    @unittest.skipUnless((RESOURCES / 'CaptureBindingTest.dll').exists(), 'portable binding mock build required')
    def test_current_binding_profile_restart_replacement_and_original_forwarding(self):
        from ..native_capture.read_capture import records
        with tempfile.TemporaryDirectory() as work:
            output = Path(work) / 'run'
            result = subprocess.run([str(RESOURCES/'ContinuityTestHost.exe'),
                str(RESOURCES/'CaptureBindingTest.dll'), str(output)],
                capture_output=True, text=True, timeout=30, creationflags=0x08000000)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            first = list(records(output/'part-1/capture.opcap'))
            second = list(records(output/'part-2/capture.opcap'))
            self.assertEqual([r['damage'] for r in first], list(range(1, 11)))
            self.assertEqual([r['damage'] for r in second if r['damage'] < 900000], list(range(11, 51)))
            for part, rows in [('part-1', first), ('part-2', second)]:
                status = json.loads((output/part/'capture-status.json').read_text(encoding='utf-8-sig'))
                self.assertEqual(status['written'], len(rows))
                self.assertEqual(status['dropped'], 0)
                self.assertEqual(status['io_error'], 0)
                self.assertEqual(status['virtual_protect_calls'], 0)
                self.assertTrue(status['stopped'])
                build = json.loads((RESOURCES/'build.json').read_text(encoding='utf-8-sig'))
                profile_path = Path(__file__).resolve().parents[1]/'native_capture/profiles/classic-2026-11-18.json'
                profile = json.loads(profile_path.read_text(encoding='utf-8-sig'))
                self.assertEqual(build['classic_sha256'], profile['classic_sha256'])
                self.assertEqual(int(status['callback_rva'], 0), int(profile['layout']['callback_rva'], 0))


if __name__ == '__main__':
    unittest.main()
