"""Push-notification helper.

Calls into ``AZTCollabProvider.notifyStatusChanged`` (via jnius) so
peers that registered a ContentObserver on the status URI wake up
within ~10 ms instead of waiting for their next ``project_status``
poll. The notification dispatch is async — this function returns
fast and doesn't block on observer execution.

Two entry points:

  notify_project_changed(langcode)
      Per-project URI. Wakes observers registered on
      ``content://org.atoznback.aztcollab/status/<langcode>``
      directly, AND observers registered on the parent
      ``content://org.atoznback.aztcollab/status`` URI with
      ``notifyForDescendants=True``.

  notify_global_changed()
      Parent URI only. For daemon-wide events that affect every
      project's status (toggle flips, peer-list mutations). Reaches
      only descendants-mode subscribers (a project-list UI), not
      per-project subscribers.

Both are no-ops on non-Android — the loopback daemon has no
ContentProvider, so peers fall back to polling per
CLIENT_INTEGRATION.md § 17b.

Threading: safe to call from any thread. ContentResolver.notifyChange
is thread-safe; jnius cross-thread calls work as long as the thread
is attached to the JVM (Python-spawned threads attach lazily via the
bootclassloader on first jnius call).
"""

import os
import sys


def _is_android():
    # Dependency-free — do NOT `import kivy.utils` here. This runs on
    # EVERY commit and post-receive (via notify_project_changed), and
    # importing kivy.utils pulls ALL of Kivy (logger, Config, ~/.kivy)
    # into the DAEMON on desktop: Kivy's logger hijacks root logging /
    # stdio (the detached-daemon "log went silent" death) and it
    # violates the no-Kivy-in-daemon invariant. Mirror the Android
    # signals of kivy.utils._get_platform via environment only (same
    # answer a Kivy host would get — the recorder/viewer set
    # ANDROID_ARGUMENT before Python starts). See client _platform.py,
    # which exists since 0.53.1 for exactly this reason.
    return ('ANDROID_ARGUMENT' in os.environ
            or os.environ.get('KIVY_BUILD') == 'android'
            or 'P4A_BOOTSTRAP' in os.environ)


_provider_cls = None
_not_android = False  # cached desktop negative (Android is env-stable,
#                       so this never flips; set once, avoids re-checking)


def _get_provider_class():
    """Lazy-load + cache the AZTCollabProvider Java class. Returns
    None off Android or if jnius isn't available."""
    global _provider_cls, _not_android
    if _provider_cls is not None:
        return _provider_cls
    if _not_android:
        return None
    if not _is_android():
        # Desktop: cache the negative so we stop re-checking forever
        # (the pre-fix code re-ran the check — and its Kivy import —
        # on every notify). Only the desktop None is cached; on Android
        # a failed autoclass below stays uncached so it can retry.
        _not_android = True
        return None
    try:
        from jnius import autoclass
        _provider_cls = autoclass(
            'org.atoznback.aztcollab.AZTCollabProvider')
    except Exception as ex:
        print(f'[notify] autoclass(AZTCollabProvider) failed: '
              f'{ex!r}', file=sys.stderr, flush=True)
        _provider_cls = None
    return _provider_cls


def notify_project_changed(langcode):
    """Push-wake any peer observing this project's status URI.

    Safe to call from any thread. Cheap (~one jnius hop + one
    ContentResolver.notifyChange); the actual observer dispatch
    happens async on the system process side, so this returns
    immediately without waiting for peer callbacks to run.

    A no-op when *langcode* is falsy or we're off Android."""
    if not langcode:
        return
    cls = _get_provider_class()
    if cls is None:
        return
    try:
        cls.notifyStatusChanged(str(langcode))
        # Confirmation line: lets a tester grep daemon logs for
        # ``[notify]`` and see whether the fire path is reaching
        # the Java side at the expected sites. Cheap (one print
        # per HEAD-advance event) — keep enabled in production.
        print(f'[notify] notifyStatusChanged({langcode!r}) fired',
              file=sys.stderr, flush=True)
    except Exception as ex:
        print(f'[notify] notifyStatusChanged({langcode!r}) raised: '
              f'{ex!r}', file=sys.stderr, flush=True)


def notify_global_changed():
    """Push-wake any peer observing the parent status URI with
    descendants-mode subscription. Use for daemon-wide events
    (work_offline toggle, lan_allow_sync toggle, peer-list
    mutations) that affect every project's rendered state, not
    just one langcode's.

    No-op off Android."""
    cls = _get_provider_class()
    if cls is None:
        return
    try:
        cls.notifyStatusChanged('')
        print(f'[notify] notifyStatusChanged(global) fired',
              file=sys.stderr, flush=True)
    except Exception as ex:
        print(f'[notify] notifyStatusChanged(global) raised: '
              f'{ex!r}', file=sys.stderr, flush=True)
