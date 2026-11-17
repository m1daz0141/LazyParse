"""Reject restricted-build implementation in the Full repository."""
import argparse
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = {'https://github.com/m1daz0141/LazyParse-Full.git',
            'git@github.com:m1daz0141/LazyParse-Full.git'}
SELF = 'tools/release/check_full_variant.py'
DENIED_PATH = re.compile(r'(?:^|/)(?:limited[^/]*|limit_main\.py|limit_entry\.py|limit_test_host\.c|'
                         r'memory_stream\.py|test_limited\.py|verify_limit_exe\.py|build_limit_native\.ps1|'
                         r'Build-LazyParse-Limit\.ps1|LazyParse-Limit\.spec)(?:$|/)', re.I)
DENIED_CONTENT = (b'LAZYPARSE_LIMIT', b'LIMIT_BUILD', b'-show-multi', b'limited_summary',
                  b'limited-captures', b'limit-bin', b'memory_only', b'from .limited', b'limit_main')


def git(*args):
    return subprocess.check_output(['git', '-c', f'safe.directory={ROOT.as_posix()}', '-C', str(ROOT), *args])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--revision')
    parser.add_argument('--remote')
    args = parser.parse_args()
    remote = args.remote or git('remote', 'get-url', '--push', 'origin').decode().strip()
    if remote not in EXPECTED:
        raise SystemExit('Full publication rejected: remote must be LazyParse-Full')
    prefix = args.revision + ':' if args.revision else ':'
    names = (git('ls-tree', '-r', '--name-only', '-z', args.revision) if args.revision else
             git('diff', '--cached', '--name-only', '--diff-filter=ACMRT', '-z'))
    failures = []
    for name in filter(None, names.decode().split('\0')):
        if name in {SELF, '.gitignore'}:
            continue
        if DENIED_PATH.search(name):
            failures.append(name)
            continue
        data = git('show', prefix + name)
        if any(token in data for token in DENIED_CONTENT):
            failures.append(name)
    if failures:
        raise SystemExit('Full publication rejected: restricted code in ' + ', '.join(failures))
    print('Full variant and remote checks passed')


if __name__ == '__main__':
    main()
