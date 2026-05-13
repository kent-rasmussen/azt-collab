"""Diagnostic logging for transient bugs.

Used 2026-05-13 to diagnose a "first-try-fails, second-try-
works" crash in the SettingsScreen → pick-project flow on a
remote tester's Tecno KN4 (Helio G81, 4 GB RAM, Android 16).
The tester can't run logcat; the peer's stderr ends up in
``/sdcard/azt_recorder.log``, so emitting unconditionally is
the only way to capture probe output.

Usage::

    from azt_collab_client._debug import first_try_log
    first_try_log('settings.tick', current_screen='settings',
                  banner_visible=False)

Output goes to stderr with ``[first-try] <label> k=v k=v ...``
formatting so it's grep-friendly.

**Gating.** Currently always-on for the 0.41.15+ diagnostic
window. The previous env-var gate (``AZT_DEBUG_FIRST_TRY``)
was useless when the tester can't set env vars on their
device. Restore the gate once the crash is diagnosed:

    if not os.environ.get('AZT_DEBUG_FIRST_TRY'):
        return
"""

import sys


def first_try_log(label, **fields):
    """Emit a ``[first-try] <label> k=v ...`` line to stderr.
    Currently unconditional (see module docstring); cheap, so
    leaving it on across the diagnostic window doesn't matter."""
    parts = ' '.join(f'{k}={v!r}' for k, v in fields.items())
    print(f'[first-try] {label} {parts}',
          file=sys.stderr, flush=True)
