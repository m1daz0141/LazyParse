"""DPI and taskbar behavior for the application window."""
import ctypes
from ctypes import wintypes
import sys


def dpi_awareness():
    if sys.platform == 'win32':
        user = ctypes.WinDLL('user32', use_last_error=True)
        try:
            user.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except AttributeError:
            user.SetProcessDPIAware()


def window_handle(window):
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user.GetAncestor.restype = wintypes.HWND
    return user.GetAncestor(window.winfo_id(), 2)


def taskbar_entry(window):
    if sys.platform != 'win32':
        return
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    handle = window_handle(window)
    style = user.GetWindowLongPtrW(handle, -20)
    user.SetWindowLongPtrW(handle, -20, (style | 0x40000) & ~0x80)
