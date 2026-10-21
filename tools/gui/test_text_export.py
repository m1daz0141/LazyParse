from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import patch

from tools.combat_core.quest_catalog import QuestCatalog, get_quest_catalog
from tools.combat_core.test_combat_core import engine, registry, packet, SELF, ENEMY, ObjectRef
from tools.combat_core.damage_policy import BURN_ATTACK_IDS
from tools.gui.backend import Backend, stored_history
from tools.gui.settings import Settings
from tools.gui.text_export import AutoTextExporter, filename, measurement_date, render_text, save_text
from tools.gui.viewmodel import attack_rows, actor_rows


def report():
    burn = min(BURN_ATTACK_IDS)
    e = engine([(1, 1000, packet(value=100)), (2, 2000, packet(value=300)),
                (3, 3000, packet(value=-50)),
                (4, 4000, packet(source=ObjectRef(0, 0), value=1, attack=burn)),
                (5, 5000, packet(source=ENEMY, target=SELF, value=25))], registry())
    return e.finish(11000, 1000)


def quest_report(closed=False):
    r = report()
    r['quest_measurement'] = {'id': 'fixture#quest-1', 'kind': 'quest', 'number': 1,
        'quest_candidate': 1130, 'world_candidate': 123,
        'end_qpc': 11000 if closed else None, 'end_reason': 'capture_stopped' if closed else None,
        'stream_finalized': closed}
    return r


class QuestCatalogTests(unittest.TestCase):
    def test_editable_csv_unicode_quotes_unknown_and_conflicts(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'Quests.csv'
            path.write_text('quest_id,name,note\n0,ザウーダン討伐,一覧のID0\n1130,"領域,調査",任意のメモ\n# comment,,\n', encoding='utf-8-sig')
            catalog = QuestCatalog.load(path)
            self.assertIsNone(catalog.error)
            self.assertEqual(catalog.label(0), 'ザウーダン討伐')
            self.assertEqual(catalog.label(1130), '領域,調査')
            self.assertIn('999', catalog.label(999))
            path.write_text('quest_id,name\n1,A\n1,B\n', encoding='utf-8')
            catalog = QuestCatalog.load(path)
            self.assertTrue(catalog.error)
            self.assertEqual(catalog.entries, {})
            self.assertTrue(QuestCatalog.load(path.with_name('missing.csv')).error)
        self.assertEqual(get_quest_catalog().label(1130), '領域調査：異世界の残滓')


class TextExportTests(unittest.TestCase):
    def test_totals_minimum_and_healing_burn_exclusion_match_output_without_mutation(self):
        r = report()
        before = deepcopy(r)
        text = render_text(r)
        self.assertIn('TotalDamage : 400', text)
        self.assertIn('TotalDPS : 40.0', text)
        self.assertIn('400 dmg | 25 dmgd', text)
        self.assertIn('2 hits - 100 min, 200 avg, 300 max', text)
        self.assertIn('JA: 0.00%', text)  # Synthetic packets explicitly have zero flags.
        self.assertNotIn('401 dmg', text)
        self.assertEqual(r, before)
        self.assertEqual(r['counts']['records'], 5)
        self.assertEqual(r['damage_totals']['excluded_positive_records'], 1)
        self.assertEqual(measurement_date(r), datetime(2026, 10, 18, 5, 40, 1, tzinfo=timezone.utc))
        self.assertNotIn('\n#123', render_text(r, anonymize=True))
        self.assertIn('自分 |', render_text(r, anonymize=True))
        self.assertIn('TotalDPS : 100.0', render_text(r, dps_mode='damage'))

    def test_old_json_missing_minimum_is_not_invented_even_when_merging_lifetimes(self):
        r = report()
        r.pop('recording_time')
        for s in r['timing']['spans']:
            s.pop('first_damage_filetime')
        for attack in r['attacks']:
            attack.pop('min_hit')
        self.assertIn('— min, 200 avg, 300 max', render_text(r))
        self.assertIn('計測日時不明', render_text(r))
        r['attacks'].append({**r['attacks'][0], 'min_hit': 12})
        self.assertIsNone(attack_rows(r, actor_rows(r)[0])[0]['min_hit'])

    def test_filenames_use_quest_or_manual_jst_and_safe_unknown_name(self):
        r = report()
        self.assertEqual(filename(r), 'LazyParse_Manual_2026-10-18_14-40-01.txt')
        q = quest_report()
        self.assertEqual(filename(q), 'LazyParse_(領域調査：異世界の残滓)_2026-10-18_14-40-01.txt')
        q['quest_measurement']['quest_candidate'] = 999
        self.assertIn('不明クエスト（ID_ 999）', filename(q))
        q['quest_measurement']['quest_candidate'] = 7
        with patch('tools.combat_core.quest_catalog.get_quest_catalog', return_value=QuestCatalog({7: '../a:b?x'})):
            name = filename(q)
            self.assertEqual(Path(name).name, name)
            self.assertNotIn(':', name)
            self.assertNotIn('/', name)

    def test_no_overwrite_collision_suffix_and_utf8_bom_crlf(self):
        with tempfile.TemporaryDirectory() as work:
            first = save_text(report(), directory=work)
            contents = first.read_bytes()
            second = save_text(report(), directory=work)
            self.assertNotEqual(first, second)
            self.assertTrue(second.stem.endswith('_2'))
            self.assertEqual(first.read_bytes(), contents)
            self.assertTrue(contents.startswith(b'\xef\xbb\xbf'))
            self.assertIn(b'\r\n', contents)
            with self.assertRaises(FileExistsError):
                save_text(report(), path=first)
            self.assertFalse(list(Path(work).glob('*.tmp')))

    def test_auto_export_arm_close_late_update_user_edit_and_restart_no_backlog(self):
        with tempfile.TemporaryDirectory() as work:
            exporter = AutoTextExporter(work)
            old = quest_report(True)
            opened = quest_report()
            old['quest_measurement']['id'] = 'old'
            self.assertEqual(exporter.update({'old': (old, True)}, enabled=True), [])
            exporter.update({'new': (opened, False), 'old': (old, True)}, enabled=True)
            final = quest_report(True)
            paths = exporter.update({'new': (final, True), 'old': (old, True)}, enabled=True)
            self.assertEqual(len(paths), 1)
            self.assertEqual(exporter.update({'new': (final, True)}, enabled=True), [])
            self.assertEqual(exporter.update({'new': (final, True)}, enabled=True, dps_mode='damage', anonymize=True), [])
            late = deepcopy(final)
            late['actors'][0]['damage_dealt'] += 100
            self.assertEqual(exporter.update({'new': (late, True)}, enabled=True), paths)
            self.assertIn('TotalDamage : 500', paths[0].read_text(encoding='utf-8-sig'))
            paths[0].write_text('user edit', encoding='utf-8')
            replacement = exporter.update({'new': (final, True)}, enabled=True)
            self.assertEqual(len(replacement), 1)
            self.assertNotEqual(replacement, paths)
            self.assertEqual(paths[0].read_text(), 'user edit')
            restarted = AutoTextExporter(work)
            self.assertEqual(restarted.update({'new': (final, True)}, enabled=True), [])
            self.assertEqual(restarted.update({'new': (final, True)}, enabled=True), [])
            self.assertEqual(len(list(Path(work).glob('*.txt'))), 2)
            exporter.update({}, enabled=False)
            self.assertEqual(exporter.update({'new': (final, True)}, enabled=True), [])

    def test_backend_manual_and_quest_completion_exports_without_capture_control(self):
        class Monitor:
            snapshot = {}
            def poll(self):
                return deepcopy(self.snapshot)
        for auto in (True, False):
            with self.subTest(auto=auto), tempfile.TemporaryDirectory() as work:
                monitor = Monitor()
                b = Backend(monitor=monitor, log_directory=None)
                b.auto_quests = auto
                b.text_exporter = AutoTextExporter(work)
                b.text_options = (True, 'quest', False, True)
                opened = quest_report() if auto else report()
                monitor.snapshot = {'measurement': {'capture_path': 'test.opcap', 'report': opened, 'complete': False},
                    'quest_view': {'current': opened, 'history': []}, 'history': []}
                b._poll()
                final = quest_report(True) if auto else report()
                monitor.snapshot['measurement'] = {'capture_path': 'test.opcap', 'report': final, 'complete': True}
                monitor.snapshot['quest_view'] = {'current': final, 'history': [final]}
                b._poll()
                files = list(Path(work).glob('*.txt'))
                self.assertEqual(len(files), 1)
                self.assertIn('(領域調査：異世界の残滓)' if auto else 'Manual', files[0].name)
                b._poll()
                self.assertEqual(len(list(Path(work).glob('*.txt'))), 1)
                self.assertTrue(b.requests.empty())

    def test_backend_failure_is_retried_and_saved_export_does_not_poll_or_control_game(self):
        class Monitor:
            def poll(self):
                raise AssertionError('saved TXT export must not poll or control a game')
        b = Backend(monitor=Monitor())
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'saved.txt'
            b._execute('ExportText', {'measurement': {'report': report()}}, path)
            self.assertTrue(path.exists())
            self.assertEqual(b.events.get()['type'], 'success')
            with self.assertRaises(FileExistsError):
                b._execute('ExportText', {'measurement': {'report': report()}}, path)
            exporter = AutoTextExporter(work)
            exporter.update({'q': (quest_report(), False)}, enabled=True)
            with patch('tools.gui.text_export._publish', side_effect=PermissionError('locked')):
                with self.assertRaises(PermissionError):
                    exporter.update({'q': (quest_report(True), True)}, enabled=True)
            self.assertEqual(len(exporter.update({'q': (quest_report(True), True)}, enabled=True)), 1)

    def test_persistent_history_keeps_latest_revision_and_manual_reports_skips_invalid(self):
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work)
            old = quest_report(True)
            old['quest_measurement']['stream_finalized'] = False
            for name, r in [('old', old), ('final', quest_report(True)), ('manual', report())]:
                (directory / (name + '.json')).write_text(json.dumps(r), encoding='utf-8')
            (directory / 'broken.json').write_text('[]')
            entries = stored_history(directory)
            self.assertEqual(len(entries), 2)
            automatic = next(e for e in entries if e['segment_number'] == '自動')
            self.assertTrue(automatic['capture_path'].endswith('final.json'))
            self.assertIn('領域調査：異世界の残滓', automatic['run_name'])

    def test_preferences_persist_automatic_export_but_reject_string_boolean(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'prefs.json'
            Settings(auto_text_export=True).save(path)
            self.assertTrue(Settings.load(path).auto_text_export)
            path.write_text('{"schema_version":1,"auto_text_export":"true"}')
            self.assertFalse(Settings.load(path).auto_text_export)

    def test_history_prefers_corrected_rule_even_if_old_ordinal_id_and_counts_differ(self):
        with tempfile.TemporaryDirectory() as work:
            old, new = quest_report(True), quest_report(True)
            common = {'capture_id': 'fixture.opcap', 'start_qpc': 100}
            old['quest_measurement'].update(common, rule_version=4, id='fixture#quest-6')
            new['quest_measurement'].update(common, rule_version=5, id='fixture#quest-7')
            old['counts']['records'] += 1  # A post-clear hit incorrectly inside the old measurement.
            for name, r in [('old', old), ('new', new)]:
                (Path(work) / (name + '.json')).write_text(json.dumps(r), encoding='utf-8')
            history = stored_history(work)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]['report']['quest_measurement']['rule_version'], 5)


class TextExportWindowTests(unittest.TestCase):
    def test_current_and_history_context_menus_export_the_selected_measurement(self):
        import tkinter as tk
        from tkinter import ttk
        from tools.gui.app import Application
        class Stub:
            interval = .5
            log_directory = None
            def __init__(self):
                self.requests = []
                self.updates, self.events = queue.Queue(), queue.Queue()
            def submit(self, *args, **kwargs):
                self.requests.append((args, kwargs))
            def close(self):
                pass
        with tempfile.TemporaryDirectory() as work:
            root = tk.Tk()
            b = Stub()
            app = Application(root, b, settings_path=Path(work) / 'preferences.json', start_backend=False)
            try:
                app.saved = {'state': 'saved_log', 'measurement': {'report': report()}}
                app.snapshot = app.saved
                root.update()
                app.render()
                with patch('tools.gui.app.EXPORT_DIRECTORY', Path(work)), patch('tkinter.Menu.tk_popup'), patch(
                        'tools.gui.app.filedialog.asksaveasfilename', return_value=str(Path(work) / 'selected.txt')):
                    app.table.event_generate('<Button-3>', x=10, y=5)
                    root.update()
                    menus = [w for w in root.winfo_children() if isinstance(w, tk.Menu)]
                    menu = menus[-1]
                    menu.invoke(menu.index('end'))
                    self.assertEqual(b.requests[-1][0], ('ExportText',))
                    self.assertNotIn('quest_measurement', b.requests[-1][1]['snapshot']['measurement']['report'])
                    q = quest_report(True)
                    app.latest = {'quest_history': [{'report': q, 'label': '領域調査：異世界の残滓 #1', 'complete': True}]}
                    app.open_history()
                    root.update()
                    history = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel) and w.title() == '記録の履歴')
                    tree = next(w for w in history.winfo_children() if isinstance(w, ttk.Treeview))
                    box = tree.bbox(tree.get_children()[0])
                    tree.event_generate('<Button-3>', x=10, y=box[1] + 3)
                    root.update()
                    menu = next(w for w in history.winfo_children() if isinstance(w, tk.Menu))
                    menu.invoke(menu.index('end'))
                    selected = b.requests[-1][1]['snapshot']['measurement']['report']
                    self.assertEqual(selected['quest_measurement']['quest_candidate'], 1130)
                    app.toggle('auto_text_export', True)
                    self.assertTrue(b.text_options[0])
                    app.set_dps_mode('damage')
                    self.assertEqual(b.text_options[1], 'damage')
                    app.toggle('anonymize', True)
                    self.assertTrue(b.text_options[2])
            finally:
                app.close()


if __name__ == '__main__':
    unittest.main()
