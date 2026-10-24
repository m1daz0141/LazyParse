from copy import deepcopy
import json
from pathlib import Path
import queue
import shutil
import tempfile
import unittest
from unittest.mock import patch

from .appearance import (Background, import_background, remove_background, palette,
                         rgb_color, DEFAULT_BACKGROUND, DEFAULT_ACCENT)
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


if __name__ == '__main__':
    unittest.main()
