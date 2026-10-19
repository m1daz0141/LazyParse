"""Deterministic lock checks for the real control client's JSON reader."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


PWSH = shutil.which('pwsh')
CLIENT = Path(__file__).with_name('runtime_client.ps1')
HARNESS = r'''
$ErrorActionPreference = 'Stop'
$ast = [Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$null, [ref]$null)
$fn = $ast.Find({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Read-SharedJson'
}, $true)
Invoke-Expression $fn.Extent.Text
[Console]::WriteLine('READY')
[Console]::Out.Flush()
$null = [Console]::ReadLine()
$timer = [Diagnostics.Stopwatch]::StartNew()
try {
    $value = Read-SharedJson $args[1]
    @{ ok = $true; value = $value; elapsed_ms = $timer.ElapsedMilliseconds } | ConvertTo-Json -Compress
} catch {
    $cause = $_.Exception
    while ($cause.InnerException) { $cause = $cause.InnerException }
    @{ ok = $false; code = ($cause.HResult -band 0xffff); elapsed_ms = $timer.ElapsedMilliseconds } | ConvertTo-Json -Compress
}
'''


@unittest.skipUnless(os.name == 'nt' and PWSH, 'Windows and PowerShell required')
class SharedControlReaderTests(unittest.TestCase):
    def test_transient_missing_published_file_recovers_and_persistent_missing_is_bounded(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'status.json'
            harness = Path(work) / 'reader.ps1'
            harness.write_text(HARNESS, encoding='utf-8')
            for create in (False, True):
                with self.subTest(create_during_retry=create):
                    process = subprocess.Popen([PWSH, '-NoProfile', '-File', str(harness), str(CLIENT), str(path)],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, encoding='utf-8', creationflags=0x08000000)
                    try:
                        self.assertEqual(process.stdout.readline().strip(), 'READY')
                        process.stdin.write('GO\n')
                        process.stdin.flush()
                        if create:
                            time.sleep(.04)
                            path.write_text('{"value": 42}', encoding='utf-8')
                        stdout, stderr = process.communicate(timeout=5)
                        self.assertEqual(process.returncode, 0, stderr)
                        result = json.loads(stdout)
                        self.assertEqual(result['ok'], create, result)
                        if create:
                            self.assertEqual(result['value']['value'], 42)
                        else:
                            self.assertEqual(result['code'], 2)
                            self.assertGreaterEqual(result['elapsed_ms'], 80)
                            self.assertLess(result['elapsed_ms'], 2000)
                    finally:
                        if process.poll() is None:
                            process.kill()  # Only this test's PowerShell reader.
                        process.communicate(timeout=5)

    def test_temporary_lock_recovers_but_persistent_lock_is_bounded(self):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'status.json'
            path.write_text('{"value": 42}', encoding='utf-8')
            harness = Path(work) / 'reader.ps1'
            harness.write_text(HARNESS, encoding='utf-8')
            for release in (True, False):
                with self.subTest(temporary=release):
                    process = subprocess.Popen([PWSH, '-NoProfile', '-File', str(harness), str(CLIENT), str(path)],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, encoding='utf-8', creationflags=0x08000000)
                    handle = None
                    try:
                        self.assertEqual(process.stdout.readline().strip(), 'READY')
                        handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
                        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
                        process.stdin.write('GO\n')
                        process.stdin.flush()
                        if release:
                            time.sleep(.04)
                            kernel.CloseHandle(handle)
                            handle = None
                        stdout, stderr = process.communicate(timeout=5)
                        self.assertEqual(process.returncode, 0, stderr)
                        result = json.loads(stdout)
                        self.assertEqual(result['ok'], release, result)
                        if release:
                            self.assertEqual(result['value']['value'], 42)
                        else:
                            self.assertEqual(result['code'], 32)
                            self.assertGreaterEqual(result['elapsed_ms'], 80)
                            self.assertLess(result['elapsed_ms'], 2000)
                    finally:
                        if handle is not None:
                            kernel.CloseHandle(handle)
                        if process.poll() is None:
                            process.kill()  # Only this test's PowerShell reader.
                        process.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
