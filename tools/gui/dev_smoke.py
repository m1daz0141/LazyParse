"""Developer-only rendering check: python -m tools.gui.dev_smoke --capture FILE."""
from pathlib import Path
import sys
import time

from .__main__ import argument_parser, launch
from .dev_capture import capture_window


def main():
    parser = argument_parser()
    parser.add_argument('--screenshot', type=Path)
    parser.add_argument('--details-screenshot', type=Path)
    args = parser.parse_args()
    if args.capture is None:
        parser.error('a saved --capture is required; this check never installs a game loader')
    root, application = launch(args)
    failed = []
    deadline = time.monotonic() + 12

    def finish():
        ready = bool(application.report) and application.saved is not None
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
                capture_window(application.details[application.selected].window, args.details_screenshot)
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
