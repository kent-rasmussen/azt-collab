"""
Loopback (HTTP+JSON over 127.0.0.1) transport. Reads
``$AZT_HOME/server.json`` to discover ``{port, token}``; auto-spawns
the daemon via ``python -m azt_collabd`` on transport failure;
budgets retries so a daemon restart shows up as a single
``SERVICE_RESTARTED`` log line rather than a hung call.
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from . import Transport, ServerUnavailable
from .._spawn import build_spawn_env
from ..paths import server_info_path


_DEFAULT_TIMEOUT = 300
_HEALTH_TIMEOUT = 1.5
_SPAWN_WAIT = 5.0
_MAX_ATTEMPTS = 3
# After a spawn attempt fails to produce a healthy daemon, don't
# try again for this long. Without it, a host app polling every few
# seconds turns one wedged daemon into an endless spawn storm — one
# new ``python -m azt_collabd`` every poll, each exiting on the held
# ``server.lock`` (field incident 2026-07-10: ~5 s cadence for 8+
# minutes until the wedged daemon was SIGTERMed).
_SPAWN_COOLDOWN_S = 60.0


class LoopbackTransport(Transport):
    name = 'loopback'

    def __init__(self):
        self._spawn_lock = threading.Lock()
        self._last_failed_spawn = 0.0

    # ── public Transport API ────────────────────────────────────────

    def health(self):
        # Auto-spawn on first contact, same as call(): health is often the
        # FIRST rpc a fresh install makes, and without this it surfaced
        # "server.json not found — start the service" instead of just
        # starting it (Windows first-run, 2026-07-16).
        try:
            info = self._read_server_info()
        except ServerUnavailable:
            if not self._spawn_server():
                raise
            info = self._read_server_info()
        url = f'http://127.0.0.1:{info["port"]}/v1/health'
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, OSError) as e:
            if self._spawn_server():
                try:
                    info = self._read_server_info()
                    url = f'http://127.0.0.1:{info["port"]}/v1/health'
                    with urllib.request.urlopen(url, timeout=5) as resp:
                        return json.loads(resp.read())
                except Exception as e2:
                    raise ServerUnavailable(
                        f'health check failed after spawn: {e2}')
            raise ServerUnavailable(f'health check failed: {e}')

    def call(self, method, path, body=None, timeout=_DEFAULT_TIMEOUT):
        last_err = None
        saw_first_attempt = False
        for attempt in range(_MAX_ATTEMPTS):
            try:
                info = self._read_server_info()
            except ServerUnavailable as ex:
                last_err = ex
                outcome = ''
                if attempt < _MAX_ATTEMPTS - 1:
                    outcome = self._spawn_server()
                if outcome == 'wedged':
                    # A daemon is running and not answering. Retrying
                    # can't help and neither can spawning; fail with a
                    # message that names the actual remedy instead of
                    # "start the service", which is wrong and sends the
                    # user to start a second one.
                    raise ServerUnavailable(
                        'the collaboration service is running but not '
                        'responding — restart it (Restart server in '
                        'settings, or kill the pid in '
                        f'{server_info_path()})')
                if outcome:
                    if saw_first_attempt and outcome == 'spawned':
                        print('[azt_collab_client] SERVICE_RESTARTED '
                              '(server.json missing → spawned)')
                    continue
                raise
            try:
                saw_first_attempt = True
                return self._call_once(info, method, path, body, timeout)
            except (urllib.error.URLError, OSError) as ex:
                last_err = ex
                outcome = ''
                if attempt < _MAX_ATTEMPTS - 1:
                    outcome = self._spawn_server()
                if outcome == 'spawned':
                    print('[azt_collab_client] SERVICE_RESTARTED '
                          f'(connection failed: {ex}) — retrying')
                    continue
                if outcome == 'alive':
                    # The daemon is UP and answering /v1/health, so
                    # this is a busy/broken daemon, not a dead one.
                    # Distinguish HOW the call failed (0.54.88): a
                    # timeout means handlers are slow; a connection
                    # closed with no response means the daemon
                    # ACCEPTED and then dropped us — which points at
                    # resource exhaustion (handler threads piling up
                    # on blocked work until ``Thread.start()`` fails,
                    # or fds) rather than slowness. Calling that
                    # "timed out" sent the reader down the wrong
                    # path (field 2026-07-27).
                    if isinstance(ex, TimeoutError):
                        print('[azt_collab_client] SERVICE_SLOW '
                              f'(call timed out: {ex}) — daemon is '
                              f'alive; retrying')
                    else:
                        print('[azt_collab_client] SERVICE_DROPPED '
                              f'(daemon alive but dropped the call: '
                              f'{ex}) — retrying')
                    continue
                raise ServerUnavailable(f'connection failed: {ex}')
        raise ServerUnavailable(str(last_err))

    def close(self):
        # Loopback has no persistent connection or fds to release.
        pass

    # ── helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _read_server_info():
        path = server_info_path()
        try:
            with open(path) as f:
                info = json.load(f)
        except FileNotFoundError:
            raise ServerUnavailable(
                f'{path} not found. Start the service: '
                f'python -m azt_collabd')
        except Exception as ex:
            raise ServerUnavailable(f'cannot read {path}: {ex}')
        if not info.get('port') or not info.get('token'):
            raise ServerUnavailable(f'{path} missing port/token')
        return info

    @staticmethod
    def _pid_alive(pid):
        if not pid or not isinstance(pid, int):
            return True   # older server.json without pid → trust it
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True

    # A daemon rewrites ``server.json`` as it comes up, so a very recent
    # mtime means a restart just happened. Wide enough to cover a desktop
    # re-exec (new interpreter boot) without masking a daemon that has
    # been unresponsive for any length of time.
    _RESTART_GRACE_S = 20.0

    def _server_json_written_recently(self):
        """True iff ``server.json`` was rewritten within the restart
        grace window (0.55.86). Used only to choose WORDING — never to
        change whether we respawn or touch the file."""
        try:
            import time as _time
            return (_time.time() - os.path.getmtime(self._server_json_path())
                    ) < self._RESTART_GRACE_S
        except Exception:
            return False

    def _server_alive(self, info):
        if not self._pid_alive(info.get('pid')):
            return False
        url = f'http://127.0.0.1:{info["port"]}/v1/health'
        try:
            with urllib.request.urlopen(url, timeout=_HEALTH_TIMEOUT) as resp:
                return resp.status == 200
        except urllib.error.HTTPError:
            # The daemon ANSWERED, just not with 200 — it's alive
            # but degraded (e.g. fd exhaustion made a handler
            # raise). Treating that as dead is what deleted a live
            # daemon's server.json and manufactured the 2026-07-10
            # spawn storm; the spawned replacement can never take
            # the still-held server.lock anyway. Alive-but-degraded
            # is the daemon's problem to surface, not ours to
            # respawn over.
            return True
        except (urllib.error.URLError, OSError):
            return False

    @staticmethod
    def _autospawn_enabled():
        return os.environ.get('AZT_CLIENT_AUTOSPAWN', '1') != '0'

    def _spawn_server(self):
        """Ensure a daemon is reachable. Returns ``'alive'`` (one was
        already up — NOTHING was spawned), ``'spawned'`` (we launched
        one and it answered), or ``''`` (no daemon).

        The three-way return exists because callers were printing
        ``SERVICE_RESTARTED`` on a truthy result, and the
        already-alive path is truthy — so a call that merely TIMED OUT
        against a busy daemon reported a restart that never happened
        (field 2026-07-27: a dozen such lines in minutes while the
        daemon was wedged-but-answering-health). ``SERVICE_RESTARTED``
        is the canonical signal that a daemon actually restarted;
        spending it on "alive but slow" destroys the one thing it was
        good for. Truthiness is unchanged, so existing
        ``if self._spawn_server():`` callers keep working."""
        if not self._autospawn_enabled():
            return ''
        with self._spawn_lock:
            info = None
            try:
                info = self._read_server_info()
                if self._server_alive(info):
                    return 'alive'
            except ServerUnavailable:
                pass
            # ALIVE BUT NOT ANSWERING (0.54.95). ``_server_alive``
            # returns False for a health TIMEOUT as well as for a dead
            # process, and the next thing this function does is delete
            # ``server.json`` — so a wedged daemon got its discovery
            # file removed, the replacement exited on the still-held
            # ``server.lock``, and from then on every client reported
            # "server.json not found. Start the service" while the real
            # daemon was still running and holding the lock. The
            # 2026-07-10 fix covered the answered-non-200 case
            # (HTTPError → alive); a timeout took the destructive path.
            #
            # If the recorded pid is alive, there IS a daemon: don't
            # delete its discovery file and don't spawn a rival that
            # cannot take the lock. Say so instead — the remedy is a
            # restart of THAT process, which the caller can name.
            if info is not None and self._pid_alive(info.get('pid')):
                # A RESTART LOOKS EXACTLY LIKE A WEDGE FOR A SECOND OR
                # TWO (0.55.86). The daemon re-execs in place, so the pid
                # is unchanged and alive throughout, but there is a gap
                # between the old image stopping and the new one binding
                # its socket. Probed inside that gap the test above is
                # satisfied and we shout SERVICE_WEDGED at a daemon that
                # is merely mid-restart.
                #
                # Field 2026-07-29: pressing Restart produced two
                # SERVICE_WEDGED lines; ``ps`` then showed pid 1021991
                # with 1d12h elapsed (execv preserves both pid and start
                # time) running the NEW version, answering normally. The
                # message was alarming, accurate about the probe, and
                # wrong about the conclusion.
                #
                # ``server.json`` is rewritten by the daemon as it comes
                # up, so a fresh mtime means a restart just happened.
                # Stay quiet inside that window and let the caller retry;
                # the refusal to respawn or delete the file is unchanged
                # either way, so this only affects what we SAY.
                if self._server_json_written_recently():
                    print(f'[azt_collab_client] service not answering '
                          f'yet (pid {info.get("pid")} alive, '
                          f'server.json just rewritten) — looks like a '
                          f'restart in progress, not a wedge; retrying',
                          file=sys.stderr, flush=True)
                    return 'wedged'
                print(f'[azt_collab_client] SERVICE_WEDGED (pid '
                      f'{info.get("pid")} is running but not answering '
                      f'/v1/health) — not respawning, not touching '
                      f'server.json; restart that process',
                      file=sys.stderr, flush=True)
                return 'wedged'
            # Cooldown: a spawn that just failed (usually because a
            # wedged-but-alive daemon still holds server.lock) will
            # fail again immediately; don't burn a process per poll.
            now = time.time()
            if now - self._last_failed_spawn < _SPAWN_COOLDOWN_S:
                return False
            try:
                os.remove(server_info_path())
            except OSError:
                pass
            try:
                kwargs = {
                    'stdout': subprocess.DEVNULL,
                    'stderr': subprocess.DEVNULL,
                    'stdin': subprocess.DEVNULL,
                    'close_fds': True,
                    'env': build_spawn_env(),
                }
                if hasattr(os, 'setsid'):
                    kwargs['start_new_session'] = True
                elif sys.platform == 'win32':
                    # CREATE_NO_WINDOW, NOT DETACHED_PROCESS: both detach
                    # from the parent's console/Ctrl+C, but DETACHED gives
                    # the daemon NO console — so every console-subsystem
                    # child IT spawns allocated a new visible one (blank
                    # windows popping during pair/clone, 2026-07-17).
                    # NO_WINDOW gives an invisible console children inherit.
                    kwargs['creationflags'] = (
                        subprocess.CREATE_NO_WINDOW
                        | subprocess.CREATE_NEW_PROCESS_GROUP)
                subprocess.Popen(
                    [sys.executable, '-m', 'azt_collabd'], **kwargs)
            except OSError as ex:
                print(f'[azt_collab_client] spawn failed: {ex}')
                return ''
            deadline = time.time() + _SPAWN_WAIT
            while time.time() < deadline:
                try:
                    info = self._read_server_info()
                    if self._server_alive(info):
                        return 'spawned'
                except ServerUnavailable:
                    pass
                time.sleep(0.1)
            self._last_failed_spawn = time.time()
            return ''

    @staticmethod
    def _call_once(info, method, path, body, timeout):
        url = f'http://127.0.0.1:{info["port"]}{path}'
        headers = {'Authorization': f'Bearer {info["token"]}'}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return json.loads(raw)
            except Exception:
                raise ServerUnavailable(
                    f'HTTP {e.code}: {raw[:200]!r}')
        return json.loads(raw)
