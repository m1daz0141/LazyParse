import argparse
from pathlib import Path
import tkinter as tk

from .app import Application
from .backend import Backend, DEFAULT_MANIFEST
from .privacy import configure as configure_privacy
from .settings import STATE
from .windows import dpi_awareness, taskbar_entry
from ..paths import initialize, HISTORY
from ..portable.deployment import Deployment


def argument_parser():
    parser = argparse.ArgumentParser(description='LazyParse desktop viewer for PSO2 Classic')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--root', type=Path)
    source.add_argument('--manifest', type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument('--capture', type=Path, help='open a saved .opcap or exported report')
    parser.add_argument('--settings', type=Path, default=STATE / 'preferences.json')
    parser.add_argument('--overparse', action='store_true', help=argparse.SUPPRESS)
    return parser


def launch(args):
    configure_privacy(getattr(args, 'overparse', False))
    initialize()
    dpi_awareness()
    root = tk.Tk()
    deployment = Deployment()
    preparation_error = None
    try:
        deployment.prepare()
    except (OSError, ValueError) as error:
        preparation_error = str(error)
    backend = Backend(args.manifest, root=args.root, log_directory=HISTORY, deployment=deployment if not preparation_error else None)
    application = Application(root, backend, settings_path=args.settings)
    root.after(150, lambda: taskbar_entry(root))
    if args.capture:
        application.open_log(args.capture)
    else:
        if preparation_error:
            application.notify('取得DLLを準備できません: ' + preparation_error)
        else:
            root.after(600, application.ensure_loader)
    return root, application


def main():
    root, _ = launch(argument_parser().parse_args())
    root.mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
