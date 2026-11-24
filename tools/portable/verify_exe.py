"""Isolated one-file smoke test; never selects or writes a game folder."""
import hashlib
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

from ..gui.test_text_export import quest_report
from ..version import VERSION

ROOT = Path(__file__).resolve().parents[2]


def close_verified_window(process, executable, *, title=None):
    """Exercise the regular release entry; close only our isolated exe's GUI."""
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    deadline, visible_since = time.monotonic() + 20, None
    while process.poll() is None and time.monotonic() < deadline:
        matches = []

        @callback_type
        def inspect(handle, _):
            if not user.IsWindowVisible(handle):
                return True
            window_title = ctypes.create_unicode_buffer(256)
            user.GetWindowTextW(handle, window_title, len(window_title))
            if window_title.value != (title or 'LazyParse'):
                return True
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
            owner = kernel.OpenProcess(0x1000, False, pid.value)
            if owner:
                try:
                    path, length = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
                    if (kernel.QueryFullProcessImageNameW(owner, 0, path, ctypes.byref(length)) and
                            Path(path.value).resolve() == executable.resolve()):
                        matches.append(handle)
                finally:
                    kernel.CloseHandle(owner)
            return True

        user.EnumWindows(inspect, 0)
        if len(matches) == 1:
            visible_since = visible_since or time.monotonic()
            if time.monotonic() - visible_since >= 1:
                if not user.PostMessageW(matches[0], 0x0010, 0, 0):  # WM_CLOSE, normal application shutdown.
                    raise ctypes.WinError(ctypes.get_last_error())
                return
        else:
            visible_since = None
        time.sleep(.05)
    raise RuntimeError('The isolated release GUI did not become available')


def main():
    output = Path(__file__).parent / 'results' / ('standalone-' + uuid.uuid4().hex)
    app = output / '最初の場所 with spaces'
    cwd = output / '別の作業フォルダー'
    app.mkdir(parents=True); cwd.mkdir()
    executable = app / 'LazyParse.exe'
    shutil.copyfile(ROOT / 'dist/LazyParse.exe', executable)
    report = output / 'fixture.json'
    report.write_text(json.dumps(quest_report(closed=True), ensure_ascii=False), encoding='utf-8')
    env = dict(os.environ)
    env['PATH'] = os.path.join(os.environ['SystemRoot'], 'System32')
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)

    def launch(folder):
        process = subprocess.Popen([str(folder / 'LazyParse.exe'), '--capture', str(report)],
                cwd=cwd, env=env, creationflags=0x08000000)
        try:
            close_verified_window(process, folder / 'LazyParse.exe')
            assert process.wait(timeout=10) == 0, process.returncode
        except BaseException:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            raise
        config = folder / 'config'
        for name in ('preferences.json', 'quests.csv', 'skills.csv', 'ja_exclusions.csv', 'capture-build.json'):
            assert (config / name).is_file(), name
        manifest = json.loads((config / 'capture-build.json').read_text(encoding='utf-8'))
        assert manifest['application_version'] == VERSION
        assert manifest['application_id'] == 'exe:' + hashlib.sha256((folder/'LazyParse.exe').read_bytes()).hexdigest()
        assert manifest['session'] == str(folder / 'logs/captures')
        assert Path(manifest['core']).is_relative_to(config)
        assert hashlib.sha256(Path(manifest['core']).read_bytes()).hexdigest() == manifest['core_sha256']
        assert not (cwd / 'config').exists()
        return config

    config = launch(app)
    preferences = json.loads((config/'preferences.json').read_text(encoding='utf-8'))
    preferences['font_size'] = 11
    preferences['theme_background'] = '#182438'
    preferences['theme_accent'] = '#e8ac68'
    background = (ROOT / 'tools/gui/assets/icon.png').read_bytes()
    background_name = 'background-' + hashlib.sha256(background).hexdigest()[:16] + '.png'
    (config/background_name).write_bytes(background)
    preferences['background_image'] = background_name
    (config/'preferences.json').write_text(json.dumps(preferences), encoding='utf-8')
    quests = (config/'quests.csv').read_bytes()
    (config/'quests.csv').write_bytes(quests + b'\n# retained edit,,\n')
    app2 = output / '移動先 with spaces'
    app.rename(app2)
    config2 = launch(app2)
    assert json.loads((config2/'preferences.json').read_text(encoding='utf-8'))['font_size'] == 11
    moved_preferences = json.loads((config2/'preferences.json').read_text(encoding='utf-8'))
    assert moved_preferences['theme_background'] == '#182438'
    assert moved_preferences['theme_accent'] == '#e8ac68'
    assert moved_preferences['background_image'] == background_name
    assert (config2/background_name).read_bytes() == background
    assert (config2/'licenses/Pillow/LICENSE').is_file()
    assert (config2/'quests.csv').read_bytes() == quests + b'\n# retained edit,,\n'
    proof = {'exe_sha256': hashlib.sha256((app2/'LazyParse.exe').read_bytes()).hexdigest(),
        'application_version': VERSION, 'exe_identity_matched_after_move':True,
        'exe_size': (app2/'LazyParse.exe').stat().st_size, 'python_and_pwsh_absent_from_path': True,
        'unicode_spaces_and_separate_cwd': True, 'relocation_and_edited_settings_preserved': True,
        'gui_smoke_passed_twice': True, 'normal_release_entry_without_test_flags': True,
        'custom_rgb_and_background_preserved_after_move': True, 'pillow_license_bundled': True,
        'game_folder_touched': False}
    (output / 'verification.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding='utf-8')
    # Keep only the proof; the copied exe and its config would pile up per build.
    for leftover in (app2, cwd, report):
        shutil.rmtree(leftover) if leftover.is_dir() else leftover.unlink()
    print(json.dumps({'proof': str(output/'verification.json'), **proof}, ensure_ascii=False))


if __name__ == '__main__':
    main()
