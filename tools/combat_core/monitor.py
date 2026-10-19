"""python -m tools.combat_core.monitor --root BUILD_ROOT [--once]"""
import argparse
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid

from .live import RuntimeMonitor, local_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--root', type=Path, help='follow runs under this build root')
    source.add_argument('--manifest', type=Path, help='follow the session in an autostart build manifest')
    parser.add_argument('--once', action='store_true', help='emit one snapshot and exit')
    parser.add_argument('--interval', type=float, default=0.5)
    parser.add_argument('--idle-seconds', type=float, default=10)
    parser.add_argument('--output', type=Path, help='atomic latest-state JSON outside capture inputs (new file)')
    args = parser.parse_args()
    if not math.isfinite(args.interval) or args.interval < 0.05:
        parser.error('--interval must be finite and at least 0.05 seconds')
    if not args.root and not args.manifest:
        args.manifest = Path(__file__).resolve().parents[1] / 'native_capture/bin/autostart-build.json'
    output = local_path(args.output) if args.output else None
    temporary = None
    try:
        monitor = RuntimeMonitor(args.root, manifest=args.manifest, idle_seconds=args.idle_seconds)
        if output:
            root = local_path(args.root) if args.root else local_path(json.loads(args.manifest.read_text(encoding='utf-8-sig'))['session'])
            if output.is_relative_to(root) or (args.manifest and output == args.manifest.resolve()):
                raise ValueError('output must be outside capture inputs and the build manifest')
            with output.open('x', encoding='utf-8'):
                pass
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8')
        while True:
            snapshot = monitor.poll()
            text = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
            if output:
                if snapshot.get('root') and output.is_relative_to(local_path(snapshot['root'])):
                    raise ValueError('new capture root overlaps the output path')
                temporary = output.with_name(output.name + '.' + uuid.uuid4().hex + '.tmp')
                with temporary.open('x', encoding='utf-8') as stream:
                    stream.write(text + '\n')
                os.replace(temporary, output)
                temporary = None
            print(text, flush=True)
            if args.once:
                break
            time.sleep(args.interval)
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f'Cannot monitor capture: {error}', file=sys.stderr)
        return 2
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


if __name__ == '__main__':
    raise SystemExit(main())
