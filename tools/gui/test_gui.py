from copy import deepcopy
import json
from pathlib import Path
import queue
import subprocess
import tempfile
import time
import unittest
import os

from tools.gui.backend import Backend, RuntimeController, saved_snapshot
from tools.gui.control import command_error_message, preparation_error
from tools.gui.settings import Settings
from tools.gui.viewmodel import actor_rows, attack_rows, source_rows, status_view, clipboard_summary
from tools.gui.viewmodel import display_elapsed, elapsed_label
from tools.gui.timing import with_dps_mode, QUEST_DEFINITION, DAMAGE_DEFINITION
from tools.combat_core.live import RuntimeMonitor

FIXTURE = Path(__file__).resolve().parents[1] / 'native_capture/captures/classic-auto-fixture-001/run-100000000000000001-1-0/segment-000001/capture.opcap'
HOST = Path(__file__).resolve().parents[1] / 'native_capture/bin/AutoRuntimeTestHost.exe'


class ControlErrorTests(unittest.TestCase):
    def test_initial_guard_denial_is_visible_without_an_identity_journal(self):
        snapshot = {'state':'waiting_for_publication', 'worker_verified':False, 'heartbeat_fresh':False,
                    'runtime_heartbeat_fresh':True,'runtime_worker_probe':{'alive':True},
                    'runtime':{'state':'identity_context_error','identity_context_error':5,
                               'guard_error':5,'guard_stage':3,'callbacks_total':0}}
        self.assertIn('Win32=5', preparation_error(snapshot))
        self.assertIn('スレッドの一時停止', preparation_error(snapshot))
        self.assertEqual(status_view(snapshot)[0], '識別情報の取得エラー')
        self.assertEqual(status_view(snapshot, saved=True)[0], '保存ログ')
        self.assertFalse(snapshot['worker_verified'])  # Display never relaxes verification.

    def test_startup_file_failure_does_not_invent_a_thread_failure_and_waiting_stays_waiting(self):
        snapshot = {'heartbeat_fresh':True,'runtime':{'identity_context_error':5,'guard_error':0,'guard_stage':3}}
        self.assertNotIn('スレッド', preparation_error(snapshot))
        self.assertIsNone(preparation_error({'runtime':{'identity_context_error':0}}))
        self.assertIsNone(preparation_error({'state':'waiting_for_runtime'}))

    def test_old_startup_error_is_not_displayed_as_current_after_dll_installation(self):
        snapshot = {'state':'waiting_for_publication','runtime_heartbeat_fresh':False,
                    'runtime':{'identity_context_error':5,'guard_error':5,'guard_stage':3}}
        self.assertIsNone(preparation_error(snapshot))
        self.assertNotEqual(status_view(snapshot)[0], '識別情報の取得エラー')
        self.assertIsNone(preparation_error({**snapshot,'runtime_heartbeat_fresh':True,
                                           'runtime_worker_probe':{'alive':False}}))

    def test_guard_denial_names_actual_stage_without_mislabeling_file_or_stop_errors(self):
        state = {'last_command':'start', 'last_command_error':5, 'binding_error':5,
                 'guard_error':5, 'guard_stage':3}
        self.assertIn('スレッドの一時停止', command_error_message(state))
        self.assertIn('Win32=5', command_error_message(state))
        self.assertNotIn('スレッド', command_error_message({**state, 'last_command':'stop'}))
        self.assertNotIn('スレッド', command_error_message({**state, 'guard_error':0}))
        self.assertNotIn('スレッド', command_error_message({'last_command_error':5}))


class TimingModeTests(unittest.TestCase):
    def test_excluded_burn_not_reintroduced_by_either_dps_mode(self):
        report = {'counts': {'positive_sum': 1300},
            'damage_totals': {'positive_sum': 300, 'excluded_positive_sum': 1000},
            'timing': {'definition': QUEST_DEFINITION, 'elapsed_seconds': 9},
            'encounters': [{'first_qpc': 0, 'last_qpc': 1000, 'qpc_frequency': 1000}],
            'roles': {'self': {'damage_dealt': 300}}}
        self.assertAlmostEqual(with_dps_mode(report, 'quest')['timing']['dps'], 300 / 9)
        self.assertEqual(with_dps_mode(report, 'damage')['timing']['dps'], 300)
        reopened = json.loads(json.dumps(with_dps_mode(report, 'damage')))
        self.assertAlmostEqual(with_dps_mode(reopened, 'quest')['timing']['dps'], 300 / 9)

    def test_switch_recalculates_all_totals_and_preserves_both_definitions_on_export(self):
        report = {'counts': {'positive_sum': 600},
            'timing': {'definition': QUEST_DEFINITION, 'elapsed_seconds': 100.999, 'spans': [{'end_qpc': 100999}]},
            'actors': [{'damage_dealt': 600}], 'attacks': [{'damage_dealt': 200}],
            'roles': {'self': {'damage_dealt': 600}},
            'encounters': [{'first_qpc': 10, 'last_qpc': 30, 'qpc_frequency': 10},
                           {'first_qpc': 800, 'last_qpc': 830, 'qpc_frequency': 10}],
            'quest_measurement': {'end_qpc': 100999, 'end_reason': 'clear_candidate'}}
        original = deepcopy(report)
        damage = with_dps_mode(report, 'damage')
        self.assertEqual(display_elapsed(damage), 5)
        self.assertEqual(damage['actors'][0]['dps'], 120)
        self.assertEqual(damage['attacks'][0]['dps'], 40)
        self.assertEqual(damage['roles']['self']['dps'], 120)
        self.assertEqual(damage['quest_measurement'], original['quest_measurement'])
        reopened = json.loads(json.dumps(damage))
        quest = with_dps_mode(reopened, 'quest')
        self.assertEqual(display_elapsed(quest), 100.999)
        self.assertAlmostEqual(quest['actors'][0]['dps'], 600 / 100.999)
        self.assertEqual(with_dps_mode(quest, 'damage'), damage)
        self.assertEqual(report, original)
        self.assertEqual(elapsed_label(100.999, False), '100')
        self.assertEqual(elapsed_label(100.999, True), '101.00')
        self.assertEqual(display_elapsed(quest), 100.999)  # Precision changes presentation only.

    def test_legacy_json_and_single_hit_do_not_invent_time_or_infinite_dps(self):
        old = {'timing': {'definition': DAMAGE_DEFINITION, 'elapsed_seconds': 3},
               'counts': {'positive_sum': 10}, 'roles': {'self': {'damage_dealt': 10}}}
        self.assertIsNone(display_elapsed(with_dps_mode(old, 'quest')))
        self.assertEqual(display_elapsed(with_dps_mode(old, 'damage')), 3)
        single = {'timing': {'definition': QUEST_DEFINITION, 'elapsed_seconds': 10},
                  'encounters': [{'first_qpc': 10, 'last_qpc': 10, 'qpc_frequency': 10}],
                  'counts': {'positive_sum': 10}, 'roles': {'self': {'damage_dealt': 10}}}
        damage = with_dps_mode(single, 'damage')
        self.assertEqual(display_elapsed(damage), 0)
        self.assertIsNone(damage['roles']['self']['dps'])
        self.assertEqual(elapsed_label(None, False), '—')


@unittest.skipUnless(FIXTURE.exists(), 'private live recording required')
class ViewModelTests(unittest.TestCase):
    def test_old_active_span_json_does_not_present_its_dps_as_continuous(self):
        report = saved_snapshot(FIXTURE)['measurement']['report']
        report['timing']['definition'] = 'sum_of_first_to_last_positive_event_spans_per_encounter'
        rows = actor_rows(report)
        self.assertIsNone(rows[0]['dps'])
        self.assertIn('Time —s', clipboard_summary(report, rows))
        self.assertEqual(rows[0]['damage_dealt'], 92341)

    def test_owner_credit_details_pet_ja_exclusion_and_crit_are_preserved(self):
        snapshot = saved_snapshot(FIXTURE)
        report = snapshot['measurement']['report']
        rows = actor_rows(report)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row['name'], row['damage_dealt'], row['damage_taken']), ('Demo', 92341, 0))
        self.assertIsNone(row['ja'])
        self.assertAlmostEqual(row['crit'], 300 / 13)
        self.assertEqual(row['hit_rates']['ja_excluded_hits'], 13)
        self.assertEqual(row['attack'], 'マロンブレイク')
        self.assertEqual(row['max_attack_id'], 115059488)
        self.assertEqual(sum(entry['damage_dealt'] for entry in attack_rows(report, row)), 92341)
        sources = source_rows(report, row)
        self.assertEqual(sorted(entry['damage'] for entry in sources), [132, 92209])
        self.assertIn('PetLongearD', [entry['name'] for entry in sources])
        self.assertIn('92,341', clipboard_summary(report, rows))
        self.assertEqual(status_view(snapshot, saved=True)[0], '保存ログ')

    def test_player_lifetimes_merge_and_only_me_keeps_party_share(self):
        report = saved_snapshot(FIXTURE)['measurement']['report']
        own = next(row for row in report['actors'] if row['role'] == 'self')
        another_life = deepcopy(own)
        another_life.update(actor_key='2/4:0:10000001#0', damage_dealt=100, hits_dealt=1, max_hit=100)
        another_life.pop('hit_rates')  # Older JSON lacks flag counts.
        other = deepcopy(own)
        other.update(object_id=999, actor_key='4:0:999', name='Other', role='other_player', damage_dealt=10000)
        report['actors'].extend([another_life, other])
        all_rows = actor_rows(report)
        self.assertEqual(len(all_rows), 2)
        own_row = next(row for row in all_rows if row['role'] == 'self')
        hidden = actor_rows(report, only_me=True, anonymize=True)
        self.assertEqual(hidden[0]['name'], '自分')
        self.assertEqual(hidden[0]['damage_dealt'], 92441)
        self.assertEqual(hidden[0]['share'], own_row['share'])
        self.assertLess(hidden[0]['share'], 100)

    def test_exported_report_reopens_and_background_open_does_not_enable_controls(self):
        with tempfile.TemporaryDirectory() as work:
            snapshot = saved_snapshot(FIXTURE)
            archive = Path(work) / 'saved.json'
            archive.write_text(json.dumps(snapshot['measurement']['report']))
            reopened = saved_snapshot(archive)
            self.assertEqual(reopened['measurement']['report']['counts'], snapshot['measurement']['report']['counts'])
            self.assertFalse(reopened['worker_verified'])
            with self.assertRaises(ValueError):
                RuntimeController().invoke(reopened, 'Start')
            archive.write_text('[]')
            with self.assertRaises(ValueError):
                saved_snapshot(archive)

    def test_current_name_updates_without_rewriting_damage(self):
        report = saved_snapshot(FIXTURE)['measurement']['report']
        before = deepcopy(report)
        own = next(row for row in report['actors'] if row['role'] == 'self')
        current = {**own, 'name': 'Changed'}
        rows = actor_rows(report, current_identities={'entities': [current]})
        self.assertEqual(rows[0]['name'], 'Changed')
        self.assertEqual(rows[0]['damage_dealt'], 92341)
        self.assertEqual(report, before)


@unittest.skipUnless(os.name == 'nt' and FIXTURE.exists(), 'Windows UI and live fixture required')
class WindowTests(unittest.TestCase):
    def test_real_widgets_details_filters_and_saved_mode_control_guard(self):
        import tkinter as tk
        from tools.gui.app import Application
        from tools.gui.windows import dpi_awareness
        class Stub:
            interval = .5
            updates = queue.Queue()
            events = queue.Queue()
            requests = []
            def close(self):
                pass
            def submit(self, *args, **kwargs):
                self.requests.append((args, kwargs))
        dpi_awareness()
        with tempfile.TemporaryDirectory() as work:
            root = tk.Tk()
            backend = Stub()
            app = Application(root, backend, settings_path=Path(work) / 'preferences.json', start_backend=False)
            try:
                app.saved = saved_snapshot(FIXTURE)
                app.snapshot = app.saved
                root.update()
                app._clear_notice()
                root.update()
                self.assertEqual(app.rows[0]['damage_dealt'], 92341)
                self.assertIn('92,341', app.summary.cget('text'))
                self.assertLess(app.summary.winfo_rooty(), root.winfo_rooty() + root.winfo_height())
                self.assertEqual(str(app.start_button.cget('state')), 'disabled')
                app.command('Start')
                self.assertEqual(backend.requests, [])
                app.selected = app.rows[0]['key']
                app._open_selected()
                root.update()
                detail = app.details[app.selected]
                self.assertEqual(len(detail.attacks.get_children()), 4)
                first = detail.attacks.item(detail.attacks.get_children()[0], 'values')
                self.assertEqual(first[:3], ('マロンブレイク', '0x06DBAB20', 'PA'))
                self.assertEqual(first[-2:], ('—', '0.0'))
                self.assertIn('"ja_excluded_hits": 13', detail.info.get('1.0', 'end'))
                self.assertIn('23.08%', detail.label.cget('text'))
                self.assertEqual(len(detail.sources.get_children()), 2)
                app._clear_notice()
                self.assertEqual([button.cget('text') for button in app.menu_holder.winfo_children()],
                     ['計測', 'DPS', '解析', '設定', '表示', 'ログ'])
                continuous = display_elapsed(app.report)
                app.dps_menu.invoke(1)
                self.assertEqual(app.settings.dps_mode, 'damage')
                self.assertEqual(app.variables['dps_mode'].get(), 'damage')
                self.assertNotEqual(display_elapsed(app.report), continuous)
                self.assertAlmostEqual(app.rows[0]['dps'], 92341 / display_elapsed(app.report))
                self.assertIn(f"{app.rows[0]['dps']:,.1f}", detail.label.cget('text'))
                app.toggle('time_decimals', False)
                self.assertTrue(app.summary.cget('text').startswith(f'Time {int(display_elapsed(app.report))} s'))
                self.assertEqual(backend.requests, [])  # Options never control the capture worker.
                app.dps_menu.invoke(0)
                self.assertEqual(app.variables['dps_mode'].get(), 'quest')
                self.assertEqual(display_elapsed(app.report), continuous)
                app.toggle('players_only', False)
                self.assertGreater(len(app.rows), 1)
                app.toggle('anonymize', True)
                self.assertIn('自分', [row['name'] for row in app.rows])
                app.minimize()
                root.update()
                root.deiconify()
                root.update()
                self.assertTrue(root.overrideredirect())
            finally:
                app.close()


class SettingsTests(unittest.TestCase):
    def test_preferences_survive_restart_and_invalid_values_are_ignored(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'preferences.json'
            settings = Settings(opacity=.75, only_me=True, geometry='710x290+20+30', dps_mode='damage', time_decimals=False)
            settings.save(path)
            loaded = Settings.load(path)
            self.assertEqual((loaded.opacity, loaded.only_me, loaded.geometry), (.75, True, '710x290+20+30'))
            self.assertEqual((loaded.dps_mode, loaded.time_decimals), ('damage', False))
            path.write_text(json.dumps({'schema_version': 1, 'opacity': -1, 'font_size': 90,
                                       'topmost': 'false', 'columns': ['unknown', 'dps', 'dps'],
                                       'dps_mode': 'invalid', 'time_decimals': 'false'}))
            loaded = Settings.load(path)
            self.assertEqual(loaded.opacity, 1)
            self.assertEqual(loaded.font_size, 9)
            self.assertEqual(loaded.columns, ['name', 'dps'])
            self.assertEqual((loaded.dps_mode, loaded.time_decimals), ('quest', True))


@unittest.skipUnless(os.name == 'nt' and HOST.exists(), 'Windows native runtime fixture required')
class GuiControlIntegrationTests(unittest.TestCase):
    def wait(self, backend, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        latest = {}
        while time.monotonic() < deadline:
            try:
                latest = backend.updates.get(timeout=.1)
            except queue.Empty:
                pass
            if predicate(latest):
                return latest
        self.fail(f'backend timed out: {latest}')

    def action(self, backend, action, snapshot):
        backend.submit(action, snapshot=snapshot)
        event = backend.events.get(timeout=22)
        self.assertEqual(event['type'], 'success', event)
        return event

    def test_background_control_archive_reset_and_stale_run_rejection(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work) / 'capture'
            root.mkdir()
            process = subprocess.Popen([str(HOST), str(root)], stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, creationflags=0x08000000)
            monitor = RuntimeMonitor(root, expected_process='AutoRuntimeTestHost')
            backend = Backend(monitor=monitor, controller=RuntimeController('AutoRuntimeTestHost'), interval=.1,
                              log_directory=Path(work) / 'reports')
            backend.start()
            try:
                ready = self.wait(backend, lambda snapshot: snapshot.get('state') == 'binding_ready_waiting_for_start')
                stale = deepcopy(ready)
                stale['run_name'] = 'run-1-1-0'
                with self.assertRaises(ValueError):
                    backend.controller.invoke(stale, 'Start')
                self.assertEqual(json.loads((root / 'autostart-state.json').read_text())['last_command_id'], 0)
                self.action(backend, 'Start', ready)
                active = self.wait(backend, lambda snapshot: snapshot.get('effective_recording'))
                archived = self.action(backend, 'Next', active)
                self.assertTrue(Path(archived['archive']).exists())
                exported = saved_snapshot(archived['archive'])
                self.assertEqual(exported['measurement']['report']['counts']['records'], 0)
                second = self.wait(backend, lambda snapshot: (snapshot.get('measurement') or {}).get('segment_number') == 2)
                self.action(backend, 'Reset', second)
                third = self.wait(backend, lambda snapshot: (snapshot.get('measurement') or {}).get('segment_number') == 3)
                self.assertEqual(len(list((Path(work) / 'reports').glob('*.json'))), 1)
                self.action(backend, 'Stop', third)
                stopped = self.wait(backend, lambda snapshot: (snapshot.get('measurement') or {}).get('complete'))
                self.action(backend, 'Shutdown', stopped)
                process.wait(timeout=5)
                self.assertEqual(process.returncode, 0)
            finally:
                backend.close()
                backend.thread.join(timeout=2)
                if process.poll() is None:
                    process.kill()  # Only the owned synthetic host.
                    process.wait(timeout=5)
                process.stderr.close()


if __name__ == '__main__':
    unittest.main()
