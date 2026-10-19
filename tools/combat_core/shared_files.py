"""Read published files without blocking Windows atomic replacement."""
import ctypes
from ctypes import wintypes
from functools import lru_cache
import os


@lru_cache(maxsize=1)
def _file_api():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def open_read(path):
    if os.name != 'nt':
        return open(path, 'rb')
    import msvcrt
    kernel = _file_api()
    # GENERIC_READ, SHARE_READ | SHARE_WRITE | SHARE_DELETE, OPEN_EXISTING.
    handle = kernel.CreateFileW(os.fspath(path), 0x80000000, 7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    try:
        return os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise


def read_text(path, encoding='utf-8-sig'):
    with open_read(path) as stream:
        return stream.read().decode(encoding)
