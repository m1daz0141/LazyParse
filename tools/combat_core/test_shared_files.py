import json
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from .shared_files import open_read, read_text
from .live import read_json

HOST = Path(__file__).resolve().parents[1] / 'native_capture/bin/AtomicFileTestHost.exe'


class SharedFileTests(unittest.TestCase):
    def test_bom_unicode_json_and_missing_file(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'status.json'
            path.write_text(json.dumps({'name': '人物'}, ensure_ascii=False), encoding='utf-8-sig')
            self.assertEqual(read_json(path), {'name': '人物'})
            self.assertIn('人物', read_text(path))
            with self.assertRaises(FileNotFoundError):
                read_text(path.with_name('missing.json'))

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics')
    def test_shared_reader_allows_publication_while_old_snapshot_stays_readable(self):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.ReplaceFileW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                        wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
        kernel.ReplaceFileW.restype = wintypes.BOOL
        with tempfile.TemporaryDirectory() as work:
            path, temporary = Path(work) / 'status.json', Path(work) / 'next.json'
            path.write_text('old', encoding='utf-8')
            with path.open('rb'):
                temporary.write_text('new', encoding='utf-8')
                self.assertFalse(kernel.ReplaceFileW(str(path), str(temporary), None, 0, None, None))
                self.assertIn(ctypes.get_last_error(), (5, 32, 33))
            with open_read(path) as snapshot:
                for number in range(25):
                    temporary.write_text(str(number), encoding='utf-8')
                    if not kernel.ReplaceFileW(str(path), str(temporary), None, 0, None, None):
                        raise ctypes.WinError(ctypes.get_last_error())
                    self.assertEqual(read_text(path), str(number))
                self.assertEqual(snapshot.read(), b'old')

    @unittest.skipUnless(os.name == 'nt' and HOST.exists(), 'build native publication fixture first')
    def test_native_temporary_and_permanent_locks_and_delete_sharing(self):
        self.assertTrue(HOST.exists(), 'build.ps1 must build AtomicFileTestHost')
        with tempfile.TemporaryDirectory() as work:
            process = subprocess.run([str(HOST), work], capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            self.assertEqual(process.stdout.count('PASS:'), 3)

    @unittest.skipUnless(os.name == 'nt' and HOST.with_name('ObserverTestHost.exe').exists() and
                         HOST.with_name('OverParseCaptureTest.dll').exists(), 'build observer fixture first')
    def test_real_observer_publishes_while_status_reader_is_held(self):
        with tempfile.TemporaryDirectory() as work:
            output = Path(work) / 'observer-run'
            process = subprocess.run([str(HOST.with_name('ObserverTestHost.exe')),
                str(HOST.with_name('OverParseCaptureTest.dll')), str(output), '--shared-status-reader'],
                capture_output=True, text=True, timeout=15)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            self.assertIn('real observer publishes', process.stdout)
            status = read_json(output / 'identity-context/context-status.json')
            self.assertTrue(status['stopped'])
            self.assertEqual(status['io_error'], 0)
            self.assertEqual(status['dropped'], 0)


if __name__ == '__main__':
    unittest.main()
