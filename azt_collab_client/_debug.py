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

**Gating — restored 0.54.94.** These were unconditional for the
0.41.15 diagnostic window, with the module's own instruction to
"restore the gate once the crash is diagnosed". That crash was
diagnosed long ago; the lines stayed on through 0.54.x, where
the call sites include per-RPC (``transport.call.post``) and
per-file-open (``lift_io.openFileDescriptor``) hooks plus a
1 Hz settings tick — so a field log filled with them, burying
the lines that mattered (Kent 2026-07-27: "is there any point
to these log entries?").

Set ``AZT_DEBUG_FIRST_TRY=1`` to turn them back on. Call sites
are deliberately left in place: re-enabling a probe should be
an env var, not a code change.
"""

import os
import sys

_ENABLED = bool(os.environ.get('AZT_DEBUG_FIRST_TRY'))


def first_try_log(label, **fields):
    """Emit a ``[first-try] <label> k=v ...`` line to stderr when
    ``AZT_DEBUG_FIRST_TRY`` is set; otherwise return immediately.

    The check is a module-level constant, so a disabled call costs
    one attribute load — cheap enough for the per-RPC sites."""
    if not _ENABLED:
        return
    parts = ' '.join(f'{k}={v!r}' for k, v in fields.items())
    print(f'[first-try] {label} {parts}',
          file=sys.stderr, flush=True)
