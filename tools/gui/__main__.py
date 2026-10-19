import argparse
from pathlib import Path
import sys
import time
import tkinter as tk

from .app import Application
from .backend import Backend, DEFAULT_MANIFEST
from .settings import STATE
from .windows import dpi_awareness, capture_window, taskbar_entry


def main():
    parser = argparse.ArgumentParser(description='LazyParse desktop viewer for PSO2 Classic')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--root', type=Path)
    source.add_argument('--manifest', type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument('--capture', type=Path, help='open a saved .opcap or exported report')
    parser.add_argument('--settings', type=Path, default=STATE / 'preferences.json')
    parser.add_argument('--smoke-test', action='store_true', help='render, optionally capture, then exit')
    parser.add_argument('--screenshot', type=Path)
    parser.add_argument('--details-screenshot', type=Path)
    args = parser.parse_args()
    if (args.screenshot or args.details_screenshot) and not args.smoke_test:
        parser.error('screenshots require --smoke-test')
    dpi_awareness()
    root = tk.Tk()
    backend = Backend(args.manifest, root=args.root, log_directory=STATE / 'logs')
    application = Application(root, backend, settings_path=args.settings)
    root.after(150, lambda: taskbar_entry(root))
    if args.capture:
        application.open_log(args.capture)
    failed = []
    if args.smoke_test:
        deadline = time.monotonic() + 12
        def finish():
            ready = bool(application.report) and (not args.capture or application.saved is not None)
            if not ready and time.monotonic() < deadline:
                root.after(100, finish)
                return
            try:
                if not ready:
                    raise RuntimeError('no report became available during UI smoke test')
                application._clear_notice()
                if args.screenshot:
                    capture_window(root, args.screenshot)
                if args.details_screenshot:
                    if not application.rows:
                        raise RuntimeError('no actor row for details')
                    application.selected = application.rows[0]['key']
                    application._open_selected()
                    detail = application.details[application.selected]
                    capture_window(detail.window, args.details_screenshot)
            except Exception as error:
                failed.append(str(error))
            application.close()
        root.after(700, finish)
    root.mainloop()
    if failed:
        print('\n'.join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
