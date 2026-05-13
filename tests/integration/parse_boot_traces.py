"""Parse boot-trace lines from a logcat dump into a timing table.

Reads logcat output (from ``adb logcat -d`` or a saved log file) and
extracts ``[boot-trace-peer]`` and ``[boot-trace-daemon]`` lines.
Aligns the two streams by logcat's wall-clock timestamps so a single
table shows both processes against a common timeline.

Used by ``measure_boot.sh`` to answer:

- **Q2 (doze)**: did the daemon's ``module_loaded → after_install_callbacks``
  trace fire while the device was in doze, and how long did it take
  vs. baseline?
- **Q3 (prewarm)**: with ``prewarm_called`` overlapping the peer's
  Kivy boot, what's the delta between
  ``compat_ok`` and ``bootstrap_called``? A near-zero delta means
  the prewarm bound the daemon before bootstrap ran; a large delta
  means it didn't help.

Standalone usage:

    python tests/integration/parse_boot_traces.py < logcat.txt

Or pipe directly:

    adb logcat -d | python tests/integration/parse_boot_traces.py

Output is a tab-separated table sorted by wall-clock; first column is
the source process (peer/daemon).
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass


# Logcat default format ("threadtime"):
#   05-09 14:37:21.123  1234  5678 I tag     : message
# We extract: month-day hour:minute:second.millis, log level, tag,
# message. Timestamp resolution is 1ms — enough for our scale.
_LOGCAT_RE = re.compile(
    r'^(?P<ts>\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+'
    r'\d+\s+\d+\s+'
    r'(?P<level>[VDIWEFS])\s+'
    r'(?P<tag>\S+)\s*:\s*'
    r'(?P<msg>.*)$'
)
# Boot-trace payload:
#   phase=<phase> t=<seconds> [k=v ...]
_TRACE_RE = re.compile(
    r'\[boot-trace-(?P<proc>peer|daemon)\]\s+'
    r'phase=(?P<phase>\S+)\s+'
    r't=(?P<t>[0-9.]+)'
    r'(?P<extra>(?:\s+\S+=\S+)*)'
)


@dataclass
class Trace:
    wall: str           # logcat wall-clock timestamp
    proc: str           # 'peer' or 'daemon'
    phase: str
    t_proc: float       # seconds since this process started
    extras: dict


def parse(stream):
    out = []
    for line in stream:
        line = line.rstrip('\n')
        m = _LOGCAT_RE.match(line)
        msg = m.group('msg') if m else line
        wall = m.group('ts') if m else ''
        tm = _TRACE_RE.search(msg)
        if not tm:
            continue
        extras = {}
        for kv in (tm.group('extra') or '').strip().split():
            if '=' in kv:
                k, v = kv.split('=', 1)
                extras[k] = v
        out.append(Trace(
            wall=wall, proc=tm.group('proc'),
            phase=tm.group('phase'), t_proc=float(tm.group('t')),
            extras=extras,
        ))
    return out


def render(traces):
    if not traces:
        print('(no boot-trace lines found in input)', file=sys.stderr)
        return
    traces.sort(key=lambda t: (t.wall, t.proc))
    print('\t'.join(['wall', 'proc', 'phase', 't_proc', 'extras']))
    for t in traces:
        extras = ' '.join(f'{k}={v}' for k, v in t.extras.items())
        print(f'{t.wall}\t{t.proc}\t{t.phase}\t{t.t_proc:.3f}\t{extras}')


def summarise(traces):
    """Print key intervals for the cold-start question.

    - peer ``bootstrap_called → compat_ok`` (the user-visible wait)
    - daemon ``module_loaded → after_install_callbacks`` (Python boot)
    - peer ``prewarm_called → compat_ok`` (overlap savings if any)
    """
    by_proc = {'peer': {}, 'daemon': {}}
    for t in traces:
        # First-occurrence wins. If the daemon respawned mid-run we
        # only report the first cycle, which is what cold-start
        # measurements care about.
        by_proc[t.proc].setdefault(t.phase, t)

    def delta(proc, a, b):
        ta = by_proc[proc].get(a)
        tb = by_proc[proc].get(b)
        if ta is None or tb is None:
            return None
        return tb.t_proc - ta.t_proc

    print('# summary', file=sys.stderr)
    candidates = [
        ('peer', 'bootstrap_called', 'compat_ok',
         'peer wait until daemon answered'),
        ('peer', 'prewarm_called', 'compat_ok',
         'prewarm overlap window'),
        ('daemon', 'module_loaded', 'after_install_callbacks',
         'daemon Python boot to dispatcher live'),
        ('daemon', 'before_import_azt_collabd',
         'after_import_azt_collabd',
         'azt_collabd import cost'),
        ('daemon', 'after_install_callbacks',
         'after_reconcile',
         'reconcile_on_startup cost'),
    ]
    for proc, a, b, label in candidates:
        d = delta(proc, a, b)
        if d is None:
            print(f'  {proc:6s} {label:48s} (missing phase)',
                  file=sys.stderr)
        else:
            print(f'  {proc:6s} {label:48s} {d:7.3f}s',
                  file=sys.stderr)


def main(argv):
    traces = parse(sys.stdin)
    render(traces)
    summarise(traces)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
