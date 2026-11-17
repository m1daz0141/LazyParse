"""Apply the PSO2-Sub-ACL grant before installing the optional loader."""
import ctypes
from ctypes import wintypes
from pathlib import Path
import subprocess


def current_user_sid():
    """Capture the caller's SID before UAC can switch to another account."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                         wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))  # TokenUser
        if not needed.value:
            raise ctypes.WinError(ctypes.get_last_error())
        data = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, data, len(data), ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel.CloseHandle(token)


def system_icacls():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    directory = ctypes.create_unicode_buffer(32768)
    size = kernel.GetSystemDirectoryW(directory, len(directory))
    if not size or size >= len(directory):
        raise ctypes.WinError(ctypes.get_last_error())
    return str(Path(directory.value) / 'icacls.exe')


def is_admin():
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    shell.IsUserAnAdmin.restype = wintypes.BOOL
    return bool(shell.IsUserAnAdmin())


class ShellExecuteInfo(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('fMask', wintypes.ULONG), ('hwnd', wintypes.HWND),
        ('lpVerb', wintypes.LPCWSTR), ('lpFile', wintypes.LPCWSTR), ('lpParameters', wintypes.LPCWSTR),
        ('lpDirectory', wintypes.LPCWSTR), ('nShow', ctypes.c_int), ('hInstApp', wintypes.HINSTANCE),
        ('lpIDList', ctypes.c_void_p), ('lpClass', wintypes.LPCWSTR), ('hkeyClass', wintypes.HKEY),
        ('dwHotKey', wintypes.DWORD), ('hIcon', wintypes.HANDLE), ('hProcess', wintypes.HANDLE)]


def run_elevated(executable, arguments):
    """Elevate only icacls and wait for its exit code before copying any DLL."""
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    ole = ctypes.WinDLL('ole32', use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell.ShellExecuteExW.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole.CoInitializeEx.restype = ctypes.c_long
    initialized = ole.CoInitializeEx(None, 0x6)  # STA, disable OLE1 DDE
    if initialized < 0 and initialized != -2147417850:  # Existing apartment is also usable.
        raise OSError(f'COM初期化に失敗: {initialized:#x}')
    try:
        info = ShellExecuteInfo(cbSize=ctypes.sizeof(ShellExecuteInfo), fMask=0x140,
            lpVerb='runas', lpFile=executable, lpParameters=subprocess.list2cmdline(arguments), nShow=0)
        if not shell.ShellExecuteExW(ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not info.hProcess:
            raise OSError('アクセス権変更プロセスの起動を確認できない')
        try:
            if kernel.WaitForSingleObject(info.hProcess, 0xffffffff) != 0:
                raise ctypes.WinError(ctypes.get_last_error())
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(info.hProcess, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            return code.value
        finally:
            kernel.CloseHandle(info.hProcess)
    finally:
        if initialized >= 0:
            ole.CoUninitialize()


def grant_sub_access(folder):
    folder = Path(folder).resolve(strict=True)
    if (str(folder).startswith(('\\\\', '//')) or folder.name.casefold() != 'sub' or
            folder.parent.name.casefold() != 'pso2_bin'):
        raise ValueError('アクセス権変更の対象はローカルのpso2_bin/subのみ')
    sid = current_user_sid()
    executable = system_icacls()
    # Same additive, recursive grant as Grant-PSO2SubAccess.ps1. No /reset or /grant:r.
    arguments = [str(folder), '/grant', f'*{sid}:(OI)(CI)F', '/T', '/C']
    details = ''
    try:
        if is_admin():
            result = subprocess.run([executable, *arguments], capture_output=True,
                text=True, errors='replace', creationflags=0x08000000)
            code = result.returncode
            details = (result.stdout + result.stderr).strip()
        else:
            code = run_elevated(executable, arguments)
    except OSError as error:
        if getattr(error, 'winerror', None) == 1223:
            raise ValueError('アクセス権変更をキャンセルしたため、DLL配置を中止') from error
        raise OSError('subのアクセス権変更に失敗、DLL配置を中止: ' + str(error)) from error
    if code != 0:
        raise OSError(f'subのアクセス権変更に失敗（icacls終了コード: {code}）、DLL配置を中止'
                      '\n一部のアクセス権は変更済みの可能性あり' + ('\n' + details if details else ''))
