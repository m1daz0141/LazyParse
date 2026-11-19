from copy import deepcopy
import json
from pathlib import Path
import queue
import shutil
import tempfile
import unittest
from unittest.mock import patch

from .appearance import (Background, import_background, remove_background, palette,
                         rgb_color, status_band_color, DEFAULT_BACKGROUND, DEFAULT_ACCENT)
from .settings import Settings
from .viewmodel import status_view
from .test_text_export import quest_report


class AppearanceTests(unittest.TestCase):
    def test_settings_validate_rgb_and_keep_only_portable_owned_image_names(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'preferences.json'
            name = 'background-0123456789abcdef.png'
            Settings(theme_background='#123456', theme_accent='#abcdef', background_image=name).save(path)
            loaded = Settings.load(path)
            self.assertEqual((loaded.theme_background, loaded.theme_accent, loaded.background_image),
                             ('#123456', '#abcdef', name))
            path.write_text(json.dumps({'schema_version': 1, 'theme_background': 'red',
                'theme_accent': '#12345g', 'background_image': '../outside.png'}))
            self.assertEqual(Settings.load(path), Settings())
            self.assertEqual(rgb_color(['0', '128', '255']), '#0080ff')
            for channels in (['-1', '0', '0'], ['256', '0', '0'], ['1.5', '0', '0'], ['True', '0', '0']):
                with self.assertRaises(ValueError):
                    rgb_color(channels)
            self.assertEqual(palette()['#0a0a0a'], DEFAULT_BACKGROUND)
            self.assertEqual(palette('#ffffff', '#ff0000')['#e4e8eb'], '#161a1d')

    def test_import_orientation_metadata_removal_relocation_and_owned_cleanup(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            source = root / '元画像.jpg'
            exif = Image.Exif()
            exif[274], exif[315] = 6, 'private author'
            Image.new('RGB', (20, 40), '#c06020').save(source, exif=exif)
            original = source.read_bytes()
            directory = root / 'config'
            name = import_background(source, directory)
            self.assertEqual(source.read_bytes(), original)
            with Image.open(directory / name) as copied:
                self.assertEqual(copied.size, (40, 20))
                self.assertFalse(copied.getexif())
            source.unlink()
            moved = root / '移動先'
            directory.rename(moved)
            background = Background()
            background.load(name, moved)
            self.assertEqual(background.source.size, (40, 20))
            unrelated = moved / 'user.png'
            unrelated.write_bytes(b'keep')
            remove_background('user.png', moved)
            self.assertTrue(unrelated.exists())
            remove_background(name, moved)
            self.assertFalse((moved / name).exists())
            bad = root / 'bad.jpg'
            bad.write_bytes(b'not an image')
            with self.assertRaises(OSError):
                import_background(bad, moved)

    def test_normal_state_labels_are_unified_without_hiding_errors(self):
        for reason in ('clear_candidate', 'clear_candidate_with_result', 'failure_candidate',
                       'failure_return_candidate', 'new_world', 'capture_stopped', None):
            snapshot = {'effective_recording': True, 'measurement': {'report': {
                'quest_measurement': {'end_reason': reason}}}}
            self.assertEqual(status_view(snapshot)[0], '待機中' if reason else '計測中')
            self.assertEqual(status_view(snapshot, saved=True)[0], '保存ログ')
        for state in ('paused', 'binding_ready_waiting_for_start', 'shutdown', None):
            self.assertEqual(status_view({'state': state})[0], '待機中')
        self.assertEqual(status_view({'effective_recording': True})[0], '計測中')
        self.assertEqual(status_view({'state': 'writer_error'})[0], '取得エラー')
        self.assertEqual(status_view({'game_version_error': 'mismatch'})[0], 'ゲームDLLの確認が必要')

    def test_footer_band_uses_custom_accent_only_for_normal_states(self):
        for snapshot in ({'effective_recording': True}, {'state': 'paused'},
                         {'state': 'binding_ready_waiting_for_start'}):
            label, color = status_view(snapshot)
            self.assertEqual(status_band_color(label, color, DEFAULT_ACCENT), color)
            self.assertEqual(status_band_color(label, color, '#FF0080'), '#ff0080')
        self.assertEqual(status_band_color(*status_view({'effective_recording': True}), DEFAULT_ACCENT), '#69c9b0')
        for snapshot in ({'state': 'writer_error'}, {'game_version_error': 'mismatch'}):
            label, color = status_view(snapshot)
            self.assertEqual(status_band_color(label, color, '#ff0080'), color)
        label, color = status_view({}, saved=True)
        self.assertEqual(status_band_color(label, color, '#ff0080'), color)

    def test_column_widths_are_saved_and_invalid_values_ignored(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'preferences.json'
            Settings(column_widths={'name': 150.5, 'dps': 40}).save(path)
            self.assertEqual(Settings.load(path).column_widths, {'name': 150.5, 'dps': 40.0})
            path.write_text(json.dumps({'schema_version': 1, 'column_widths':
                {'name': 0, 'dps': 'wide', 'unknown': 50, 'damage_dealt': 90, 'ja': True}}))
            self.assertEqual(Settings.load(path).column_widths, {'damage_dealt': 90.0})


class AppearanceWindowTests(unittest.TestCase):
    def tearDown(self):
        import gc
        gc.collect()  # Dispose closed test windows on their owning UI thread.

    def test_rgb_dialog_image_scroll_reset_restart_and_failure_preserve_capture(self):
        import tkinter as tk
        from PIL import Image
        from .app import Application, BG, TEXT

        class Stub:
            interval = .5
            def __init__(self):
                self.updates, self.events, self.requests = queue.Queue(), queue.Queue(), []
            def submit(self, *args, **kwargs):
                self.requests.append((args, kwargs))
            def close(self):
                pass

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        with tempfile.TemporaryDirectory() as work:
            directory = Path(work) / 'app/config'
            settings_path = directory / 'preferences.json'
            root, backend = tk.Tk(), Stub()
            app = Application(root, backend, settings_path=settings_path, start_backend=False)
            try:
                report = quest_report()
                app.snapshot = {'effective_recording': True, 'measurement': {'report': report}}
                root.update()
                app.render()
                self.assertEqual(app.status.cget('text'), '● 計測中')
                before = deepcopy(app.report)
                window = app.choose_theme()
                entries = [w for w in descendants(window) if isinstance(w, tk.Spinbox)]
                buttons = {w.cget('text'): w for w in descendants(window) if isinstance(w, tk.Button)}
                def set_values(values):
                    for entry, value in zip(entries, values):
                        entry.delete(0, 'end')
                        entry.insert(0, str(value))
                set_values([-1, 0, 0, 1, 2, 3])
                with patch('tools.gui.app.messagebox.showerror') as error:
                    buttons['適用'].invoke()
                    error.assert_called_once()
                self.assertEqual(app.settings.theme_background, DEFAULT_BACKGROUND)
                # All colors may coincide: subsequent changes still use the
                # original widget roles rather than reverse-mapping RGB values.
                set_values([0]*6)
                buttons['適用'].invoke()
                self.assertTrue(app._commit_appearance('#ffffff', '#884400', ''))
                self.assertEqual(app.bar.cget('background'), '#ffffff')
                self.assertEqual(app.summary.cget('foreground'), '#161a1d')
                image = Path(work) / 'original.jpg'
                Image.new('RGB', (300, 150), '#205090').save(image)
                with patch('tools.gui.app.filedialog.askopenfilename', return_value=str(image)):
                    app.choose_background()
                image.unlink()
                root.update()
                app.render()
                self.assertTrue(app.table.find_withtag('background'))
                self.assertEqual(app.background.photo.width(), app.table.winfo_width())
                photo = app.background.photo
                app.render()
                self.assertIs(app.background.photo, photo)  # Polling does not resize/decode again.
                app.table.configure(scrollregion=(0, 0, 600, 1200))
                app.table.yview_moveto(.5)
                root.update()
                self.assertEqual(app.table.coords('background')[1], app.table.canvasy(0))
                name = app.settings.background_image
                app.selected = app.rows[0]['key']
                app._open_selected()
                detail = app.details[app.selected]
                self.assertEqual(detail.label.cget('background'), '#ffffff')
                self.assertEqual(app.report, before)
                self.assertFalse(backend.requests)
                with (patch.object(Settings, 'save', side_effect=PermissionError('denied')),
                      patch('tools.gui.app.messagebox.showerror')):
                    self.assertFalse(app._commit_appearance('#123456', '#fedcba', ''))
                self.assertEqual(app.settings.background_image, name)
                self.assertTrue((directory / name).exists())
            finally:
                app.close()
            moved = Path(work) / 'moved/config'
            moved.parent.mkdir()
            shutil.copytree(directory, moved)
            root, backend = tk.Tk(), Stub()
            app = Application(root, backend, settings_path=moved/'preferences.json', start_backend=False)
            try:
                root.update()
                app.render()
                self.assertEqual(app.settings.theme_background, '#ffffff')
                self.assertTrue(app.table.find_withtag('background'))
                app.reset_background()
                self.assertFalse(app.table.find_withtag('background'))
                self.assertEqual(app.settings.theme_background, '#ffffff')
                app.reset_appearance()
                self.assertEqual(app.bar.cget('background'), BG)
                self.assertEqual(app.summary.cget('foreground'), TEXT)
                self.assertEqual(Settings.load(moved/'preferences.json').background_image, '')
                self.assertFalse((moved / name).exists())
                self.assertFalse(backend.requests)
            finally:
                app.close()

    def test_footer_band_follows_accent_and_header_borders_resize_columns(self):
        import tkinter as tk
        from .app import Application, COLUMN_MIN_WIDTH

        class Stub:
            interval = .5
            def __init__(self):
                self.updates, self.events, self.requests = queue.Queue(), queue.Queue(), []
            def submit(self, *args, **kwargs):
                self.requests.append((args, kwargs))
            def close(self):
                pass

        class Event:
            def __init__(self, x):
                self.x, self.y, self.x_root, self.y_root = x, 5, x, 5

        with tempfile.TemporaryDirectory() as work:
            settings_path = Path(work) / 'config/preferences.json'
            root, backend = tk.Tk(), Stub()
            app = Application(root, backend, settings_path=settings_path, start_backend=False)
            try:
                app.snapshot = {'effective_recording': True, 'measurement': {'report': quest_report()}}
                root.update()
                app.render()
                self.assertEqual(app.indicator.cget('background'), '#69c9b0')
                self.assertTrue(app._commit_appearance(DEFAULT_BACKGROUND, '#aa3366', ''))
                self.assertEqual(app.indicator.cget('background'), '#aa3366')
                app.reset_appearance()
                self.assertEqual(app.indicator.cget('background'), '#69c9b0')

                before = app._layout()
                (name, _, start, border), (right, _, _, right_end) = before[0], before[1]
                sort = (app.sort_key, app.descending)
                app._header_press(Event(round(border)))
                self.assertEqual((app.sort_key, app.descending), sort)  # A border press does not sort.
                app._header_drag(Event(right_end + 100))
                self.assertAlmostEqual(app._layout()[0][3], right_end - COLUMN_MIN_WIDTH, delta=1)
                app._header_drag(Event(round(border) + 10))
                app._header_release(Event(round(border) + 10))
                after = app._layout()
                self.assertAlmostEqual(after[0][3], round(border) + 10, delta=1)
                self.assertAlmostEqual(after[1][3], right_end, delta=1)  # Other columns keep their place.
                for moved, original in zip(after[2:], before[2:]):
                    self.assertAlmostEqual(moved[2], original[2], places=6)
                    self.assertAlmostEqual(moved[3], original[3], places=6)
                app._header_press(Event(round(after[0][3])))
                app._header_drag(Event(-500))
                self.assertAlmostEqual(app._layout()[0][3], COLUMN_MIN_WIDTH, delta=1)
                app._header_release(Event(0))
                app._header_press(Event(round((after[1][2] + after[1][3]) / 2)))
                self.assertEqual(app.sort_key, right)  # Inside a column, a click still sorts.
                widths = dict(app.settings.column_widths)
                self.assertEqual(set(widths), {name, right})
                self.assertFalse(backend.requests)
            finally:
                app.close()
            self.assertEqual(Settings.load(settings_path).column_widths, widths)
            root, backend = tk.Tk(), Stub()
            app = Application(root, backend, settings_path=settings_path, start_backend=False)
            try:
                root.geometry('670x275+200+150')
                root.update()
                self.assertEqual(app.settings.column_widths, widths)
                # A long footer never hides the grip, and every border/corner resizes.
                app.notify('長い通知 ' * 60)
                app.render()
                root.update()
                self.assertGreater(app.grip.winfo_width(), 0)
                self.assertLessEqual(app.grip.winfo_rootx() + app.grip.winfo_width(), root.winfo_rootx() + root.winfo_width())

                class At:
                    def __init__(self, x, y):
                        self.x_root, self.y_root = root.winfo_rootx() + x, root.winfo_rooty() + y
                        self.x = self.y = 0
                with patch.object(app, 'show_diagnostics') as diagnostics:
                    footer_y = root.winfo_height() - 2
                    self.assertEqual(app._edge_press(At(300, footer_y)), 'break')
                    self.assertEqual(app._edge_drag(At(300, footer_y + 40)), 'break')
                    app._edge_release(At(300, footer_y + 40))
                    root.update()
                    self.assertEqual(root.winfo_height(), 315)
                    app.summary.event_generate('<Button-1>', x=20, y=5)
                    diagnostics.assert_called_once()  # Away from the border the footer click still works.
                app._edge_press(At(1, 1))
                app._edge_drag(At(-49, -29))
                app._edge_release(At(-49, -29))
                root.update()
                self.assertEqual((root.winfo_width(), root.winfo_height(), root.winfo_rootx(), root.winfo_rooty()),
                                 (720, 345, 150, 120))
                app._edge_press(At(root.winfo_width() - 1, 100))
                app._edge_drag(At(-2000, 100))
                app._edge_release(At(-2000, 100))
                root.update()
                self.assertEqual((root.winfo_width(), root.winfo_rootx()), (500, 150))  # Minimum width.
                self.assertIsNone(app._edge_press(At(250, 100)))  # Inside: normal widget handling.
                self.assertEqual(app._edges(At(0, root.winfo_height() - 1)), 'sw')
                app.reset_column_widths()
                self.assertEqual(app.settings.column_widths, {})
            finally:
                app.close()


if __name__ == '__main__':
    unittest.main()
