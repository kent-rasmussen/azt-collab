#!/usr/bin/env python3
"""Classify commits in <base>..<tip> by file count, total bytes, max-file.

Usage:
    cd /path/to/local-clone-with-full-history
    python classify_pending.py <base-sha> <tip-sha>

For baf (the stuck tester at the time this was written):
    base=7a0e1710  tip=c115b64c

One row per commit (oldest first). The rightmost column shows the
single largest file added in that commit — use it to spot the
"someone recorded a phrase/text instead of a word" cases the
recorder isn't supposed to allow.

Prereq: the local clone must contain the full pending history.
Since the diverged commits live only on the tester's Android device
(they haven't pushed), you need a copy of the daemon's working_dir
``.git`` from there — share/zip + transfer is fine, dulwich's
on-disk format is platform-independent.
"""
import subprocess
import sys


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    base, tip = sys.argv[1], sys.argv[2]

    shas = subprocess.check_output(
        ['git', 'log', '--reverse', '--format=%H', f'{base}..{tip}'],
        text=True,
    ).split()

    print(f"{'sha':12} {'files':>5} {'total_MB':>9} {'max_KB':>9}  max_file")
    for sha in shas:
        out = subprocess.check_output(
            ['git', 'diff-tree', '-r', '--no-commit-id', '--root', sha],
            text=True,
        )
        files = []
        for line in out.splitlines():
            if not line.startswith(':') or '\t' not in line:
                continue
            meta, path = line.split('\t', 1)
            parts = meta.split()
            if len(parts) < 4:
                continue
            blob_new = parts[3]
            if blob_new == '0' * 40:
                continue
            try:
                size = int(subprocess.check_output(
                    ['git', 'cat-file', '-s', blob_new], text=True))
            except subprocess.CalledProcessError:
                continue
            files.append((path, size))
        if not files:
            print(f'{sha[:12]} (no blob changes)')
            continue
        total = sum(s for _, s in files)
        max_path, max_size = max(files, key=lambda x: x[1])
        print(f'{sha[:12]} {len(files):5d} '
              f'{total / 1024 / 1024:9.2f} {max_size / 1024:9.1f}  '
              f'{max_path}')


if __name__ == '__main__':
    main()
