#!/usr/bin/env python3
"""Verify project contents against the consolidation manifest after history squashing."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(
        ['git', '-C', str(ROOT), *args], text=True, stderr=subprocess.PIPE
    ).strip()


def main():
    manifest = json.loads((ROOT / 'migration/sources.json').read_text())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', default='HEAD', help='Revision to compare with the recorded project snapshots (default: HEAD).')
    revision = parser.parse_args().revision
    snapshot = git('rev-parse', '--verify', revision + '^{commit}')
    git('merge-base', '--is-ancestor', snapshot, 'HEAD')
    errors = []
    total_files = 0
    for source in manifest['repositories']:
        name = source['name']
        try:
            actual_tree = git('rev-parse', snapshot + ':' + source['path'])
            if actual_tree != source['snapshot_tree']:
                errors.append(f'{name}: tree differs from the recorded project snapshot')
            files = git('ls-tree', '-r', '--name-only', snapshot + ':' + source['path']).splitlines()
            total_files += len(files)
            if len(files) != source['file_count']:
                errors.append(f'{name}: tracked entry count differs')
        except subprocess.CalledProcessError as error:
            errors.append(f'{name}: {error.stderr.strip() or "Git ancestry check failed"}')

    gitlinks = {
        line.split('\t', 1)[1]
        for line in git('ls-tree', '-r', snapshot).splitlines()
        if line.startswith('160000 ')
    }
    try:
        module_paths = {
            line.split(' ', 1)[1]
            for line in (git('config', '-f', '.gitmodules', '--get-regexp', r'^submodule\..*\.path$').splitlines() if (ROOT / '.gitmodules').exists() else [])
        }
        if gitlinks != module_paths:
            errors.append('Root .gitmodules paths do not match the imported gitlinks')
    except subprocess.CalledProcessError as error:
        errors.append('Unable to read root submodule configuration: ' + error.stderr.strip())

    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 1
    print(
        f'Verified {len(manifest["repositories"])} projects, {total_files} tracked entries, '
        f'and {len(gitlinks)} submodule paths.'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
