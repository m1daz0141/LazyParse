from copy import deepcopy
from pathlib import Path
import queue
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from .app import Application
from .settings import Settings
from .test_text_export import quest_report


class BackendStub:
    interval = .5

    def __init__(self):
        self.updates, self.events = queue.Queue(), queue.Queue()
        self.requests = []

    def submit(self, *args, **kwargs):
        self.requests.append((args, kwargs))

    def close(self):
        pass


class MenuUpdateTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.path = Path(self.work.name) / 'preferences.json'
        self.root = tk.Tk()
        self.backend = BackendStub()
        self.app = Application(self.root, self.backend, settings_path=self.path, start_backend=False)
        self.root.update()
        self.root.after_cancel(self.app.consume_timer)

    def tearDown(self):
        self.app.close()
        self.work.cleanup()

    @staticmethod
    def entries(menu):
        return [None if menu.type(i) == 'separator' else menu.entrycget(i, 'label')
                for i in range(menu.index('end') + 1)]

    def consume(self, now, snapshot=None):
        if snapshot is not None:
            self.backend.updates.put(snapshot)
        with patch('tools.gui.app.time.monotonic', return_value=now):
            self.app._consume()
        self.root.after_cancel(self.app.consume_timer)

    def snapshot(self, damage=400, *, finished=False, quest_id='fixture#quest-1'):
        report = quest_report(closed=finished)
        report['quest_measurement']['id'] = quest_id
        for actor in report['actors']:
            if actor['role'] == 'self':
                actor['damage_dealt'] = damage
        return {'state': 'paused' if finished else 'recording', 'effective_recording': not finished,
                'measurement': {'capture_path': 'fixture.opcap', 'quest_id': quest_id, 'report': report}}

    def test_menu_locations_labels_radio_switching_and_defaults(self):
        app = self.app
        self.assertEqual([button.cget('text') for button in app.menu_holder.winfo_children()],
                         ['計測', '解析', '設定', '表示', 'ログ'])
        options = ['クエスト毎に自動計測', 'Timeを小数点まで表示', '計測終了後にTXTを自動出力']
        self.assertEqual(self.entries(app.setup_menu)[-4:], [None, *options])
        self.assertFalse(set(options) & set(self.entries(app.logging_menu)))
        self.assertEqual(self.entries(app.parsing_menu)[:3],
                         ['DPS：クエスト全体表示', 'DPS：総ダメージ基準表示', None])
        self.assertFalse(app.variables['time_decimals'].get())
        self.assertTrue(app.variables['auto_text_export'].get())
        app.parsing_menu.invoke('DPS：総ダメージ基準表示')
        self.assertEqual(app.settings.dps_mode, 'damage')
        app.parsing_menu.invoke('DPS：クエスト全体表示')
        self.assertEqual(app.settings.dps_mode, 'quest')
        app.setup_menu.invoke('計測終了後にTXTを自動出力')
        self.assertFalse(self.backend.text_options[0])
        self.assertEqual(self.backend.requests, [])

    def test_whole_second_display_batches_new_damage_but_keeps_final_results_immediate(self):
        original = self.snapshot()
        before = deepcopy(original)
        self.consume(10, original)
        self.assertEqual(self.app.rows[0]['damage_dealt'], 400)
        self.consume(10.5, self.snapshot(600))
        self.assertEqual(self.app.rows[0]['damage_dealt'], 400)
        self.app.render()  # Resizing or selection must not advance the numeric display.
        self.assertEqual(self.app.rows[0]['damage_dealt'], 400)
        self.consume(10.9, self.snapshot(900))
        self.assertEqual(self.app.rows[0]['damage_dealt'], 400)
        self.consume(11)
        self.assertEqual(self.app.rows[0]['damage_dealt'], 900)
        self.consume(11.1, self.snapshot(1000, finished=True))
        self.assertEqual(self.app.rows[0]['damage_dealt'], 1000)
        self.assertEqual(original, before)

    def test_decimal_switch_and_new_quest_and_error_are_not_delayed(self):
        self.consume(10, self.snapshot())
        self.consume(10.1, self.snapshot(600))
        self.app.setup_menu.invoke('Timeを小数点まで表示')
        self.consume(10.2)
        self.assertEqual(self.app.rows[0]['damage_dealt'], 600)
        self.consume(10.3, self.snapshot(700))
        self.assertEqual(self.app.rows[0]['damage_dealt'], 700)
        self.app.setup_menu.invoke('Timeを小数点まで表示')
        self.consume(10.4, self.snapshot(100, quest_id='fixture#quest-2'))
        self.assertEqual(self.app.rows[0]['damage_dealt'], 100)
        failed = self.snapshot(150, quest_id='fixture#quest-2')
        failed.update(state='read_error', effective_recording=False)
        self.consume(10.5, failed)
        self.assertEqual(self.app.rows[0]['damage_dealt'], 150)

    def test_detail_first_tab_is_visible_and_populated_without_switching(self):
        self.consume(10, self.snapshot())
        app = self.app
        app.selected = app.rows[0]['key']
        app._open_selected()
        self.root.update()
        detail = app.details[app.selected]
        self.assertEqual(detail.notebook.select(), str(detail.attacks.master))
        self.assertTrue(detail.attacks.winfo_ismapped())
        rows = detail.attacks.get_children()
        self.assertTrue(rows)
        self.assertTrue(detail.attacks.bbox(rows[0]))
        self.assertTrue(detail.attacks.item(rows[0], 'values')[0])
        self.assertIn('400', detail.label.cget('text'))

    def test_saved_choices_override_new_defaults(self):
        self.assertFalse(Settings.load(self.path).time_decimals)
        self.assertTrue(Settings.load(self.path).auto_text_export)
        Settings(time_decimals=True, auto_text_export=False).save(self.path)
        saved = Settings.load(self.path)
        self.assertTrue(saved.time_decimals)
        self.assertFalse(saved.auto_text_export)


if __name__ == '__main__':
    unittest.main()
