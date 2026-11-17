import ctypes
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from . import acl


class AclTests(unittest.TestCase):
    def test_real_grant_is_additive_recursive_and_inherited_by_new_dll(self):
        with tempfile.TemporaryDirectory() as work:
            folder = Path(work) / '日本語 & space/pso2_bin/sub'
            child = folder / 'nested'
            child.mkdir(parents=True)
            existing = child / 'existing.dll'
            existing.write_bytes(b'original')
            sid = acl.current_user_sid()
            # Owner of this temporary folder can grant rights without UAC.
            with patch.object(acl, 'is_admin', return_value=True):
                acl.grant_sub_access(folder)
            self.assertEqual(existing.read_bytes(), b'original')
            (folder / 'eos_gaming.dll').write_bytes(b'new')
            # Ask Windows for actual ACEs, rather than just asserting the command.
            import os
            env = dict(os.environ, LAZYPARSE_ACL_TEST_FOLDER=str(folder), LAZYPARSE_ACL_TEST_SID=sid)
            script = """
$ErrorActionPreference = 'Stop'
$folder = $env:LAZYPARSE_ACL_TEST_FOLDER
$sid = $env:LAZYPARSE_ACL_TEST_SID
foreach ($path in @($folder, (Join-Path $folder 'nested/existing.dll'), (Join-Path $folder 'eos_gaming.dll'))) {
    $rules = @((Get-Acl -LiteralPath $path).Access | Where-Object {
        $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -eq $sid -and
        $_.AccessControlType -eq 'Allow' -and ($_.FileSystemRights -band 2032127) -eq 2032127
    })
    if (-not $rules.Count) { throw "FullControl absent: $path" }
    if ($path -eq $folder -and -not @($rules | Where-Object { -not $_.IsInherited -and ($_.InheritanceFlags -band 3) -eq 3 }).Count) { throw 'OI/CI grant absent' }
    if ($path.EndsWith('eos_gaming.dll') -and -not @($rules | Where-Object IsInherited).Count) { throw 'DLL inheritance absent' }
}
"""
            powershell = str(Path(acl.system_icacls()).parent / 'WindowsPowerShell/v1.0/powershell.exe')
            result = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-Command', script],
                env=env, capture_output=True, text=True, errors='replace', creationflags=0x08000000)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_uac_uses_original_user_sid_and_nonzero_exit_is_failure(self):
        with tempfile.TemporaryDirectory() as work:
            folder = Path(work)/'pso2_bin/sub'
            folder.mkdir(parents=True)
            with patch.object(acl, 'current_user_sid', return_value='S-1-5-21-123'), \
                 patch.object(acl, 'system_icacls', return_value='C:/Windows/System32/icacls.exe'), \
                 patch.object(acl, 'is_admin', return_value=False), \
                 patch.object(acl, 'run_elevated', return_value=0) as elevated:
                acl.grant_sub_access(folder)
                elevated.assert_called_once_with('C:/Windows/System32/icacls.exe',
                    [str(folder.resolve()), '/grant', '*S-1-5-21-123:(OI)(CI)F', '/T', '/C'])
                elevated.return_value = 5
                with self.assertRaisesRegex(OSError, 'DLL配置を中止'):
                    acl.grant_sub_access(folder)
                elevated.side_effect = ctypes.WinError(1223)
                with self.assertRaisesRegex(ValueError, 'キャンセル'):
                    acl.grant_sub_access(folder)

    def test_elevated_process_is_awaited_and_exit_code_and_handle_are_kept(self):
        shell, kernel, ole = Mock(), Mock(), Mock()
        ole.CoInitializeEx.return_value = 0
        kernel.WaitForSingleObject.return_value = 0
        def launch(pointer):
            info = pointer._obj
            self.assertEqual(info.lpVerb, 'runas')
            self.assertEqual(info.fMask, 0x140)
            self.assertEqual(info.lpParameters, subprocess.list2cmdline(arguments))
            info.hProcess = 1234
            return True
        def exit_code(handle, pointer):
            pointer._obj.value = 5
            return True
        shell.ShellExecuteExW.side_effect = launch
        kernel.GetExitCodeProcess.side_effect = exit_code
        arguments = ['C:/space & 日本語/pso2_bin/sub', '/grant', '*S-1-5-21-123:(OI)(CI)F', '/T', '/C']
        with patch.object(acl.ctypes, 'WinDLL', side_effect=lambda name, **_: {'shell32': shell, 'kernel32': kernel, 'ole32': ole}[name]):
            self.assertEqual(acl.run_elevated('C:/Windows/System32/icacls.exe', arguments), 5)
        kernel.WaitForSingleObject.assert_called_once_with(1234, 0xffffffff)
        kernel.CloseHandle.assert_called_once_with(1234)
        ole.CoUninitialize.assert_called_once()

    def test_invalid_target_never_requests_elevation(self):
        with tempfile.TemporaryDirectory() as work, patch.object(acl, 'current_user_sid') as sid:
            with self.assertRaises(ValueError):
                acl.grant_sub_access(work)
            sid.assert_not_called()
