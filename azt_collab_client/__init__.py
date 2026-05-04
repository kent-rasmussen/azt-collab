"""
azt_collab_client — thin client library for azt_collabd.

Ops that go through the server return a ``Result`` (structured status
codes + params); the caller calls ``translate_result(result)`` for
display. ``Result.has(S.PUSHED)`` etc. is the way to drive business
logic — no more substring matching on log strings.
"""

__version__ = "0.20.0"
# 0.16.0 floor: the daemon now persists scheduler jobs across
# kills (jobs.json + reconcile_on_startup). Pre-0.16 daemons forget
# job_ids on respawn, so poll_job returns None and the peer can't
# distinguish "never existed" from "interrupted." Bumping the floor
# forces the user to update the server APK / azt_collabd if the
# peer ships against 0.20+ client.
MIN_SERVER_VERSION = "0.16.0"
SERVER_APK_INSTALL_URL = (
    'https://github.com/atoznback/azt-collab/releases/latest'
)
from . import status as S
from .status import Status, Result
from .projects import Project, ProjectStatus
from .translate import translate_status, translate_result, set_translator
from .rpc import call, health, ServerUnavailable
from .lift_io import (
    LiftHandle, MediaHandle, audio_uri_for, image_uri_for, is_content_uri,
)
from .recent import last_project, set_last_project


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
    import threading
    try:
        from jnius import autoclass
        from android import activity as android_activity  # noqa: F401
        from kivy.clock import Clock
    except Exception as ex:
        return {'ok': False, 'error': 'spawn_failed', 'detail': str(ex)}

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
        import sys as _sys
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
              f'data_present={data is not None}',
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
    server as too old in that case, which is the safer side."""
    if not s:
        return (0, 0, 0)
    out = []
    for chunk in str(s).split('.'):
        digits = ''
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
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
        return {'ok': False, 'error': 'server_unreachable',
                'detail': str(ex)}
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
    silently no-ops on transport failure."""
    try:
        call('POST', '/v1/config/contributor', {'contributor': name})
    except ServerUnavailable:
        pass


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


def init_project(working_dir, remote_url, branch='main',
                 contributor='Recorder'):
    """Initialize a git repo at *working_dir*, set the remote, and
    push. Server uses store-resident credentials. Returns Result."""
    try:
        resp = call('POST', '/v1/projects/init', {
            'working_dir': working_dir,
            'remote_url': remote_url,
            'branch': branch,
            'contributor': contributor,
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


def sync_project(langcode, contributor):
    """Synchronous sync. Returns Result. Blocks until the server's sync
    pass returns. Use ``request_sync`` for fire-and-forget edits where
    the UI doesn't wait."""
    try:
        resp = call('POST', f'/v1/projects/{langcode}/sync',
                    {'contributor': contributor})
    except ServerUnavailable as ex:
        return Result(statuses=[Status(
            'SERVER_UNAVAILABLE', {'error': str(ex)})])
    if resp.get('ok'):
        return Result.from_dict(resp.get('result') or {})
    return Result(statuses=[Status(
        'SERVER_ERROR', {'error': resp.get('error', 'unknown')})])


def request_sync(langcode, contributor):
    """Schedule a debounced sync server-side. Returns a job_id (str) or
    None on transport failure. Multiple calls within the debounce
    window collapse into one run; the server commits and pushes once."""
    try:
        resp = call('POST', f'/v1/projects/{langcode}/sync_async',
                    {'contributor': contributor})
    except ServerUnavailable:
        return None
    if not resp.get('ok'):
        return None
    return resp.get('job_id')


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


__all__ = [
    'configure', 'is_online', 'open_server_ui', 'pick_project',
    'check_server_compat',
    'get_credentials_status', 'set_collab_host',
    'get_contributor', 'set_contributor',
    'github_app_install_url', 'github_app_client_id',
    'github_device_flow_start', 'github_device_flow_status',
    'save_github_tokens', 'mark_github_app_installed',
    'save_gitlab_credentials', 'migrate_from_prefs',
    'list_projects', 'open_project', 'register_project', 'rename_project',
    'derive_langcode', 'init_project',
    'create_project_from_template',
    'clone_project',
    'clone_project_start', 'clone_project_status',
    'project_status', 'sync_project', 'request_sync', 'poll_job',
    'record_project_sync_time',
    'LiftHandle', 'MediaHandle', 'audio_uri_for', 'image_uri_for',
    'is_content_uri',
    'last_project', 'set_last_project',
    'Status', 'Result', 'S', 'Project', 'ProjectStatus',
    'translate_status', 'translate_result', 'set_translator',
    'ServerUnavailable',
    '__version__', 'MIN_SERVER_VERSION',
]
