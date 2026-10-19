"""Desktop settings-UI launch handshake (no Kivy, no daemon import).

``open_server_ui()`` used to call a spawn "ok" if the child had not
exited within 0.25 s, with the child's stderr on a PIPE nobody read.
On Windows that pipe is a few KB, Kivy's startup logging fills it, and
the child blocks before its window draws — while the host has already
logged "spawned". Field 2026-10-19 (Windows 11, azt on OneDrive):
nothing ever appeared.

This module is both halves of the replacement:

- **UI side** (``azt_collabd/ui/app.py``) calls ``mark_ready(token)``
  once its window exists, and polls ``take_raise_request()`` so a
  second launch can bring it to the front instead of starting a rival.
- **Launcher side** (``open_desktop``) sends the child's output to a
  per-day log file, and reports ``ok`` only when the child it started
  has said ready — matched by a per-launch token, not a pid, because a
  Windows venv's ``python.exe`` is a redirector whose pid is not the
  interpreter's.

Files, all under ``$AZT_HOME``:

- ``settings_ui.json`` — ``{pid, token, started, ready, raised_req}``,
  written by the local (non-remote) UI only; removed on clean exit.
- ``settings_ui_raise`` — a raise request; its content is a request id
  the UI echoes into ``raised_req`` when it has acted on it.
- ``settings-ui-YYYY-MM-DD_log.txt`` — the UI child's stdout + stderr.
"""

import datetime
import json
import os
import re
import sys
import time
import uuid

from .paths import azt_home

# How long a launch may take to show its window before we stop waiting
# and say so. Python + Kivy from a synced folder (OneDrive) on a slow
# disk takes seconds; this is generous on purpose — the child is left
# running either way.
READY_TIMEOUT_S = 15.0
# How long a running UI has to acknowledge a raise request before we
# decide its marker is stale (pid reused, or a hung UI) and launch anew.
RAISE_ACK_TIMEOUT_S = 3.0
# Lines of the UI log returned as ``detail`` on failure.
_DETAIL_LINES = 12

_LOG_RE = re.compile(r'^settings-ui-(\d{4}-\d{2}-\d{2})_log\.txt$')


def marker_path():
    return os.path.join(azt_home(), 'settings_ui.json')


def raise_request_path():
    return os.path.join(azt_home(), 'settings_ui_raise')


def log_path(day=None):
    day = day or datetime.date.today()
    return os.path.join(azt_home(),
                        f'settings-ui-{day.isoformat()}_log.txt')


def log_files():
    """Every retained settings-UI log, oldest first (for diagnostics)."""
    try:
        names = os.listdir(azt_home())
    except OSError:
        return []
    return sorted(os.path.join(azt_home(), n) for n in names
                  if _LOG_RE.match(n))


def _retention_days():
    # Same knob as the daemon's per-day logs. Read the file directly:
    # the daemon may be the thing that's down.
    try:
        with open(os.path.join(azt_home(), 'config.json'),
                  encoding='utf-8') as f:
            cfg = json.load(f)
        v = (cfg.get('logging') or {}).get('retention_days')
        if v is None:
            v = cfg.get('logging.retention_days', 3)
        return max(1, int(v))
    except Exception:
        return 3


def _prune_logs():
    cutoff = datetime.date.today() - datetime.timedelta(
        days=_retention_days())
    for p in log_files():
        m = _LOG_RE.match(os.path.basename(p))
        try:
            if datetime.date.fromisoformat(m.group(1)) < cutoff:
                os.remove(p)
        except Exception:
            pass


def _write_json_atomic(path, data):
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_marker():
    try:
        with open(marker_path(), encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _pid_alive(pid):
    from .transports.loopback import LoopbackTransport
    return LoopbackTransport._pid_alive(pid)


# ── UI side ──────────────────────────────────────────────────────────────

def mark_ready(token=''):
    """Record that this process's settings window exists. Called by the
    local UI from ``on_start``; never by a ``--peer`` (remote) window,
    which is a deliberate second window, not a rival."""
    _write_json_atomic(marker_path(), {
        'pid': os.getpid(), 'token': token or '',
        'started': time.time(), 'ready': True, 'raised_req': '',
    })


def take_raise_request():
    """If a raise request is waiting, consume it, acknowledge it in the
    marker, and return True. The caller raises its window."""
    p = raise_request_path()
    try:
        with open(p, encoding='utf-8') as f:
            req = f.read().strip()
        os.remove(p)
    except FileNotFoundError:
        return False
    except Exception:
        req = ''
    data = read_marker()
    if data.get('pid') == os.getpid():
        data['raised_req'] = req
        try:
            _write_json_atomic(marker_path(), data)
        except Exception:
            pass
    return True


def clear_marker():
    """On clean exit: remove the marker if it is still ours."""
    if read_marker().get('pid') == os.getpid():
        try:
            os.remove(marker_path())
        except OSError:
            pass


# ── launcher side ────────────────────────────────────────────────────────

def _log_tail(path, offset):
    try:
        with open(path, 'rb') as f:
            f.seek(offset)
            text = f.read().decode('utf-8', 'replace')
    except Exception:
        return ''
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return '\n'.join(lines[-_DETAIL_LINES:])


def _raise_running(timeout):
    """Ask an already-running UI to come to the front. Returns its pid
    if it acknowledged within ``timeout``, else None."""
    data = read_marker()
    pid = data.get('pid')
    if not data.get('ready') or not _pid_alive(pid):
        return None
    req = uuid.uuid4().hex
    try:
        with open(raise_request_path(), 'w', encoding='utf-8') as f:
            f.write(req)
    except OSError:
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if read_marker().get('raised_req') == req:
            return pid
        time.sleep(0.1)
    # Nobody acted on it: the marker is stale (pid reused) or that UI
    # is hung. Withdraw the request so a new UI doesn't act on it.
    try:
        os.remove(raise_request_path())
    except OSError:
        pass
    return None


def _windows_gui_python(python_exe):
    """``pythonw.exe`` beside ``python.exe`` if there is one: same
    interpreter, no console window."""
    d, base = os.path.split(python_exe)
    if base.lower() == 'python.exe':
        cand = os.path.join(d, 'pythonw.exe')
        if os.path.isfile(cand):
            return cand
    return python_exe


def open_desktop(argv, env=None, timeout=READY_TIMEOUT_S,
                 raise_timeout=RAISE_ACK_TIMEOUT_S):
    """Launch (or raise) the local settings UI and report what happened.

    ``argv`` is the command up to and including ``ui``; the per-launch
    ``--ready-token`` is appended here. Returns one of:

    - ``{'ok': True, 'pid': …, 'raised': True}`` — one was already open;
    - ``{'ok': True, 'pid': …}`` — the new window said it is up;
    - ``{'ok': False, 'error': 'spawn_failed', 'detail': …}``;
    - ``{'ok': False, 'error': 'spawn_exited', 'returncode': …,
      'detail': <UI log tail>, 'log': <path>}``;
    - ``{'ok': False, 'error': 'ui_not_ready', 'detail': …,
      'log': <path>, 'pid': …}`` — still starting, or stuck; left running.
    """
    import subprocess
    pid = _raise_running(raise_timeout)
    if pid:
        return {'ok': True, 'pid': pid, 'raised': True}

    argv = list(argv)
    kwargs = {}
    if sys.platform == 'win32':
        argv[0] = _windows_gui_python(argv[0])
        kwargs['creationflags'] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.DETACHED_PROCESS)
    else:
        kwargs['start_new_session'] = True
    token = uuid.uuid4().hex
    argv += ['--ready-token', token]

    _prune_logs()
    lp = log_path()
    try:
        os.makedirs(os.path.dirname(lp), exist_ok=True)
        log = open(lp, 'ab')
    except OSError as ex:
        return {'ok': False, 'error': 'spawn_failed',
                'detail': f'cannot open {lp}: {ex}'}
    with log:
        stamp = datetime.datetime.now().isoformat(timespec='seconds')
        log.write(f'--- {stamp} launch: {argv}\n'.encode('utf-8'))
        log.flush()
        offset = log.tell()
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, close_fds=True, env=env,
                **kwargs)
        except OSError as ex:
            return {'ok': False, 'error': 'spawn_failed',
                    'detail': str(ex), 'log': lp}

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = read_marker()
        if data.get('token') == token and data.get('ready'):
            return {'ok': True, 'pid': data.get('pid') or proc.pid}
        rc = proc.poll()
        if rc is not None:
            return {'ok': False, 'error': 'spawn_exited',
                    'returncode': rc, 'detail': _log_tail(lp, offset),
                    'log': lp}
        time.sleep(0.1)
    return {'ok': False, 'error': 'ui_not_ready', 'pid': proc.pid,
            'detail': _log_tail(lp, offset), 'log': lp}
