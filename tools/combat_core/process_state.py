"""Read-only worker identity check, independent of UI and capture control."""
import ctypes
from ctypes import wintypes
from pathlib import PureWindowsPath
import sys
import time

FILETIME_EPOCH = 116444736000000000


def utc_filetime():
    return FILETIME_EPOCH + time.time_ns() // 100


def probe_process(pid):
    if sys.platform != 'win32':
        return {'alive': None, 'error': 'process verification requires Windows'}
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION only.
    if not handle:
        error = ctypes.get_last_error()
        return {'alive': False if error == 87 else None, 'win32_error': error}
    try:
        created, exited, cpu_kernel, cpu_user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(cpu_kernel), ctypes.byref(cpu_user)):
            return {'alive': None, 'win32_error': ctypes.get_last_error()}
        path, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
        if not kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
            return {'alive': None, 'win32_error': ctypes.get_last_error()}
        return {'alive': not (exited.dwLowDateTime or exited.dwHighDateTime),
                'name': PureWindowsPath(path.value).stem,
                'started_filetime': created.dwLowDateTime | (created.dwHighDateTime << 32)}
    finally:
        kernel.CloseHandle(handle)
