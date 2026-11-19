"""DPI and taskbar behavior for the application window."""
import ctypes
from ctypes import wintypes
import sys

GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_MINIMIZEBOX, WS_SYSMENU = 0x00020000, 0x00080000
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x00000080, 0x00040000
SW_HIDE, SW_MINIMIZE, SW_SHOWNA = 0, 6, 8


def dpi_awareness():
    if sys.platform == 'win32':
        user = ctypes.WinDLL('user32', use_last_error=True)
        try:
            user.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except AttributeError:
            user.SetProcessDPIAware()


def _user32():
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user.GetAncestor.restype = wintypes.HWND
    user.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    return user


def window_handle(window):
    return _user32().GetAncestor(window.winfo_id(), 2)


def taskbar_entry(window):
    """Give the borderless (override-redirect) window a normal taskbar button.

    Tk creates such windows as tool windows. The taskbar only re-reads the
    extended style when the window is shown, so a visible window is hidden and
    shown once without activation. The minimize box lets a click on the taskbar
    button minimize/restore it instead of a pinned icon starting another copy.
    """
    if sys.platform != 'win32':
        return
    user = _user32()
    handle = user.GetAncestor(window.winfo_id(), 2)
    style = user.GetWindowLongPtrW(handle, GWL_STYLE)
    user.SetWindowLongPtrW(handle, GWL_STYLE, style | WS_MINIMIZEBOX | WS_SYSMENU)
    extended = user.GetWindowLongPtrW(handle, GWL_EXSTYLE)
    user.SetWindowLongPtrW(handle, GWL_EXSTYLE, (extended | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW)
    if user.IsWindowVisible(handle):
        user.ShowWindow(handle, SW_HIDE)
        user.ShowWindow(handle, SW_SHOWNA)


def minimize(window):
    """Minimize without dropping override-redirect (which recreates the window)."""
    if sys.platform != 'win32':
        window.overrideredirect(False)
        window.iconify()
        return
    _user32().ShowWindow(window_handle(window), SW_MINIMIZE)
