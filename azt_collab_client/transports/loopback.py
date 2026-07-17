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
                if attempt < _MAX_ATTEMPTS - 1 and self._spawn_server():
                    if saw_first_attempt:
                        print('[azt_collab_client] SERVICE_RESTARTED '
                              '(server.json missing → spawned)')
                    continue
                raise
            try:
                saw_first_attempt = True
                return self._call_once(info, method, path, body, timeout)
            except (urllib.error.URLError, OSError) as ex:
                last_err = ex
                if attempt < _MAX_ATTEMPTS - 1 and self._spawn_server():
                    print('[azt_collab_client] SERVICE_RESTARTED '
                          f'(connection failed: {ex}) — retrying')
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
        if not self._autospawn_enabled():
            return False
        with self._spawn_lock:
            try:
                if self._server_alive(self._read_server_info()):
                    return True
            except ServerUnavailable:
                pass
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
                    # Detach: without these the daemon shares the parent's
                    # console/process group, dying with the console window
                    # or a stray Ctrl+C (2026-07-16).
                    kwargs['creationflags'] = (
                        subprocess.DETACHED_PROCESS
                        | subprocess.CREATE_NEW_PROCESS_GROUP)
                subprocess.Popen(
                    [sys.executable, '-m', 'azt_collabd'], **kwargs)
            except OSError as ex:
                print(f'[azt_collab_client] spawn failed: {ex}')
                return False
            deadline = time.time() + _SPAWN_WAIT
            while time.time() < deadline:
                try:
                    info = self._read_server_info()
                    if self._server_alive(info):
                        return True
                except ServerUnavailable:
                    pass
                time.sleep(0.1)
            self._last_failed_spawn = time.time()
            return False

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
