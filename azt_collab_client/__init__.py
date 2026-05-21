"""
azt_collab_client — thin client library for azt_collabd.

Ops that go through the server return a ``Result`` (structured status
codes + params); the caller calls ``translate_result(result)`` for
display. ``Result.has(S.PUSHED)`` etc. is the way to drive business
logic — no more substring matching on log strings.
"""

__version__ = "0.44.12"
# Floor on the azt_collabd version this client is willing to talk
# to. ``check_server_compat()`` returns ``server_too_old`` when the
# running daemon is below this; peer apps surface that to the user
# as "please update the AZT collaboration service."
#
# 0.16.0 floor: the daemon now persists scheduler jobs across kills
# (jobs.json + reconcile_on_startup). Pre-0.16 daemons forget
# job_ids on respawn, so poll_job returns None and the peer can't
# distinguish "never existed" from "interrupted."
#
# 0.25.0 floor: synchronized release. The daemon now stamps
# `last_project` on every langcode-bound RPC and exposes new
# endpoints (`/v1/recent/last_project`, `/v1/credentials/gitlab/test`)
# that this client uses; new dataclass fields (`Project.last_commit`,
# `ProjectStatus.commits_ahead`) require the daemon to populate them;
# `_h_init_project` writes `remote_url` / `last_sync` / `last_commit`
# back to projects.json; the post-push remote-mirror update keeps
# `commits_ahead` honest; `_resolve_path` consults
# `projects.json::working_dir` instead of assuming dirname ==
# langcode. A 0.25 client against a pre-0.25 daemon would silently
# lose all of these. Lock-step bump intended to flush every peer APK
# through a rebuild.
# 0.30.28 floor: deliberate bump (no underlying wire-format
# requirement) to keep exercising the ``server_too_old`` bootstrap
# path against the latest peer build. Drop back to a real-world
# floor before any release that ships in the public update
# channel.
#
# 0.36.0 floor: HARD requirement. 0.36.0 daemons expose
# ``POST /v1/projects/<lang>/atomic_commit`` so peers writing
# LIFT / audio / image bytes through a ``content://`` URI on
# Android can hand the full payload to the daemon, which then
# performs the tempfile + ``os.replace`` atomic write in its own
# process — serialized via ``project_lock`` against the daemon's
# own merge-output writes. Pre-0.36.0 daemons have no atomic
# write for URI peers; ``LiftHandle.atomic_open_write`` on a URI
# falls back to the lock-only ``open_write``, which prevents
# same-peer-process races but NOT peer-vs-daemon nor peer-vs-other-
# peer-process races. The malformed-XML ``baf`` repro
# (NOTES_TO_DAEMON.md 2026-05-12, closed) is the canonical
# example: two same-process writes interleaved through the
# ContentProvider FD path produced two same-lang gloss elements
# with overlapping text, which the daemon's merge then misparsed
# catastrophically. The 0.36.0 client uses the new RPC for URI
# atomic writes, closing that gap; pinning the floor here forces
# every peer talking to a 0.36.0+ daemon to be running a client
# that knows about the RPC (older daemons reject the endpoint).
#
# 0.35.4 floor: HARD requirement. 0.35.4 daemons write a
# forensic ``<azt-collab-diagnostic>`` XML file under the
# project's ``.azt-collab/diagnostics/`` whenever any input or
# output guard fires, and stage it into the merge commit so the
# data is retrievable from any clone. Pre-0.35.4 daemons log
# the guard trip to stderr only — Android logcat is ephemeral
# and not retrievable post-hoc, so a guard trip with a
# pre-0.35.4 daemon leaves no audit trail. The user explicitly
# asked (2026-05-12) that every guard firing be recoverable
# from the repo so a future analysis can reconstruct what
# happened; pinning the floor here is the discipline that
# enforces it.
#
# 0.35.3 floor: HARD requirement. The reopened ``baf`` field
# repro (NOTES_TO_DAEMON.md, 2026-05-12 second cut) showed that
# the 0.35.1 input-side truncation guard was insufficient: a
# merge produced 1 entry from two healthy 1700-entry inputs.
# 0.35.3 adds an output-side ``_looks_catastrophic_output``
# guard that refuses to commit a merge result whose entry count
# is < 1/4 of the smaller healthy input. Defense-in-depth
# regardless of upstream cause. Forcing the floor here ensures
# no peer talks to a daemon that lacks the output guard, since
# the proximate-cause analysis for the original baf collapse is
# undetermined and the same bug shape could recur.
#
# 0.35.1 floor: HARD requirement. Pre-0.35.1 daemons have no
# truncation guard in ``lift_merge.three_way_merge``; if one side
# arrives at the merge with a near-empty entry list (peer-side
# write race / partial commit / sandbox hiccup — upstream cause
# not yet narrowed), the merge correctly honors the apparent
# deletions and produces a 1-entry destructive result, which the
# daemon then commits and pushes before any peer can notice.
# Field-reported 2026-05-12 against the ``baf`` project: 1701
# entries reduced to 1, project unrecoverable via the normal
# clone flow. The 0.35.1 daemon refuses the destructive merge
# (keeps the larger side intact, surfaces a
# ``truncation-suspected`` Conflict). Forcing the floor blocks
# the entire sync flow until the user updates the server APK —
# preferable to letting a pre-fix daemon irreversibly wipe a
# project's contents.
#
# 0.35.0 floor: SOFT requirement — the wire format hasn't changed
# (older daemons just don't emit ``AUTH_REFRESH_STALE``; older
# clients fall back to the verbose ``[CODE] {...}`` translate
# render on unknown codes). Raised anyway to flush peer rebuilds
# through the new auto/user sync contract documented in
# ``CLAUDE.md`` § "Peer contract: routing on sync results".
# Without that contract, the recorder (and any future peer)
# disrupts mid-flow on ``NOT_A_REPO`` / ``NO_REMOTE`` /
# ``SERVER_UNAVAILABLE`` etc. during auto-sync — the same
# symptom that surfaced as the "selected B got A" picker
# complaint earlier in this session. Forcing the floor ensures
# every peer that talks to a 0.35.0+ daemon has been rebuilt
# against the new client, where the contract is loud in
# CLAUDE.md and the AUTH_REFRESH_STALE handler is wired through
# translate.py. Also bundles the deadline-aware re-auth toast
# so users see the 8h-cliff warning before they hit it.
#
# 0.34.1 floor: HARD requirement on top of the 0.34.0 sync fixes.
# Pre-0.34.1 ``lift_merge.three_way_merge`` walks
# ``sorted(all_guids)``, which rewrites every LIFT file in
# guid-alphabetical order on the first real merge. The damage is
# committed to git history before any peer can notice (the picker
# / recorder never sees the disk state before the merge commit
# pushes), so silent fallback against a pre-0.34.1 daemon
# guarantees one-shot, irreversible loss of document order for the
# project. Forcing the floor blocks the merge entirely until the
# user updates the server APK — preferable to scrambling and
# pushing the result. See NOTES_TO_DAEMON.md (closed 2026-05-11)
# for the field repro against ``kent-rasmussen/sw-US-x-kent``.
#
# 0.34.0 floor: HARD requirement, not a deliberate-test bump. Three
# load-bearing sync fixes only land in 0.34.0: (1) ``_merge_diverged``
# now uses ``worktree.commit(merge_heads=…)`` to atomically write
# the second parent — pre-0.34 graft fallback silently produced
# merge commits with only the local parent, every push then
# rejected with ``DivergedBranches``; (2) HTTP 403 detection moved
# from ``'403' in str(exc)`` substring matching (false-positives on
# the trigraph inside hex SHAs, observed in the field) to
# ``\b403\b`` word-boundary regex + ``diagnose_403`` now scopes
# ``check_app_installed`` to the repo owner so multi-org users
# don't get a bogus ``REPO_NOT_AUTHORIZED`` against an unrelated
# org's install; (3) ``porcelain.fetch`` / ``pull`` now pass the
# remote NAME (``'origin'``) not the URL, so dulwich's
# ``_import_remote_refs`` actually fires and
# ``refs/remotes/origin/<branch>`` advances on each fetch —
# pre-0.34 the local tracking ref was frozen at clone time and
# every sync acted on a phantom remote state. A peer paired with
# a pre-0.34 daemon will lose two-device sync silently after the
# first race; the floor forces the user to update the server APK
# before the peer will attempt a sync at all.
# 0.43.0 floor: HARD requirement. 0.43.0 daemons expose the new
# ``commit_project`` (commit-only) RPC and own all push timing via
# the scheduler's drain loop. Pre-0.43 daemons skip the commit
# step entirely while offline (the bug filed in NOTES_TO_DAEMON.md
# 2026-05-15) — a 0.43 peer running against a pre-0.43 daemon
# would still lose offline commits. Also adds the
# ``sync.work_offline`` toggle and ``S.WORK_OFFLINE_ENABLED``
# status code; a 0.43 client paired with an older daemon would
# render the work-offline UI but the toggle would have no effect.
MIN_SERVER_VERSION = "0.44.0"
# 0.41.24 floor: deliberate bump, test scaffolding to force the
# bootstrap install/update popup to fire when one side is rebuilt
# and the other isn't. Set to the current ``azt_collabd.__version__``
# so a peer rebuilt at 0.41.24 calling a server still at 0.41.23
# (or earlier) trips ``server_too_old`` in ``check_server_compat``
# and ``install_server_apk_popup`` renders the "Update {name}?"
# flavour. Drop back to a real-world floor (matching what we
# actually require for correctness) before any release that ships
# in the public update channel.
# Public release page for the server APK. Tapping "Open install
# page" in ``install_server_apk_popup`` opens this URL in the
# browser so the user can read release notes / browse the project
# before downloading. The actual download is then a one-tap step on
# the page (or a separate "Install" button in the popup that does
# the in-app download via the GitHub API + Android system
# installer; that path uses ``asset['browser_download_url']`` from
# the release JSON, not this constant).
SERVER_APK_INSTALL_URL = (
    'https://github.com/kent-rasmussen/azt-collab/releases/latest'
)
# Maintainer contact for the suite. Surfaced from
# ``_show_release_too_old`` (and other in-app "this needs human
# attention" surfaces) as a mailto: link so users can email when
# the release feed can't satisfy their version requirement, or
# when they want to report a problem they hit in production.
# Forks should override this in their own build of the client
# (no env-var hook yet — change here, rebuild the peer APK).
MAINTAINER_EMAIL = 'kent_rasmussen@sil.org'

import base64
from . import status as S
from .status import Status, Result
from .projects import Project, ProjectStatus
from .translate import translate_status, translate_result, set_translator
from .rpc import call, health, ServerUnavailable
from .lift_io import (
    LiftHandle, MediaHandle, CAWLHandle,
    audio_uri_for, image_uri_for, is_content_uri,
)
from .recent import last_project, set_last_project
from .peer_prefs import peer_pref, set_peer_pref


def configure(app_id: str):
    """Reserved for later migration steps (app identity for logging /
    provider routing). Currently a no-op."""
    return None


def open_server_ui(on_status=None):
    """Open the daemon settings UI on whichever platform we're on.

    Desktop: spawns ``python -m azt_collabd ui`` detached and returns
    ``{'ok': True, 'pid': <int>}``.

    Android: dispatches a launch intent to the installed server APK
    (``org.atoznback.aztcollab``). On success returns
    ``{'ok': True, 'launched': 'android-apk'}``. If the APK isn't
    installed, opens an install-prompt popup
    (``ui.popups.install_server_apk_popup``) and returns
    ``{'ok': False, 'error': 'server_apk_not_installed', 'prompted': True}``.

    ``on_status`` is forwarded to the install popup so the host can
    surface "could not open install page" errors in its status bar.

    Sister apps should bind their "Open Sync Settings" button to this
    helper so the platform branching lives in one place::

        from azt_collab_client import open_server_ui
        result = open_server_ui(on_status=self._set_log)
        if not result['ok'] and not result.get('prompted'):
            self._set_log(result['error'])
    """
    try:
        from kivy.utils import platform
    except Exception:
        platform = ''
    if platform == 'android':
        return _open_server_ui_android(on_status)
    import os
    import subprocess
    import sys as _sys
    import time as _time
    from ._spawn import build_spawn_env
    try:
        proc = subprocess.Popen(
            [_sys.executable, '-m', 'azt_collabd', 'ui'],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=hasattr(os, 'setsid'),
            env=build_spawn_env(),
        )
    except OSError as ex:
        return {'ok': False, 'error': f'spawn_failed: {ex}'}
    deadline = _time.time() + 0.25
    while _time.time() < deadline:
        rc = proc.poll()
        if rc is not None:
            try:
                err = proc.stderr.read() if proc.stderr else b''
            except Exception:
                err = b''
            detail = err.decode('utf-8', 'replace').strip()[:200]
            return {'ok': False, 'error': 'spawn_exited',
                    'returncode': rc, 'detail': detail}
        _time.sleep(0.02)
    return {'ok': True, 'pid': proc.pid}


def _open_server_ui_android(on_status):
    try:
        from jnius import autoclass, cast
    except Exception as ex:
        return {'ok': False, 'error': 'launch_failed',
                'detail': f'jnius unavailable: {ex}'}
    try:
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        Intent = autoclass('android.content.Intent')
        ctx = cast('android.content.Context', PythonActivity.mActivity)
        pm = ctx.getPackageManager()
        intent = pm.getLaunchIntentForPackage('org.atoznback.aztcollab')
    except Exception as ex:
        return {'ok': False, 'error': 'launch_failed',
                'detail': f'{type(ex).__name__}: {ex}'}
    if intent is None:
        try:
            from .ui.popups import install_server_apk_popup
            install_server_apk_popup(on_status=on_status)
        except Exception as ex:
            return {'ok': False, 'error': 'server_apk_not_installed',
                    'prompted': False,
                    'detail': f'install popup failed: {ex}'}
        return {'ok': False, 'error': 'server_apk_not_installed',
                'prompted': True}
    try:
        # Tag the launch so the server APK can distinguish "peer
        # opened me to expose settings" from "user tapped my
        # launcher icon" — the former gets an unobtrusive
        # update-available badge (peer already runs its own boot
        # update flow against the server), the latter gets a
        # popup-on-boot prompt (the user-direct launch path
        # otherwise has no occasion to learn about updates).
        intent.putExtra('azt_launch_source', 'peer')
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        ctx.startActivity(intent)
    except Exception as ex:
        return {'ok': False, 'error': 'launch_failed',
                'detail': f'{type(ex).__name__}: {ex}'}
    return {'ok': True, 'launched': 'android-apk'}


_AZT_PICK_REQ_CODE = 0x4747  # arbitrary; uniquely ours within the recorder


def pick_project(timeout_seconds=None):
    """Launch the project-picker helper and return the selected
    project. Blocks until the picker window closes.

    Desktop: spawns ``python -m azt_collabd projects`` as a subprocess
    and parses ``AZT_PICK\\t<path>`` from its stdout.

    Android: dispatches an Intent to the standalone server APK's
    PickerActivity and waits on ``onActivityResult`` for the chosen
    path. Requires the server APK to be installed; if it isn't,
    returns ``{'ok': False, 'error': 'server_apk_not_installed'}``.

    Returns one of:
        {'ok': True, 'path': '/abs/path/to/file.lift'}
        {'ok': False, 'error': 'cancelled'}
        {'ok': False, 'error': 'spawn_exited',
         'returncode': N, 'detail': '...'}
        {'ok': False, 'error': 'spawn_failed',
         'detail': '...'}
        {'ok': False, 'error': 'server_apk_not_installed'}
        {'ok': False, 'error': 'timeout'}
    """
    try:
        from kivy.utils import platform
    except Exception:
        platform = ''
    if platform == 'android':
        return _pick_project_android(timeout_seconds)
    return _pick_project_desktop(timeout_seconds)


def _pick_project_desktop(timeout_seconds):
    import os
    import subprocess
    import sys as _sys
    from ._spawn import build_spawn_env
    try:
        proc = subprocess.Popen(
            [_sys.executable, '-m', 'azt_collabd', 'projects'],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=hasattr(os, 'setsid'),
            env=build_spawn_env(),
        )
    except OSError as ex:
        return {'ok': False, 'error': 'spawn_failed', 'detail': str(ex)}
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return {'ok': False, 'error': 'timeout'}
    rc = proc.returncode
    out = (stdout or b'').decode('utf-8', 'replace')
    for line in out.splitlines():
        if line.startswith('AZT_PICK\t'):
            parts = line.split('\t')
            path = parts[1].strip() if len(parts) > 1 else ''
            langcode = parts[2].strip() if len(parts) > 2 else ''
            if path:
                return {'ok': True, 'path': path, 'langcode': langcode}
    if rc == 0:
        # Process exited 0 but no AZT_PICK line — treat as cancelled.
        return {'ok': False, 'error': 'cancelled'}
    if rc == 1:
        return {'ok': False, 'error': 'cancelled'}
    err = (stderr or b'').decode('utf-8', 'replace').strip()[:200]
    return {'ok': False, 'error': 'spawn_exited',
            'returncode': rc, 'detail': err}


def _pick_project_android(timeout_seconds):
    """Launch the picker Activity in the server APK and wait for its
    result.

    Wraps a single launch+wait pass in a tiny retry loop so the
    "RESULT_CANCELED with non-null data" anomaly auto-recovers: that
    state shouldn't be reachable through a normal pick flow (the
    picker's ``_emit_and_quit`` either sets RESULT_OK with data or
    ``on_request_close`` sets RESULT_CANCELED with no data). Android
    can synthesize it when the user back-presses during ``setResult``,
    or when an OEM launcher tampers with the result Intent. Either
    way, silently swallowing it as a clean cancel leaves the user on
    a recorder window with no project loaded; re-launching the picker
    once gives them another chance to choose."""
    import threading
    try:
        from jnius import autoclass
        from android import activity as android_activity  # noqa: F401
        from kivy.clock import Clock
    except Exception as ex:
        return {'ok': False, 'error': 'spawn_failed', 'detail': str(ex)}

    last_result = None
    for attempt in range(2):
        last_result = _pick_project_android_once(
            timeout_seconds, autoclass, android_activity, Clock,
            attempt=attempt)
        if last_result.get('error') != 'unexpected_cancel':
            break
        # else: anomaly — fall through to one retry attempt.
    if last_result and last_result.get('error') == 'unexpected_cancel':
        # Both attempts hit the RESULT_CANCELED-with-data anomaly. We
        # don't have a recovery left — leaving the recorder running on
        # an empty window is the worst UX (user sees no project, no
        # explanation). Show a single-button modal so the user knows
        # *why* the app is closing, then stop the host App on confirm.
        _show_picker_failure_and_exit(Clock)
    return last_result or {'ok': False, 'error': 'cancelled'}


def _show_picker_failure_and_exit(Clock):
    """Schedule a Kivy modal on the UI thread that informs the user the
    picker failed and exits the host App when they tap OK. Called from
    the worker thread after both ``_pick_project_android_once``
    attempts return ``'unexpected_cancel'``. Returns immediately; the
    actual stop happens when the user taps the button.

    Lives in the client (not the recorder) so every peer that goes
    through ``pick_project()`` gets the same fallback without each
    host having to wire its own ``_handle_pick`` branch for
    ``unexpected_cancel``."""
    import sys as _sys

    def _show(*_):
        try:
            from kivy.app import App
            from kivy.uix.modalview import ModalView
            from kivy.uix.boxlayout import BoxLayout
            from kivy.uix.label import Label
            from kivy.uix.button import Button
            from kivy.metrics import dp, sp
            from .translate import tr as _tr
        except Exception as ex:
            print(f'[pick_project] _show_picker_failure_and_exit: '
                  f'kivy unavailable, exiting silently: {ex}',
                  file=_sys.stderr, flush=True)
            try:
                import os as _os
                _os._exit(1)
            except Exception:
                pass
            return

        view = ModalView(size_hint=(0.85, None), height=dp(220),
                         auto_dismiss=False)
        box = BoxLayout(orientation='vertical', padding=dp(16),
                        spacing=dp(12))
        msg = Label(
            text=_tr('The project picker failed to return a result. '
                     'The app will now close — please reopen it.'),
            halign='center', valign='middle')
        msg.bind(size=lambda w, s: setattr(w, 'text_size', s))
        box.add_widget(msg)
        btn = Button(text=_tr('OK'), size_hint_y=None, height=dp(48),
                     font_size=sp(16), bold=True)

        def _exit(*_a):
            try:
                view.dismiss()
            except Exception:
                pass
            try:
                app = App.get_running_app()
                if app is not None:
                    app.stop()
            except Exception:
                pass
            # Belt and braces: if Kivy didn't actually stop (the host
            # may have on_stop hooks that swallow), force-exit so the
            # user isn't left in the broken state.
            try:
                import os as _os
                _os._exit(0)
            except Exception:
                pass

        btn.bind(on_release=_exit)
        box.add_widget(btn)
        view.add_widget(box)
        view.open()

    print('[pick_project] picker anomaly persisted across retry; '
          'scheduling failure modal',
          file=_sys.stderr, flush=True)
    Clock.schedule_once(_show, 0)


def _pick_project_android_once(timeout_seconds, autoclass,
                               android_activity, Clock, attempt=0):
    import threading
    import sys as _sys

    done = threading.Event()
    holder = {'result': None}
    # Track whether ``android_activity.bind`` actually fired so the
    # cleanup path doesn't try to unbind a handler that was never
    # registered (early failure in ``_setup_on_ui``).
    bind_state = {'bound': False}

    def _unbind_handler():
        """Drop ``_on_result`` from the global activity-result
        dispatch list. Without this each ``pick_project()`` invocation
        leaves a dangling handler bound for the lifetime of the host
        process, so subsequent picks fire 2× / 3× / N×; each closure
        writes to its own (long-since-stale) ``holder``, but the JNI
        cost grows linearly with picks. Best-effort: a missing
        ``unbind`` symbol on older Kivy/python-for-android builds
        falls through silently."""
        if not bind_state['bound']:
            return
        bind_state['bound'] = False
        try:
            android_activity.unbind(on_activity_result=_on_result)
        except Exception:
            # Old Kivy versions exposed bind without unbind; tolerate.
            pass

    def _on_result(request_code, result_code, data):
        if request_code != _AZT_PICK_REQ_CODE:
            print(f'[pick_project] _on_result: ignoring foreign '
                  f'request_code={request_code} (ours={_AZT_PICK_REQ_CODE})',
                  file=_sys.stderr, flush=True)
            return
        # Diagnostic: log the raw result so a ``no_path`` mystery
        # can be pinpointed (RESULT_OK with empty extra vs. caller
        # set CANCELED vs. data is None vs. extras missing). The
        # picker side has corresponding prints; together they narrow
        # the empty-path origin to one of three regions.
        print(f'[pick_project] _on_result: result_code={result_code} '
              f'data_present={data is not None} attempt={attempt}',
              file=_sys.stderr, flush=True)
        if result_code == -1 and data is not None:  # RESULT_OK
            try:
                path = data.getStringExtra('path') or ''
                langcode = data.getStringExtra('langcode') or ''
            except Exception as _ex:
                print(f'[pick_project] _on_result: getStringExtra raised: '
                      f'{_ex!r}', file=_sys.stderr, flush=True)
                path = ''
                langcode = ''
            print(f'[pick_project] _on_result: path={path!r} '
                  f'langcode={langcode!r}',
                  file=_sys.stderr, flush=True)
            holder['result'] = ({'ok': True, 'path': path,
                                 'langcode': langcode} if path
                                else {'ok': False, 'error': 'no_path'})
        elif data is not None:
            # Anomaly: RESULT_CANCELED (or any non-OK code) with data
            # attached. The picker contract is RESULT_OK→data /
            # RESULT_CANCELED→no-data; this combination shouldn't be
            # reachable normally. Don't silently swallow as 'cancelled'
            # — surface a distinct code so the outer loop can retry
            # the picker once before giving up.
            print(f'[pick_project] _on_result: anomaly — non-OK with '
                  f'data; will retry the picker',
                  file=_sys.stderr, flush=True)
            holder['result'] = {'ok': False, 'error': 'unexpected_cancel'}
        else:
            holder['result'] = {'ok': False, 'error': 'cancelled'}
        done.set()
        # Single-shot: drop ourselves from the global dispatch list so
        # the next ``pick_project()`` doesn't fire two of us, three
        # of us, etc.
        _unbind_handler()

    # JNI proxy creation (android_activity.bind) needs the app
    # ClassLoader to resolve PythonActivity's inner-class interfaces
    # (ActivityResultListener). Worker threads attached by jnius'
    # thread hook don't carry that ClassLoader, so doing the bind on
    # one fails with ClassNotFoundException. Dispatch the JNI work to
    # the Kivy main thread (which is the Android UI thread and has
    # the right ClassLoader); only the wait stays on the caller.
    setup_done = threading.Event()
    setup = {'ok': True}

    def _setup_on_ui(*_):
        try:
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            Intent = autoclass('android.content.Intent')
            ComponentName = autoclass('android.content.ComponentName')
            intent = Intent('org.atoznback.aztcollab.PICK_PROJECT')
            # Setting the component explicitly ensures the suite-signed
            # server APK is the resolver (rather than any handler that
            # might claim the action), and gives a clean
            # ActivityNotFoundException when the APK isn't installed.
            try:
                intent.setComponent(ComponentName(
                    'org.atoznback.aztcollab',
                    'org.kivy.android.PythonActivity'))
            except Exception:
                pass
            activity = PythonActivity.mActivity
            # Pre-check resolvability — some OEM builds silently no-op
            # startActivityForResult instead of throwing, which would
            # wedge done.wait() forever.
            try:
                pm = activity.getPackageManager()
                ri = pm.resolveActivity(intent, 0)
                if ri is None:
                    setup['ok'] = False
                    setup['error'] = 'server_apk_not_installed'
                    return
            except Exception:
                pass
            android_activity.bind(on_activity_result=_on_result)
            bind_state['bound'] = True
            activity.startActivityForResult(intent, _AZT_PICK_REQ_CODE)
        except Exception as ex:
            msg = str(ex)
            setup['ok'] = False
            if 'ActivityNotFound' in msg or 'No Activity' in msg:
                setup['error'] = 'server_apk_not_installed'
            else:
                setup['error'] = 'spawn_failed'
                setup['detail'] = msg
        finally:
            setup_done.set()

    Clock.schedule_once(_setup_on_ui, 0)
    if not setup_done.wait(timeout=10):
        return {'ok': False, 'error': 'spawn_failed',
                'detail': 'ui-thread setup wedged'}
    if not setup.get('ok'):
        out = {'ok': False, 'error': setup['error']}
        if 'detail' in setup:
            out['detail'] = setup['detail']
        return out

    # Cap the wait so a launched-but-never-returns Activity can't
    # wedge the recorder forever. 10 minutes is a generous default
    # for picking a project; callers can pass a smaller timeout.
    wait_for = timeout_seconds if timeout_seconds is not None else 600
    if not done.wait(timeout=wait_for):
        # Timed out without an activity result. Drop the handler so
        # a much-later result on the wrong code path doesn't write
        # to a stale ``holder``.
        _unbind_handler()
        return {'ok': False, 'error': 'timeout'}
    return holder['result'] or {'ok': False, 'error': 'cancelled'}


def is_online():
    """Ask the server whether it has internet access."""
    try:
        resp = call('GET', '/v1/online')
    except ServerUnavailable:
        return False
    return bool(resp.get('online'))


def _version_tuple(s):
    """Best-effort 'X.Y.Z' → (X, Y, Z). Pads with zeros, ignores trailing
    pre-release tags. Wrong only on absurd inputs and we'd surface the
    server as too old in that case, which is the safer side.

    Rules:

    - Splits on ``.`` AND ``-`` so date-tagged forms like
      ``2026-05-06`` decompose to ``(2026, 5, 6)`` rather than
      collapsing to ``(2026, 0, 0)``.
    - **First chunk** uses leading-digits-only: a ``v`` /
      ``V`` prefix on the first chunk yields ``0`` so a
      caller that forgot to ``.lstrip('vV')`` surfaces as
      "too old" instead of accidentally matching. Pure-text
      first chunks (``'garbage'``) also yield ``0``.
    - **Later chunks** concatenate every digit character in
      the chunk: ``'rc1'`` → ``1``, ``'05'`` → ``5``,
      ``'beta12'`` → ``12``. This lets pre-release-style
      suffixes still contribute a usable ordinal in the
      tuple without forcing the caller to normalise them.
      Pure-text later chunks (``'final'``, ``'rc'``) still
      yield ``0``.
    """
    if not s:
        return (0, 0, 0)
    import re
    chunks = re.split(r'[.\-]', str(s))
    out = []
    for i, chunk in enumerate(chunks):
        if i == 0:
            # First chunk: leading digits only. 'v1' → 0 so a
            # caller that forgot to strip 'v' surfaces as
            # version 0 (too old) rather than silently matching.
            digits = ''
            for ch in chunk:
                if ch.isdigit():
                    digits += ch
                else:
                    break
        else:
            # Later chunks: every digit character contributes.
            digits = ''.join(c for c in chunk if c.isdigit())
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def check_server_compat():
    """One-shot version handshake. Returns one of:

      ``{'ok': True, 'server_version': '0.7.0'}``
          server reachable; both directions of the version check pass.

      ``{'ok': False, 'error': 'server_too_old',
         'server_version': '0.5.0', 'min_required': '0.6.0'}``
          server reachable but older than this client supports;
          peer should surface "Please update the AZT Collaboration
          service" to the user.

      ``{'ok': False, 'error': 'client_too_old',
         'client_version': '0.13.6', 'server_version': '0.12.0',
         'min_required': '0.14.0'}``
          server reachable, but it requires a newer client than this
          peer ships. Peer should surface "Please update this app".
          Symmetric to ``server_too_old`` so peer apps can branch on
          the same shape.

      ``{'ok': False, 'error': 'server_unreachable'}``
          health probe failed; peer may retry or fall back to
          showing an install prompt.

    Sister apps should call this once at startup; the result is the
    decision-making input for the install / update UX. Subsequent
    rpc calls do not re-check (compatibility doesn't drift mid-run)."""
    try:
        resp = call('GET', '/v1/health', timeout=5)
    except ServerUnavailable as ex:
        # Surface the transport's coarse failure ``kind`` so
        # bootstrap can pick fail-fast vs keep-retrying without
        # parsing the message. See ``transports.ServerUnavailable``
        # for the recognised values.
        return {'ok': False, 'error': 'server_unreachable',
                'detail': str(ex),
                'kind': getattr(ex, 'kind', '') or ''}
    server_version = str(resp.get('version', ''))
    if (_version_tuple(server_version)
            < _version_tuple(MIN_SERVER_VERSION)):
        return {'ok': False, 'error': 'server_too_old',
                'server_version': server_version,
                'min_required': MIN_SERVER_VERSION}
    # Server publishes the minimum client it's willing to talk to. Old
    # daemons that don't include the field are treated as "no floor",
    # so this check is forward-compatible with pre-0.12.0 servers.
    min_client = str(resp.get('min_client_version', '') or '')
    if min_client and (_version_tuple(__version__)
                       < _version_tuple(min_client)):
        return {'ok': False, 'error': 'client_too_old',
                'client_version': __version__,
                'server_version': server_version,
                'min_required': min_client}
    return {'ok': True, 'server_version': server_version}


# ── Credentials API (server-owned credentials.json) ────────────────────────

def get_credentials_status():
    """Return a dict describing what's configured:
        {host, github: {connected, username, app_installed},
         gitlab: {connected, username}}
    Never contains raw tokens. On transport failure returns an empty
    status so the UI degrades gracefully."""
    try:
        resp = call('GET', '/v1/credentials/status')
    except ServerUnavailable:
        return {'host': 'github',
                'github': {'connected': False, 'username': '',
                           'app_installed': False},
                'gitlab': {'connected': False, 'username': ''}}
    if resp.get('ok'):
        return {k: v for k, v in resp.items() if k != 'ok'}
    return {}


def set_collab_host(host):
    """Persist the user's host selection (github|gitlab)."""
    try:
        call('POST', '/v1/credentials/host', {'host': host})
    except ServerUnavailable:
        pass


# ── Contributor (commit author display name) ───────────────────────────────
#
# Server-owned: stored in ``$AZT_HOME/config.json :: collab.contributor``.
# Sync / init endpoints fall back to this value when the peer passes an
# empty ``contributor``. So peers that read this once can stop carrying
# their own "Your name" preference; the suite has one source of truth.

def get_contributor():
    """Return the user's display name (commit author) stored on the
    server. Empty string if unset or unreachable."""
    try:
        resp = call('GET', '/v1/config/contributor')
    except ServerUnavailable:
        return ''
    if not resp.get('ok'):
        return ''
    return str(resp.get('contributor', ''))


def set_contributor(name):
    """Persist the user's display name on the server. Best-effort:
    silently no-ops on transport failure.

    The display name is the **commit author** for every git op
    the daemon runs. As of 0.40.0 peers no longer pass it
    per-call — the daemon reads from store directly. If the
    stored name is empty, commit-issuing endpoints refuse with
    ``S.CONTRIBUTOR_UNSET``."""
    try:
        call('POST', '/v1/config/contributor', {'contributor': name})
    except ServerUnavailable:
        pass


def get_device_name():
    """Return the daemon's stored device-name label. Auto-populates
    from the OS on first read (Android: ``Settings.Global.DEVICE_NAME``
    → ``Build.MANUFACTURER + MODEL``; desktop: ``socket.gethostname()``),
    so a non-empty string comes back on a fresh install.

    The label disambiguates the git commit author's email slot
    (``<contributor>@<device_name>``) when the same human commits
    from multiple devices. Peers display it in the daemon settings
    UI alongside the contributor name; both are user-editable.

    Empty string on transport failure."""
    try:
        resp = call('GET', '/v1/config/device_name')
    except ServerUnavailable:
        return ''
    if not resp.get('ok'):
        return ''
    return str(resp.get('device_name', '') or '')


def set_device_name(name):
    """Persist the user's device-name label. Empty string clears
    the override, causing the daemon to re-detect from the OS on
    next read. Best-effort: silently no-ops on transport failure."""
    try:
        call('POST', '/v1/config/device_name', {'device_name': name})
    except ServerUnavailable:
        pass


def get_cawl_prefetch_all_variants():
    """Read the daemon's CAWL prefetch policy.

    Returns False (default) when the daemon warms one image
    per CAWL id (the file whose basename contains ``__``).
    Returns True when the daemon warms every image-shaped
    index entry. False on RPC failure — matches the default
    so peers reading this for display don't flip-flop on a
    transient error."""
    try:
        resp = call('GET', '/v1/config/cawl_prefetch_all_variants')
    except ServerUnavailable:
        return False
    if not resp.get('ok'):
        return False
    return bool(resp.get('enabled', False))


def set_cawl_prefetch_all_variants(enabled):
    """Persist the daemon's CAWL prefetch policy. The change
    takes effect on the next ``auto_prefetch`` trigger (next
    project-load, scheduler-edge retry, etc.) — flipping
    doesn't retroactively re-warm an in-flight worker.
    Best-effort: silently no-ops on transport failure."""
    try:
        call('POST', '/v1/config/cawl_prefetch_all_variants',
             {'enabled': bool(enabled)})
    except ServerUnavailable:
        pass


def get_server_ui_language():
    """Return the daemon-side persisted UI language (BCP-47 code,
    e.g. ``'fr'``) or ``''`` on RPC failure.

    Lets peers mirror the language choice picked in the server
    APK's settings UI. On Android, ``$AZT_HOME`` is per-process
    private (server's filesDir vs. peer's filesDir), so the
    language preference doesn't propagate via the file system —
    each peer has to ask the daemon. Bootstrap calls this on
    entry; if the value is non-empty and differs from the peer's
    local pref, it applies via ``i18n.set_language`` so all peer
    UI (popups, status text, KV strings) tracks the daemon's
    choice."""
    try:
        resp = call('GET', '/v1/config/ui_language', timeout=5)
    except ServerUnavailable:
        return ''
    if not resp.get('ok'):
        return ''
    return str(resp.get('language', '') or '')


def github_app_install_url():
    """Return the configured GitHub App install URL (string) or '' if the
    server is unreachable / the App identity isn't configured. The URL
    derives from the daemon's ``azt_collabd.config`` (which the server
    APK populates at startup), so peers don't have to hard-code it."""
    try:
        resp = call('GET', '/v1/credentials/github/install_url')
    except ServerUnavailable:
        return ''
    if not resp.get('ok'):
        return ''
    return str(resp.get('url', ''))


def github_app_client_id():
    """Return the configured GitHub App client_id, or '' if unavailable.
    Peers used to read this directly from ``azt_collabd.auth``; now they
    ask the server, which holds the canonical value."""
    try:
        resp = call('GET', '/v1/credentials/github/client_id')
    except ServerUnavailable:
        return ''
    if not resp.get('ok'):
        return ''
    return str(resp.get('client_id', ''))


def github_device_flow_start():
    """Kick off a GitHub App device flow on the server. Returns
    ``{ok, job_id, user_code, verification_uri, interval, expires_in}``
    on success, or ``{ok: False, error}`` on failure. The server polls
    GitHub on its own; the peer just polls
    ``github_device_flow_status(job_id)`` until DONE / FAILED."""
    try:
        resp = call('POST',
                    '/v1/credentials/github/device_flow/start', {})
    except ServerUnavailable as ex:
        return {'ok': False, 'error': f'server_unavailable: {ex}'}
    return resp


def github_device_flow_status(job_id):
    """Poll a device flow job. Returns
    ``{ok, state, username, app_installed, error, error_params}``.
    State is one of ``'POLLING' | 'DONE' | 'FAILED'``."""
    try:
        resp = call(
            'GET', f'/v1/credentials/github/device_flow/{job_id}')
    except ServerUnavailable as ex:
        return {'ok': False, 'error': f'server_unavailable: {ex}'}
    return resp


def save_github_tokens(token_data, username=''):
    """Persist a device-flow token response + (optional) username."""
    call('POST', '/v1/credentials/github/tokens', {
        'access_token': token_data.get('access_token', ''),
        'refresh_token': token_data.get('refresh_token', ''),
        'username': username,
    })


def mark_github_app_installed(installed=True):
    try:
        call('POST', '/v1/credentials/github/app_installed',
             {'installed': bool(installed)})
    except ServerUnavailable:
        pass


def save_gitlab_credentials(username, token):
    call('POST', '/v1/credentials/gitlab',
         {'username': username, 'token': token})


def test_gitlab_credentials(username='', token=''):
    """Validate the supplied GitLab username + PAT against
    ``gitlab.com/api/v4/user``. Empty fields fall through to the
    stored credentials on the server side, so the UI's Test button can
    re-check what's already saved without making the user retype.

    Returns ``{'ok': bool, 'valid': bool, 'server_username': str,
    'error': str}``. ``ok=False`` means the daemon was unreachable;
    ``valid=False`` with a populated ``error`` means the daemon ran
    the check and the credentials were rejected (or the username
    didn't match what GitLab returned)."""
    try:
        resp = call('POST', '/v1/credentials/gitlab/test',
                    {'username': username, 'token': token})
    except ServerUnavailable as ex:
        return {'ok': False, 'valid': False, 'server_username': '',
                'error': f'server_unavailable: {ex}'}
    return {
        'ok': bool(resp.get('ok')),
        'valid': bool(resp.get('valid')),
        'server_username': resp.get('server_username', '') or '',
        'error': resp.get('error', '') or '',
    }


def test_github_credentials():
    """Validate the stored GitHub access token against
    ``api.github.com/user`` and refresh the cached ``app_installed``
    flag at the same time. No args — the daemon reads the token from
    its credentials store. The flow is symmetric with
    ``test_gitlab_credentials``: a successful test persists
    ``confirmed=True`` (and the freshly-probed ``app_installed``);
    failure persists ``confirmed=False`` so the UI's verified badge
    drops back off until the user re-tests.

    Returns ``{'ok': bool, 'valid': bool, 'server_username': str,
    'app_installed': bool, 'error': str}``. ``ok=False`` means the
    daemon was unreachable; ``valid=False`` with a populated
    ``error`` means the daemon ran the check and the token failed."""
    try:
        resp = call('POST', '/v1/credentials/github/test', {})
    except ServerUnavailable as ex:
        return {'ok': False, 'valid': False, 'server_username': '',
                'app_installed': False, 'app_suspended': False,
                'installation_id': None,
                'error': f'server_unavailable: {ex}'}
    return {
        'ok': bool(resp.get('ok')),
        'valid': bool(resp.get('valid')),
        'server_username': resp.get('server_username', '') or '',
        'app_installed': bool(resp.get('app_installed')),
        # 0.30.11+: surfaces a suspended (paused-via-GitHub-UI)
        # install separately so the connect screen can route the
        # user to settings/installations/<id> for resume rather than
        # the generic install page. Older daemons return missing
        # keys; ``False`` / ``None`` defaults match the
        # "not suspended" interpretation.
        'app_suspended': bool(resp.get('app_suspended')),
        'installation_id': resp.get('installation_id'),
        'error': resp.get('error', '') or '',
    }


def migrate_from_prefs(prefs_path):
    """One-shot (idempotent) migration from a legacy prefs.json. The
    server moves gh_*/gl_*/collab_host keys into credentials.json and
    strips them from prefs.json."""
    try:
        resp = call('POST', '/v1/credentials/migrate_from_prefs',
                    {'prefs_path': prefs_path})
    except ServerUnavailable:
        return {'migrated': False, 'reason': 'server_unavailable'}
    return {k: v for k, v in resp.items() if k != 'ok'}


# ── Projects API ────────────────────────────────────────────────────────────

def list_projects():
    """Return a list of registered Projects."""
    try:
        resp = call('GET', '/v1/projects')
    except ServerUnavailable:
        return []
    if not resp.get('ok'):
        return []
    return [Project.from_dict(p) for p in resp.get('projects', [])]


def open_project(langcode):
    """Return the registered Project for *langcode*, or None."""
    try:
        resp = call('GET', f'/v1/projects/{langcode}')
    except ServerUnavailable:
        return None
    if not resp.get('ok'):
        return None
    return Project.from_dict(resp.get('project', {}))


def rename_project(old_langcode, new_langcode):
    """Rename a project's langcode key in the daemon's
    ``projects.json`` (preserving working_dir / lift_path /
    remote_url / created_at / last_sync). Used by the picker's
    confirm-langcode flow when the user overrides the
    auto-derived value before the project is handed back to the
    recorder. Same-name rename is a no-op.

    Returns the resulting Project on success, or None if the
    old langcode wasn't registered or the rename was rejected
    (e.g. the new langcode is already in use). On transport
    failure returns None and the caller should fall back to the
    derived langcode."""
    if not new_langcode or old_langcode == new_langcode:
        return open_project(old_langcode)
    try:
        resp = call('POST',
                    f'/v1/projects/{old_langcode}/rename',
                    {'new_langcode': new_langcode})
    except ServerUnavailable:
        return None
    if not resp.get('ok'):
        return None
    return Project.from_dict(resp.get('project', {}))


def register_project(langcode, working_dir, lift_path='', remote_url=''):
    """Tell the server about an existing project. Returns the Project."""
    resp = call('POST', '/v1/projects/register', {
        'langcode': langcode,
        'working_dir': working_dir,
        'lift_path': lift_path,
        'remote_url': remote_url,
    })
    if not resp.get('ok'):
        return None
    return Project.from_dict(resp.get('project', {}))


def derive_langcode(working_dir, lift_path=''):
    """Ask the server to compute a langcode from working_dir/lift_path.
    Returns '' on transport failure."""
    try:
        resp = call('POST', '/v1/projects/derive_langcode',
                    {'working_dir': working_dir, 'lift_path': lift_path})
    except ServerUnavailable:
        return ''
    if not resp.get('ok'):
        return ''
    return str(resp.get('langcode', ''))


def init_project(working_dir, remote_url, branch='main'):
    """Initialize a git repo at *working_dir*, set the remote, and
    push. Server uses store-resident credentials AND the
    store-resident contributor name (set via ``set_contributor``).

    As of 0.40.0 peers no longer pass ``contributor`` — the daemon
    is the sole authoritative source for the commit-author name
    (NOTES_TO_DAEMON.md "Daemon is now the sole authoritative
    source"). If no name is set, the daemon refuses with
    ``S.CONTRIBUTOR_UNSET`` — peers route the user to set their
    name via the daemon settings UI (``open_server_ui()``).

    Returns Result."""
    try:
        resp = call('POST', '/v1/projects/init', {
            'working_dir': working_dir,
            'remote_url': remote_url,
            'branch': branch,
        })
    except ServerUnavailable as ex:
        return Result(statuses=[Status(
            'SERVER_UNAVAILABLE', {'error': str(ex)})])
    if not resp.get('ok'):
        return Result(statuses=[Status(
            'SERVER_ERROR', {'error': resp.get('error', 'unknown')})])
    return Result.from_dict(resp.get('result') or {})


def create_project_from_template(vernlang, dest_dir, template_url=''):
    """Ask the server to download a LIFT template into
    ``dest_dir/<vernlang>.lift`` and register it as a project. Returns
    the resulting Project on success. On failure returns a tuple
    ``(None, error_str)`` so the host can surface the real reason
    (transport down, endpoint unknown on an old daemon, download failed,
    etc.). ``template_url=''`` uses the daemon's configured default
    (SILCAWL by default)."""
    try:
        resp = call('POST', '/v1/projects/from_template', {
            'template_url': template_url,
            'vernlang': vernlang,
            'dest_dir': dest_dir,
        })
    except ServerUnavailable as ex:
        return None, f'server_unavailable: {ex}'
    if not resp.get('ok'):
        err = resp.get('error') or 'unknown_error'
        if err == 'not_found':
            err = (
                'server_too_old (endpoint /v1/projects/from_template '
                'missing — restart the daemon)')
        return None, err
    return Project.from_dict(resp.get('project', {}))


def clone_project(remote_url, dest_dir, on_progress=None,
                  poll_interval=0.5, langcode=''):
    """Drive a server-side clone job to completion. Synchronous: blocks
    until the clone finishes (or fails). Returns
    ``{'ok': True, 'lift_path': str, 'result': Result}`` on success or
    ``{'ok': False, 'error': str, 'result': Result|None}`` on failure.
    ``on_progress(line)`` is called for each new server progress line.

    For a non-blocking driver (recorder uses this so it can run a Kivy
    Clock-driven progress loop), call ``clone_project_start`` +
    ``clone_project_status`` directly."""
    import time as _time
    kicked = clone_project_start(remote_url, dest_dir, langcode=langcode)
    if not kicked.get('ok'):
        return {'ok': False,
                'error': kicked.get('error', 'unknown'),
                'result': None}
    job_id = kicked['job_id']
    last_index = 0
    while True:
        _time.sleep(poll_interval)
        resp = clone_project_status(job_id, last_index)
        if not resp.get('ok'):
            return {'ok': False,
                    'error': resp.get('error', 'server_unavailable'),
                    'result': None}
        last_index = resp.get('next_index', last_index)
        if on_progress:
            for line in resp.get('progress', []):
                try:
                    on_progress(line)
                except Exception:
                    pass
        state = resp.get('state', 'CLONING')
        if state == 'DONE':
            return {'ok': bool(resp.get('lift_path')),
                    'lift_path': resp.get('lift_path', ''),
                    # Canonical langcode from the daemon's
                    # ``projects.json`` (set on auto-register after
                    # clone). Pass-through so peers can stamp it on
                    # the picker's result Intent without re-deriving
                    # — see CHANGELOG TODO closed in 0.18.1.
                    'langcode': resp.get('langcode', ''),
                    'result': resp.get('result'),
                    'error': '' if resp.get('lift_path') else 'no_lift_found'}
        if state == 'FAILED':
            return {'ok': False,
                    'error': resp.get('error', 'clone_failed'),
                    'result': resp.get('result')}


def clone_project_start(remote_url, dest_dir, langcode=''):
    """Kick off a server-side clone job. Returns ``{ok, job_id}`` on
    success or ``{ok: False, error}`` on failure. Poll progress with
    ``clone_project_status``.

    ``langcode`` is the user-confirmed key the daemon will register
    the project under (collected by the picker's confirm-langcode
    popup before this call). Empty string falls back to the daemon's
    auto-derivation from the LIFT filename / repo URL — matches the
    legacy desktop / scripted-call shape."""
    try:
        resp = call('POST', '/v1/projects/clone', {
            'remote_url': remote_url,
            'dest_dir': dest_dir,
            'langcode': langcode,
        })
    except ServerUnavailable as ex:
        return {'ok': False, 'error': f'server_unavailable: {ex}'}
    return resp


def clone_project_status(job_id, last_index=0):
    """Poll a clone job. Returns
    ``{ok, state, progress: [str], next_index, lift_path, result, error}``.
    State is one of ``'CLONING' | 'DONE' | 'FAILED'``. ``progress`` only
    contains lines emitted since ``last_index`` (use ``next_index`` for
    the next call)."""
    try:
        resp = call('POST', f'/v1/projects/clone/{job_id}',
                    {'last_index': int(last_index)})
    except ServerUnavailable as ex:
        return {'ok': False, 'error': f'server_unavailable: {ex}'}
    if resp.get('ok'):
        raw_result = resp.get('result')
        if raw_result is not None:
            resp['result'] = Result.from_dict(raw_result)
    return resp


def project_status(langcode):
    """Return a ProjectStatus for *langcode*, or None."""
    try:
        resp = call('GET', f'/v1/projects/{langcode}/status')
    except ServerUnavailable:
        return None
    if not resp.get('ok'):
        return None
    return ProjectStatus.from_dict(resp)


def sync_project(langcode):
    """Synchronous sync — the user-gestured "push pending commits
    now" bump. Returns Result. Blocks until the server's drain
    pass returns.

    Sync button surface: peers usually display a status badge
    (commits_ahead, work_offline) from ``project_status`` and call
    this only when the user taps the badge. Per-edit commits go
    through ``commit_project`` instead, which doesn't block on the
    network.

    Typed refusals the peer routes:
        S.WORK_OFFLINE_ENABLED  → toast + open_server_ui() to the
                                  sync settings screen (since
                                  0.43.0). Auto-sync paths never
                                  see this code because they go
                                  through ``commit_project``.
        S.CONTRIBUTOR_UNSET     → toast + open_server_ui() to set
                                  the user's name.
        S.AUTH_REQUIRED         → route to the credentials screen.

    As of 0.40.0 ``contributor`` is no longer a parameter — the
    daemon uses its store-resident contributor name."""
    try:
        resp = call('POST', f'/v1/projects/{langcode}/sync', {})
    except ServerUnavailable as ex:
        return Result(statuses=[Status(
            'SERVER_UNAVAILABLE', {'error': str(ex)})])
    if resp.get('ok'):
        return Result.from_dict(resp.get('result') or {})
    return Result(statuses=[Status(
        'SERVER_ERROR', {'error': resp.get('error', 'unknown')})])


def commit_project(langcode):
    """Schedule a debounced *commit* server-side. Returns a
    job_id (str) or None on transport failure. Multiple calls
    within the debounce window collapse into one commit.

    Since 0.43.0 the commit and push halves are split: this
    endpoint commits only. Push is driven by the daemon's
    connectivity watcher's drain loop (online + post-online
    grace + ``sync.work_offline`` off). Peers call this per
    group of related changes (e.g. recording a clip writes both
    the .wav and the .lift — one debounce window collapses both
    into one commit). The user-gestured Sync button —
    ``sync_project`` — is the only path that triggers a push
    immediately; everything else flows through the drain.

    Pre-0.43 this RPC was ``request_sync`` and did both halves;
    older peer code that still calls ``request_sync`` keeps
    working (alias) but should migrate — the result no longer
    carries ``PUSHED`` since this path doesn't push.

    Contributor is read from the daemon store at exec time.
    Unset name → ``S.CONTRIBUTOR_UNSET`` on poll_job(job_id);
    route the user to ``open_server_ui()`` to set it."""
    import sys as _sys
    print(f'[commit-client] commit_project({langcode!r}) sending',
          file=_sys.stderr, flush=True)
    try:
        resp = call('POST', f'/v1/projects/{langcode}/commit', {})
    except ServerUnavailable as ex:
        print(f'[commit-client] commit_project({langcode!r}) → '
              f'ServerUnavailable: {ex}',
              file=_sys.stderr, flush=True)
        return None
    if not resp.get('ok'):
        print(f'[commit-client] commit_project({langcode!r}) → '
              f'not ok, resp={resp!r}',
              file=_sys.stderr, flush=True)
        return None
    job_id = resp.get('job_id')
    print(f'[commit-client] commit_project({langcode!r}) → '
          f'job_id={job_id!r}',
          file=_sys.stderr, flush=True)
    return job_id


# Backwards-compat alias for peers that still import the old
# name. Same wire path (the daemon routes the legacy
# ``sync_async`` URL to the new ``commit`` handler), same
# return shape, but the result no longer carries ``PUSHED`` —
# push moved to the daemon's drain loop.
request_sync = commit_project


def get_work_offline():
    """Read the daemon-wide work-offline toggle. Returns bool
    (False on transport failure — safe default since the daemon
    is the source of truth and the peer can't push without it
    anyway)."""
    try:
        resp = call('GET', '/v1/config/work_offline')
    except ServerUnavailable:
        return False
    if not resp.get('ok'):
        return False
    return bool(resp.get('work_offline', False))


def set_work_offline(enabled: bool):
    """Persist the daemon-wide work-offline toggle. Returns the
    new value the daemon reports (or False on transport failure).
    Toggling OFF triggers an immediate push-drain pass server-
    side; toggling ON suppresses the watcher's drain and makes
    the Sync button return ``S.WORK_OFFLINE_ENABLED``."""
    try:
        resp = call('POST', '/v1/config/work_offline',
                    {'enabled': bool(enabled)})
    except ServerUnavailable:
        return False
    if not resp.get('ok'):
        return False
    return bool(resp.get('work_offline', False))


def cawl_index(langcode):
    """Return the daemon's CAWL image-URL index for ``langcode``'s
    image repo.

    The daemon resolves the project's ``cawl_image_repo`` (per-
    project field with daemon-global fallback) and serves the
    cached index for that repo. Two projects pointing at the same
    image_repo share one cache directory, so the dedup is
    transparent — the peer doesn't need to know the repo slug.

    Shape::

        {
            'repo':       'owner/repo',          # what was fetched
            'branch':     'HEAD',                # symbolic; not deref'd
            'fetched_at': 1715520000,            # unix seconds
            'files': [
                {'path': 'cawl-1234.jpg',
                 'url':  'https://raw.githubusercontent.com/.../cawl-1234.jpg'},
                ...
            ],
        }

    Empty dict on any failure (daemon unreachable, project unknown,
    no image_repo configured for the project, endpoint missing on
    older daemons): peers treat that as "no images known" — same
    shape as their pre-migration empty resolver dict, no daemon-
    error branch required.

    The daemon caches the index under
    ``$AZT_HOME/cawl/<owner>/<repo>/index.json`` and refreshes
    lazily on a TTL (24h default). Peers calling ``cawl_index``
    repeatedly within the TTL get the cached copy — there is no
    per-peer rate-limit cost to calling this on every project
    load. Pre-migration peers fetched directly from
    ``api.github.com`` on every load and exhausted GitHub's
    60/hr unauthenticated cap; this wrapper is the daemon-owned
    replacement.

    Peers map ``files`` to whatever CAWL-identifier→URL shape they
    want; the daemon stays naming-convention-agnostic. For image
    *binaries* (the bytes), use ``CAWLHandle(langcode, basename).
    open_read()`` — also daemon-served, one cache per device per
    repo regardless of peer count.

    Android transport routes through the ContentProvider's file
    URI (``<lang>/cawl/index.json``) instead of the JSON-RPC
    endpoint. The RPC path goes through ``ContentResolver.call``
    whose Bundle response is capped at ~1 MB per Binder
    transaction — a populated CAWL index (~5000+ entries with
    long GitHub raw URLs) exceeds that cap and the Bundle is
    dropped silently, surfacing here as an empty dict even though
    the daemon emits the full payload. The file route uses a
    kernel FD with no IPC size limit. Desktop loopback HTTP has
    no such cap so the JSON-RPC path is fine there."""
    try:
        from kivy.utils import platform
    except Exception:
        platform = ''
    if platform == 'android':
        try:
            from .lift_io import _cawl_index_via_fd
            return _cawl_index_via_fd(langcode)
        except Exception:
            return {}
    try:
        resp = call('GET', f'/v1/projects/{langcode}/cawl/index')
    except ServerUnavailable:
        return {}
    if not resp.get('ok'):
        return {}
    index = resp.get('index')
    return index if isinstance(index, dict) else {}


def cawl_prefetch(langcode, paths):
    """Ask the daemon to warm a working-set of CAWL image paths
    in the background.

    The daemon spawns a worker that iterates *paths* and pulls
    each into its image cache (``get_image_path``-driven; serves
    from cache or fetches from GitHub). Returns immediately; peers
    poll ``cawl_cache_status(langcode)`` for progress.

    Idempotent: a second call with the same paths against an
    active worker returns the existing state. A call with a
    different paths-set replaces the state and starts a new
    worker.

    *paths* is a list of relative paths inside the repo
    (``"0001_body/foo.png"``, ``"0002_skin_of_man/bar.png"``,
    etc.) — the same shape ``cawl_index(langcode)['files'][i]
    ['path']`` returns. Peers that map CAWL identifiers to a
    single variant per identifier should pass just their chosen
    variants here so the progress banner reflects work the peer
    will actually use, not the whole repo.

    Why peers should prefer this over iterating
    ``CAWLHandle(...).open_read()`` themselves: the daemon-driven
    iteration lets the daemon know the size of the work, which is
    what makes the ``cache_status`` progress indicator
    meaningful. The per-image path still works for on-demand
    fetches when the peer needs a specific image (e.g., the
    current swipe target); use it for that, use this for bulk
    warming.

    Returns a dict::

        {'image_repo': 'owner/repo',
         'requested': N, 'completed': M, 'finished': bool}

    Empty/failure values on any transport or daemon error."""
    empty = {'image_repo': '', 'requested': 0,
             'completed': 0, 'finished': True}
    if not isinstance(paths, (list, tuple)):
        return empty
    try:
        resp = call('POST',
                    f'/v1/projects/{langcode}/cawl/prefetch',
                    {'paths': list(paths)})
    except ServerUnavailable:
        return empty
    if not resp.get('ok'):
        return empty
    return {
        'image_repo': resp.get('image_repo') or '',
        'requested': int(resp.get('requested') or 0),
        'completed': int(resp.get('completed') or 0),
        'finished': bool(resp.get('finished')),
    }


def cawl_cache_status(langcode):
    """Return ``{'image_repo': str, 'cached': int, 'total': int}``
    for the CAWL image cache backing *langcode*'s image_repo.

    ``cached`` is the count of image files currently on disk in the
    daemon's cache; ``total`` is the image-shaped index entries.
    Peers poll this while a CAWL prefetch is running and surface
    a "Caching images: M / N" indicator so the user knows
    network is being used in the background — without that
    indicator they might disconnect Wi-Fi mid-fetch and end up
    with a half-warm cache.

    Returns ``{'image_repo': '', 'cached': 0, 'total': 0}`` on
    any failure (daemon unreachable, project unknown, no image
    repo configured, endpoint missing on older daemons). Peers
    treat that as "nothing to show" and hide the indicator."""
    empty = {'image_repo': '', 'cached': 0, 'total': 0}
    try:
        resp = call('GET',
                    f'/v1/projects/{langcode}/cawl/cache_status')
    except ServerUnavailable:
        return empty
    if not resp.get('ok'):
        return empty
    return {
        'image_repo': resp.get('image_repo') or '',
        'cached': int(resp.get('cached') or 0),
        'total': int(resp.get('total') or 0),
    }


def set_cawl_image_repo(langcode, repo):
    """Persist a per-project CAWL image_repo override.

    ``repo`` is a GitHub ``owner/repo`` slug, e.g.
    ``'kent-rasmussen/cawl-images'``. Empty string clears the
    override; the project then falls back to the daemon-global
    default (set via ``azt_collabd.configure(cawl_image_repo=…)``
    on the recorder side, or the ``AZT_CAWL_IMAGE_REPO`` env var).

    Returns the updated ``Project``, or None on transport failure
    / unknown langcode. Best-effort: callers can drive a UI from
    the return shape but should not block on it."""
    try:
        resp = call('POST', f'/v1/projects/{langcode}/cawl_image_repo',
                    {'cawl_image_repo': str(repo or '')})
    except ServerUnavailable:
        return None
    if not resp.get('ok'):
        return None
    return Project.from_dict(resp.get('project', {}))


def set_repo_slug(langcode, slug):
    """Persist a per-project GitHub-repo-name override for the
    publish path.

    Most projects publish to a repo named after the langcode
    (``Project.langcode`` is the typical default). This setter
    lets the user override that — vanity slug, project-style
    naming, collision avoidance with an existing GitHub repo —
    without changing the LIFT ``<form lang="…">`` tag.

    ``slug`` is a plain repo name (no owner, no slashes). Empty
    string clears the override; callers should then fall back
    to ``langcode``.

    Returns the updated ``Project``, or None on transport
    failure / unknown langcode. Read the resulting slug at any
    time via ``open_project(langcode).repo_slug``;
    ``project_status(langcode)`` and ``list_projects()`` carry
    the same field. Pre-0.39 daemons don't emit it, so the
    client-side dataclass defaults to ``''`` for forward-compat;
    that means a peer that calls this against an older daemon
    will get None back (404 on the endpoint) — the same shape
    every other setter wrapper uses, so peer code branches
    consistently."""
    try:
        resp = call('POST', f'/v1/projects/{langcode}/repo_slug',
                    {'repo_slug': str(slug or '')})
    except ServerUnavailable:
        return None
    if not resp.get('ok'):
        return None
    return Project.from_dict(resp.get('project', {}))


def atomic_commit_bytes(langcode, rel_path, data):
    """Atomically write *data* to ``<working_dir>/<rel_path>`` for
    project *langcode*. Goes through the daemon's
    ``/v1/projects/<lang>/atomic_commit`` endpoint so the
    tempfile-rename dance happens in the daemon's process — where
    the destination filesystem lives on Android — and serializes
    via ``project_lock`` against the daemon's own merge-output
    writes and any other peer's atomic_commit.

    *rel_path* is one of:

    - ``<file>.lift``           — top-level LIFT file
    - ``audio/<file>``          — sibling audio
    - ``images/<file>``         — sibling image

    Returns ``Result``. Success: a single ``ATOMIC_COMMITTED``
    status with ``bytes_written`` and ``sha256`` params. Transport
    failures translate to ``SERVER_UNAVAILABLE`` / ``SERVER_ERROR``
    like every other wrapper — peers never see a raw
    ``ServerUnavailable``.

    Used by ``LiftHandle.atomic_open_write`` (and ``MediaHandle``)
    for ``content://`` URIs, where the FD-write path through the
    ContentProvider has no atomic-rename equivalent. Filesystem
    paths still get the local tempfile+rename via
    ``_AtomicWriteFile`` directly.

    Memory cost: *data* is base64-encoded for transit, so the peer
    holds ~1.33× the file size in memory between encode and the
    HTTP/binder send. For LIFT (tens of MB at worst) this is fine.
    Pass chunked uploads if a future case ships a much larger
    payload."""
    data_b64 = base64.b64encode(data).decode('ascii')
    try:
        resp = call('POST', f'/v1/projects/{langcode}/atomic_commit',
                    {'path': rel_path, 'data_b64': data_b64})
    except ServerUnavailable as ex:
        return Result(statuses=[Status(
            'SERVER_UNAVAILABLE', {'error': str(ex)})])
    if resp.get('ok'):
        return Result.from_dict(resp.get('result') or {})
    return Result(statuses=[Status(
        'SERVER_ERROR', {'error': resp.get('error', 'unknown')})])


def set_daemon_log_to_file(enabled):
    """Toggle the daemon's stderr-to-file mirror. Takes effect
    immediately in the running daemon process — no restart
    required. Returns ``{'enabled': bool, 'log_path': str}`` on
    success, ``None`` on transport failure."""
    try:
        resp = call('POST',
                    '/v1/logging/daemon_log_to_file',
                    {'enabled': bool(enabled)})
    except ServerUnavailable:
        return None
    if not resp.get('ok'):
        return None
    return {
        'enabled': bool(resp.get('enabled')),
        'log_path': resp.get('log_path') or '',
    }


def get_daemon_log():
    """Return the daemon's persisted stderr log as
    ``{'log': str, 'log_path': str, 'bytes': int,
    'enabled': bool}``. ``log`` is truncated daemon-side to the
    last ~256 KB if the file is larger. ``enabled`` reflects the
    current state of the "Save daemon log to file" toggle —
    useful for the settings UI to seed its button label without
    a separate getter call. Empty ``log`` (with ``bytes=0``)
    when the toggle hasn't been enabled / no output accumulated
    yet. Returns ``None`` on transport failure."""
    try:
        resp = call('GET', '/v1/logging/daemon_log')
    except ServerUnavailable:
        return None
    if not resp.get('ok'):
        return None
    return {
        'log': resp.get('log') or '',
        'log_path': resp.get('log_path') or '',
        'bytes': int(resp.get('bytes') or 0),
        'enabled': bool(resp.get('enabled')),
    }


def restart_server():
    """Ask the daemon to restart itself.

    Daemon returns OK and then, after a short delay so the response
    can flush, exits — on desktop the process re-execs into
    ``python -m azt_collabd``; on Android the ``:provider`` process
    exits and Android's ContentProvider auto-spawn revives it on
    the next peer call. Caller sees ``Result`` with one of:

    * ``RESTARTING`` (informational): the daemon accepted the request
      and the restart is in flight. The next RPC from this peer will
      land on a fresh daemon (loopback transport prints a single
      ``SERVICE_RESTARTED`` line; ContentProvider transparently
      lazy-spawns).
    * ``SERVER_UNAVAILABLE``: no daemon was reachable to accept the
      request in the first place.
    * ``SERVER_ERROR``: the daemon returned a non-OK response.

    The wrapper itself never raises — UI code can call it from any
    button handler without try/except, consistent with the
    query-shaped-wrapper rule in ``azt_collab_client/CLAUDE.md``.
    Caller should typically follow with a short delay + a
    ``health()`` check to confirm the new daemon is up before
    re-invoking other RPCs."""
    try:
        resp = call('POST', '/v1/admin/restart', {})
    except ServerUnavailable as ex:
        return Result(statuses=[Status(
            'SERVER_UNAVAILABLE', {'error': str(ex)})])
    if resp.get('ok'):
        return Result(statuses=[Status('RESTARTING', {
            'transport': resp.get('transport') or 'unknown',
        })])
    return Result(statuses=[Status(
        'SERVER_ERROR', {'error': resp.get('error', 'unknown')})])


def atomic_finalize_pending(langcode, rel_path, token):
    """Phase 2 of the two-phase atomic write: rename the daemon's
    scratch file at ``<working_dir>/.azt_atomic_pending/<token>``
    to ``<working_dir>/<rel_path>``, atomically, under the project
    lock.

    Used internally by ``LiftHandle.atomic_open_write`` /
    ``MediaHandle.atomic_open_write`` on Android to bypass the
    Binder per-transaction size cap that limits the legacy
    single-RPC ``atomic_commit_bytes`` to payloads under ~700 KB.
    Phase 1 ships the bytes via the ContentProvider FD path (no
    Binder cap); this RPC is the small finalize.

    Public-but-internal: peers should drive this through
    ``atomic_open_write``, not call it directly.

    Returns ``Result``. Success: a single ``ATOMIC_COMMITTED``
    status with ``bytes_written`` and ``sha256`` params, same
    shape ``atomic_commit_bytes`` returns. Transport failures
    translate to ``SERVER_UNAVAILABLE`` / ``SERVER_ERROR``."""
    try:
        resp = call('POST',
                    f'/v1/projects/{langcode}/atomic_finalize',
                    {'token': token, 'path': rel_path})
    except ServerUnavailable as ex:
        return Result(statuses=[Status(
            'SERVER_UNAVAILABLE', {'error': str(ex)})])
    if resp.get('ok'):
        return Result.from_dict(resp.get('result') or {})
    return Result(statuses=[Status(
        'SERVER_ERROR', {'error': resp.get('error', 'unknown')})])


def poll_job(job_id):
    """Return the current state of a job: dict with keys ``state``
    ('PENDING' | 'RUNNING' | 'DONE'), ``langcode``, ``result`` (Result
    or None), ``created_at``, ``started_at``, ``finished_at``. Returns
    None if the job is unknown or unreachable."""
    try:
        resp = call('GET', f'/v1/jobs/{job_id}')
    except ServerUnavailable:
        return None
    if not resp.get('ok'):
        return None
    raw_result = resp.get('result')
    decoded_result = (Result.from_dict(raw_result)
                      if raw_result is not None else None)
    return {
        'job_id': resp.get('job_id'),
        'langcode': resp.get('langcode'),
        'state': resp.get('state'),
        'result': decoded_result,
        'created_at': resp.get('created_at', 0.0),
        'started_at': resp.get('started_at', 0.0),
        'finished_at': resp.get('finished_at', 0.0),
    }


def record_project_sync_time(langcode, timestamp=None):
    body = {}
    if timestamp is not None:
        body['timestamp'] = float(timestamp)
    try:
        call('POST', f'/v1/projects/{langcode}/last_sync', body)
    except ServerUnavailable:
        pass


def grant_collaborator(langcode, username, level='push'):
    """Invite ``username`` as a collaborator on the GitHub repo
    backing ``langcode``. ``level`` is the GitHub permission
    ('pull' | 'push' | 'admin' | 'maintain' | 'triage'); default
    ``'push'`` matches typical SIL collaborator workflow.

    Returns a ``Result`` carrying one of:

    - ``S.COLLABORATOR_INVITED`` — invitation issued; the user must
      still accept it on GitHub.
    - ``S.COLLABORATOR_ALREADY`` — already a collaborator (or has
      a pending invite); no new state on GitHub.
    - ``S.INVALID_USERNAME`` — empty / whitespace username.
    - ``S.NO_REMOTE`` — project has no remote URL configured.
    - ``S.NOT_GITHUB_REMOTE`` — remote is not a GitHub URL (GitLab
      / self-hosted not yet supported by this endpoint).
    - ``S.AUTH_REQUIRED`` — no GitHub token on file for the host.
    - ``S.COLLABORATOR_INVITE_FAILED`` — GitHub returned an
      unexpected error (auth refused, repo not found, etc.); the
      ``error`` param carries the underlying message.

    The langcode-based dispatch means peers don't have to parse
    repo URLs themselves — the daemon looks up the project's
    ``remote_url`` and extracts ``owner/repo``, eliminating "wrong
    project" risk from peer-side URL handling."""
    body = {'username': str(username or ''), 'level': str(level or 'push')}
    try:
        resp = call('POST',
                    f'/v1/projects/{langcode}/collaborators',
                    body)
    except ServerUnavailable as ex:
        return Result(statuses=[Status(
            'SERVER_UNAVAILABLE', {'error': str(ex)})])
    if resp.get('ok'):
        return Result.from_dict(resp.get('result') or {})
    return Result(statuses=[Status(
        'SERVER_ERROR', {'error': resp.get('error', 'unknown')})])


__all__ = [
    'configure', 'is_online', 'open_server_ui', 'pick_project',
    'check_server_compat',
    'get_credentials_status', 'set_collab_host',
    'get_contributor', 'set_contributor',
    'get_device_name', 'set_device_name',
    'get_cawl_prefetch_all_variants', 'set_cawl_prefetch_all_variants',
    'github_app_install_url', 'github_app_client_id',
    'github_device_flow_start', 'github_device_flow_status',
    'save_github_tokens', 'mark_github_app_installed',
    'save_gitlab_credentials', 'test_gitlab_credentials',
    'test_github_credentials',
    'migrate_from_prefs',
    'list_projects', 'open_project', 'register_project', 'rename_project',
    'derive_langcode', 'init_project',
    'create_project_from_template',
    'clone_project',
    'clone_project_start', 'clone_project_status',
    'project_status', 'sync_project',
    'commit_project', 'request_sync', 'poll_job',
    'get_work_offline', 'set_work_offline',
    'atomic_commit_bytes', 'atomic_finalize_pending',
    'set_daemon_log_to_file', 'get_daemon_log',
    'restart_server',
    'cawl_index', 'cawl_cache_status', 'cawl_prefetch',
    'set_cawl_image_repo', 'set_repo_slug',
    'record_project_sync_time', 'grant_collaborator',
    'LiftHandle', 'MediaHandle', 'CAWLHandle',
    'audio_uri_for', 'image_uri_for', 'is_content_uri',
    'last_project', 'set_last_project',
    'peer_pref', 'set_peer_pref',
    'Status', 'Result', 'S', 'Project', 'ProjectStatus',
    'translate_status', 'translate_result', 'set_translator',
    'ServerUnavailable',
    '__version__', 'MIN_SERVER_VERSION',
]
