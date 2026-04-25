"""
Advisory file locks, per project working_dir.

Serializes dulwich mutations across concurrent requests (two clients
triggering sync at once, or the scheduler firing while a manual sync is
in flight). Re-entrant within a single process so helpers that delegate
to other locked ops (e.g., ``commit_audio_and_sync`` → ``sync_repo``)
don't deadlock against themselves.

On POSIX we use ``fcntl.flock``. On platforms without ``fcntl`` the
cross-process guarantee is lost but the in-process threading lock still
prevents two threads from stepping on each other.
"""

import errno
import hashlib
import os
import threading
import time
from contextlib import contextmanager

try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None

from .paths import azt_home


DEFAULT_TIMEOUT = 10.0
POLL_INTERVAL = 0.1


class LockTimeout(Exception):
    """Raised by ``project_lock`` when the advisory lock cannot be
    acquired before the timeout expires."""


class _ReentrantFileLock:
    """Per-path reentrant lock. First acquire takes both a threading
    RLock and (on POSIX) a fcntl.flock on a file under
    ``$AZT_HOME/locks/``. Nested acquires from the same thread only
    bump a depth counter."""

    def __init__(self, path):
        self._path = path
        self._rlock = threading.RLock()
        self._fd = None
        self._depth = 0

    def acquire(self, timeout):
        deadline = time.time() + max(0.0, timeout)
        remaining = max(0.0, deadline - time.time())
        if not self._rlock.acquire(timeout=remaining):
            raise LockTimeout(f'lock busy (in-process): {self._path}')
        if self._depth == 0:
            try:
                self._acquire_flock(deadline)
            except BaseException:
                self._rlock.release()
                raise
        self._depth += 1

    def _acquire_flock(self, deadline):
        if _fcntl is None:
            self._fd = None
            return
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            while True:
                try:
                    _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                    break
                except OSError as e:
                    if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                        raise
                    if time.time() >= deadline:
                        raise LockTimeout(
                            f'lock busy (cross-process): {self._path}')
                    time.sleep(POLL_INTERVAL)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        # Record holder for debugging / diagnostics
        try:
            os.truncate(fd, 0)
            os.write(fd, f'pid={os.getpid()} ts={time.time()}\n'.encode())
        except OSError:
            pass

    def release(self):
        self._depth -= 1
        if self._depth == 0:
            if self._fd is not None and _fcntl is not None:
                try:
                    _fcntl.flock(self._fd, _fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    os.close(self._fd)
                except Exception:
                    pass
                self._fd = None
        self._rlock.release()


_registry_lock = threading.Lock()
_locks: dict = {}


def _lock_path(working_dir):
    abs_path = os.path.abspath(working_dir)
    h = hashlib.sha1(abs_path.encode()).hexdigest()[:16]
    d = os.path.join(azt_home(), 'locks')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f'{h}.lock')


def _get_lock(working_dir):
    p = _lock_path(working_dir)
    with _registry_lock:
        lock = _locks.get(p)
        if lock is None:
            lock = _ReentrantFileLock(p)
            _locks[p] = lock
        return lock


@contextmanager
def project_lock(working_dir, timeout=None):
    """Acquire the advisory lock for *working_dir*. Re-entrant within the
    same process. Raises ``LockTimeout`` if busy beyond *timeout*
    seconds (defaults to the current ``locks.DEFAULT_TIMEOUT``, so
    tests / future config can tune it at runtime)."""
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    lock = _get_lock(working_dir)
    lock.acquire(timeout)
    try:
        yield
    finally:
        lock.release()
