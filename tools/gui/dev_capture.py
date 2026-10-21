"""Developer-only PNG capture of this application window."""
import ctypes
from ctypes import wintypes
from pathlib import Path
import struct
import sys
import zlib

from .windows import window_handle


def capture_window(window, path):
    if sys.platform != 'win32':
        raise OSError('window capture requires Windows')
    path = Path(path)
    if path.exists():
        raise FileExistsError(path)
    window.update()
    user = ctypes.WinDLL('user32', use_last_error=True)
    gdi = ctypes.WinDLL('gdi32', use_last_error=True)
    handle = window_handle(window)
    rect = wintypes.RECT()
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    if not user.GetWindowRect(handle, ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    width, height = rect.right - rect.left, rect.bottom - rect.top
    class BitmapInfo(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('width', wintypes.LONG), ('height', wintypes.LONG),
            ('planes', wintypes.WORD), ('bits', wintypes.WORD), ('compression', wintypes.DWORD),
            ('image_size', wintypes.DWORD), ('x', wintypes.LONG), ('y', wintypes.LONG),
            ('used', wintypes.DWORD), ('important', wintypes.DWORD)]
    info = BitmapInfo(ctypes.sizeof(BitmapInfo), width, -height, 1, 32, 0, width * height * 4, 0, 0, 0, 0)
    gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi.CreateCompatibleDC.restype = wintypes.HDC
    gdi.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BitmapInfo), wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    gdi.CreateDIBSection.restype = wintypes.HANDLE
    gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
    gdi.SelectObject.restype = wintypes.HANDLE
    gdi.DeleteObject.argtypes = [wintypes.HANDLE]
    gdi.DeleteDC.argtypes = [wintypes.HDC]
    user.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user.PrintWindow.restype = wintypes.BOOL
    dc = gdi.CreateCompatibleDC(None)
    bitmap = old = None
    try:
        pixels = ctypes.c_void_p()
        bitmap = gdi.CreateDIBSection(dc, ctypes.byref(info), 0, ctypes.byref(pixels), None, 0)
        if not dc or not bitmap:
            raise ctypes.WinError(ctypes.get_last_error())
        old = gdi.SelectObject(dc, bitmap)
        if not user.PrintWindow(handle, dc, 2):
            raise ctypes.WinError(ctypes.get_last_error())
        raw = ctypes.string_at(pixels, width * height * 4)
        scanlines = bytearray()
        for y in range(height):
            scanlines.append(0)
            for offset in range(y * width * 4, (y + 1) * width * 4, 4):
                scanlines.extend((raw[offset + 2], raw[offset + 1], raw[offset]))
        def chunk(kind, data):
            return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
        png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
        png += chunk(b'IDAT', zlib.compress(bytes(scanlines))) + chunk(b'IEND', b'')
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(png)
    finally:
        if old:
            gdi.SelectObject(dc, old)
        if bitmap:
            gdi.DeleteObject(bitmap)
        if dc:
            gdi.DeleteDC(dc)
