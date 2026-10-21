"""PyInstaller entry point. Writable files live beside LazyParse.exe."""
import sys
import traceback

from tools.gui.__main__ import main

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        from tkinter import messagebox
        messagebox.showerror('LazyParse', '起動できませんでした。保存先の書き込み権限も確認してください。\n\n' + traceback.format_exc())
        raise SystemExit(1)
