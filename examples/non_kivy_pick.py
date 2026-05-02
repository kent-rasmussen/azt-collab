"""Cross-toolkit demo: drive A-Z+T project selection from a host
that has zero Kivy imports of its own.

The picker subprocess is the suite's project-picking helper — it
runs Kivy internally, but its result is delivered over stdout so any
language / toolkit can call it the same way the recorder does. This
script proves the contract.

Run this from the AZT venv (anywhere `python -m azt_collabd` is
importable):

    python azt-collab/examples/non_kivy_pick.py
"""
import subprocess
import sys


def pick_project():
    proc = subprocess.run(
        [sys.executable, '-m', 'azt_collabd', 'projects'],
        capture_output=True, text=True)
    for line in (proc.stdout or '').splitlines():
        if line.startswith('AZT_PICK\t'):
            _, path = line.split('\t', 1)
            return path.strip() or None
    return None


def main():
    path = pick_project()
    if path:
        print(f'picked: {path}')
        return 0
    print('cancelled or failed', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
