"""Install only the added loader; existing game DLLs are read and verified."""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from ..paths import HOME, BUNDLE
from .files import atomic_json, exclusive_file
from ..version import VERSION

RESOURCES = BUNDLE / 'tools/portable/bin'
PATH_MARKER = 'LAZYPARSE_CORE_PATH_V1'
PATH_BYTES = 1024 * 2


class GameVersionMismatch(ValueError):
    """A valid game directory contains binaries this release cannot capture."""


def application_identity():
    # Fingerprint the actual executable as well as recording a friendly version.
    # GUI-only releases and rebuilds therefore also require folder selection.
    if getattr(sys, 'frozen', False):
        with open(sys.executable, 'rb') as stream:
            return 'exe:' + hashlib.file_digest(stream, 'sha256').hexdigest()
    return 'source:' + VERSION


def digest(data):
    return hashlib.sha256(data).hexdigest()


def patched_loader(template, core):
    marker = PATH_MARKER.encode('utf-16le')
    if template.count(marker) != 1:
        raise ValueError('ローダーのパス設定領域が不正です。')
    offset = template.index(marker)
    if template[offset:offset+PATH_BYTES] != marker.ljust(PATH_BYTES, b'\0'):
        raise ValueError('ローダーの設定領域が変更されています。')
    value = (str(core.resolve()) + '\0').encode('utf-16le')
    if len(value) > PATH_BYTES:
        raise ValueError('保存先のパスが長すぎます。exeを浅いフォルダーへ移動してください。')
    return template[:offset] + value.ljust(PATH_BYTES, b'\0') + template[offset+PATH_BYTES:]


def game_running():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    class Entry(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD), ('pid', wintypes.DWORD),
            ('heap', ctypes.c_size_t), ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
            ('parent', wintypes.DWORD), ('priority', wintypes.LONG), ('flags', wintypes.DWORD),
            ('name', wintypes.WCHAR * 260)]
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = Entry(); entry.size = ctypes.sizeof(Entry)
        valid = kernel.Process32FirstW(handle, ctypes.byref(entry))
        if not valid:
            raise ctypes.WinError(ctypes.get_last_error())
        while valid:
            if entry.name.casefold() == 'pso2.exe':
                return True
            valid = kernel.Process32NextW(handle, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(ctypes.get_last_error())
        return False
    finally:
        kernel.CloseHandle(handle)


class Deployment:
    def __init__(self, home=HOME, resources=RESOURCES, running=game_running, *, app_identity=None):
        self.home, self.resources, self.running = Path(home).resolve(), Path(resources), running
        self.config = self.home / 'config'
        self.record = self.config / 'native-install.json'
        self.manifest = self.config / 'capture-build.json'
        self.app_identity = app_identity or application_identity()
        self._game_cache = {}

    def needs_reselection(self):
        try:
            record = json.loads(self.record.read_text(encoding='utf-8'))
            return record.get('application_id') != self.app_identity
        except (OSError, ValueError, AttributeError):
            return True

    def prepare(self):
        build = json.loads((self.resources / 'build.json').read_text(encoding='utf-8-sig'))
        core, template = ((self.resources / name).read_bytes() for name in ('Capture.dll', 'Loader.dll'))
        if digest(core) != build['core_sha256'] or digest(template) != build['loader_sha256']:
            raise ValueError('同梱DLLのハッシュが一致しません。再ビルドしてください。')
        directory = self.config / 'native' / build['core_sha256'][:16]
        directory.mkdir(parents=True, exist_ok=True)
        core_path = directory / 'Capture.dll'
        if core_path.exists() and digest(core_path.read_bytes()) != build['core_sha256']:
            raise ValueError('展開済みの取得DLLが変更されています。')
        if not core_path.exists():
            with core_path.open('xb') as stream:
                stream.write(core)
        loader = patched_loader(template, core_path)
        loader_path = directory / 'Loader.dll'
        if not loader_path.exists() or loader_path.read_bytes() != loader:
            temporary = loader_path.with_suffix('.tmp')
            temporary.write_bytes(loader)
            os.replace(temporary, loader_path)
        session = self.home / 'logs/captures'
        session.mkdir(parents=True, exist_ok=True)
        value = {**build, 'loader': str(loader_path), 'loader_sha256': digest(loader),
            'core': str(core_path), 'session': str(session), 'control_abi': 1,
            'application_id': self.app_identity, 'application_version': VERSION,
            'requires_start_signal': True}
        atomic_json(self.manifest, value)
        self.build = value
        return value

    def verify_game(self, folder, *, force=True):
        if str(folder).startswith(('\\\\', '//')) or '://' in str(folder):
            raise ValueError('ローカルのPSO2フォルダーを選択してください。')
        folder = Path(folder).resolve(strict=True)
        if folder.name.casefold() != 'sub' or folder.parent.name.casefold() != 'pso2_bin':
            raise ValueError('pso2_bin の中の sub フォルダーを選択してください。')
        names = [('pso2bridge.dll', 'bridge_sha256'), ('pso2classic.dll', 'classic_sha256')]
        paths = [folder / name for name, _ in names]
        def signature():
            result = []
            for path in paths:
                if not path.is_file():
                    raise ValueError(f'{path.name} がありません。pso2_bin/subを確認してください。')
                stat = path.stat()
                result.append((stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino))
            return tuple(result)
        before = signature()
        cached = self._game_cache.get(folder)
        if not force and cached and cached[0] == before:
            if cached[1]:
                raise GameVersionMismatch(cached[1])
            return folder
        mismatches = []
        for path, (_, key) in zip(paths, names):
            with path.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != self.build[key]:
                mismatches.append(f'{path.name}\n対応SHA-256: {self.build[key]}\n検出SHA-256: {actual}')
        if signature() != before:
            raise ValueError('ゲームファイルの更新中です。更新完了後にもう一度確認してください。')
        message = ('ゲームDLLの変更を検出しました。このLazyParseでは未対応のゲーム版です。\n'
                   '配置・記録開始を中止しました。対応するLazyParseへ更新してください。\n\n' +
                   '\n\n'.join(mismatches)) if mismatches else None
        self._game_cache[folder] = (before, message)
        if message:
            raise GameVersionMismatch(message)
        return folder

    def _owned(self, target, data):
        sha = digest(data)
        if sha == self.build['loader_sha256'] or sha in self.build.get('legacy_loader_sha256', []):
            return True
        # Moving just the exe creates fresh config. Recognize the exact same
        # loader code with only its reserved path field changed, never a DLL
        # that merely uses the same filename or exports.
        template = (self.resources / 'Loader.dll').read_bytes()
        offset = template.index(PATH_MARKER.encode('utf-16le'))
        if (len(data) == len(template) and data[:offset] == template[:offset] and
                data[offset+PATH_BYTES:] == template[offset+PATH_BYTES:]):
            return True
        try:
            record = json.loads(self.record.read_text(encoding='utf-8'))
            return Path(record['destination']).resolve() == target and record['sha256'] == sha
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def check(self, folder):
        folder = self.verify_game(folder)
        target = folder / 'eos_gaming.dll'
        if not target.is_file():
            raise ValueError('eos_gaming.dll がありません。ゲームフォルダーを再選択して配置してください。')
        if digest(target.read_bytes()) != self.build['loader_sha256']:
            raise ValueError('ローダーの更新または保存先の変更が必要です。ゲーム終了後、フォルダーを再選択してください。')
        return folder

    def install(self, folder):
        if self.running():
            raise ValueError('PSO2を通常終了してからDLLを配置してください。')
        with exclusive_file(self.config / 'deployment.lock'):
            folder = self.verify_game(folder)
            target = folder / 'eos_gaming.dll'
            if target.is_symlink():
                raise ValueError('配置先がリンクです。自動で置換できません。')
            old = target.read_bytes() if target.exists() else None
            if old is not None and not self._owned(target, old):
                raise ValueError('別の eos_gaming.dll が存在します。既存ファイルは上書きしません。')
            loader = Path(self.build['loader']).read_bytes()
            if digest(loader) != self.build['loader_sha256']:
                raise ValueError('展開したローダーが変更されています。')
            temporary = target.with_name('LazyParse-' + uuid.uuid4().hex + '.tmp')
            try:
                temporary.write_bytes(loader)
                if self.running():
                    raise ValueError('配置中にPSO2が起動しました。終了後に再度操作してください。')
                if old is None:
                    os.link(temporary, target)  # Never replace a newly appeared file.
                else:
                    if not target.exists() or target.read_bytes() != old:
                        raise ValueError('配置先の内容が操作中に変わりました。')
                    os.replace(temporary, target)
                try:
                    atomic_json(self.record, {'schema_version': 1, 'destination': str(target),
                        'sha256': self.build['loader_sha256'], 'session': self.build['session'],
                        'application_id': self.app_identity, 'application_version': VERSION})
                except OSError:
                    if target.read_bytes() == loader:
                        if old is None:
                            target.unlink()
                        else:
                            temporary.write_bytes(old)
                            os.replace(temporary, target)
                    raise
            finally:
                temporary.unlink(missing_ok=True)
            if target.read_bytes() != loader:
                raise ValueError('配置後のハッシュが一致しません。')
            return str(folder)

    def remove(self):
        if self.running():
            raise ValueError('PSO2を通常終了してからDLLを撤去してください。')
        with exclusive_file(self.config / 'deployment.lock'):
            record = json.loads(self.record.read_text(encoding='utf-8'))
            original = Path(record['destination'])
            target = original.resolve()
            if (target.name.casefold() != 'eos_gaming.dll' or original.is_symlink() or
                    target.parent.name.casefold() != 'sub' or target.parent.parent.name.casefold() != 'pso2_bin'):
                raise ValueError('配置台帳のパスが不正です。')
            if target.exists():
                if digest(target.read_bytes()) != record['sha256']:
                    raise ValueError('配置後にDLLが変更されています。自動撤去しません。')
                target.unlink()
            self.record.unlink()
            return str(target.parent)
