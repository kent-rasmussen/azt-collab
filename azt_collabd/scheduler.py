"""
Async sync scheduler.

Two responsibilities:

1. **Debounced job queue.** ``request_sync(langcode, contributor)``
   schedules a sync to run after ``settings.debounce_ms``. Subsequent
   calls within the window reset the timer (trailing-edge debounce) so
   bursts of rapid edits — recording a clip writes both the .wav and
   the .lift — collapse into one commit/push.

2. **Connectivity watcher.** A background thread polls
   ``net._has_internet`` every ``settings.connectivity_poll_s``. On
   the offline → online edge, projects flagged ``pending_push`` get
   re-synced.

Jobs are remembered in a process-local dict keyed by ``job_id`` so
clients can poll status. Old jobs are pruned past _MAX_JOBS.
"""

import threading
import time
import uuid
from collections import OrderedDict

from . import projects
from . import settings as _settings
from . import status as S
from .net import _has_internet
from .repo import sync_repo as _sync_repo
from .status import Result, Status
from .store import get_sync_credentials


_MAX_JOBS = 100


# ── Job table ───────────────────────────────────────────────────────────────

class JobState:
    PENDING = 'PENDING'
    RUNNING = 'RUNNING'
    DONE = 'DONE'


class Job:
    __slots__ = ('id', 'langcode', 'contributor', 'state', 'result',
                 'created_at', 'started_at', 'finished_at')

    def __init__(self, langcode, contributor):
        self.id = uuid.uuid4().hex[:12]
        self.langcode = langcode
        self.contributor = contributor
        self.state = JobState.PENDING
        self.result = None
        self.created_at = time.time()
        self.started_at = 0.0
        self.finished_at = 0.0

    def to_dict(self):
        return {
            'job_id': self.id,
            'langcode': self.langcode,
            'state': self.state,
            'result': self.result.to_dict() if self.result else None,
            'created_at': self.created_at,
            'started_at': self.started_at,
            'finished_at': self.finished_at,
        }


# ── Scheduler state ─────────────────────────────────────────────────────────

_lock = threading.RLock()
# debounce timers and pending jobs keyed by langcode
_pending_timers: dict = {}     # langcode → threading.Timer
_pending_jobs: dict = {}       # langcode → Job (the next to run)
_jobs: "OrderedDict[str, Job]" = OrderedDict()

# connectivity watcher
_watcher_thread = None
_watcher_stop = None
_last_online_state = None      # None until first probe


def _store_job(job):
    with _lock:
        _jobs[job.id] = job
        # prune
        while len(_jobs) > _MAX_JOBS:
            _jobs.popitem(last=False)


def _set_pending_push(langcode, value):
    """Mark/clear a project's pending_push state in projects.json."""
    try:
        data = projects._load_raw()  # internal but stable enough for now
        if langcode in data:
            entry = dict(data[langcode])
            if value:
                entry['pending_push'] = True
            else:
                entry.pop('pending_push', None)
            data[langcode] = entry
            projects._save_raw(data)
    except Exception as ex:
        print(f'[scheduler] pending_push update failed: {ex}')


# ── Public API ──────────────────────────────────────────────────────────────

def request_sync(langcode, contributor):
    """Schedule a debounced sync for *langcode*. Returns the job id of
    the eventual run (the same id is returned for subsequent calls
    within the debounce window — the timer just resets)."""
    debounce_s = _settings.debounce_ms() / 1000.0
    with _lock:
        existing_timer = _pending_timers.pop(langcode, None)
        if existing_timer is not None:
            existing_timer.cancel()
        job = _pending_jobs.get(langcode)
        if job is None:
            job = Job(langcode, contributor)
            _pending_jobs[langcode] = job
            _store_job(job)
        else:
            # Latest contributor name wins (last-call-wins debounce)
            job.contributor = contributor
        if debounce_s <= 0:
            # Run immediately on a worker thread so request_sync stays
            # non-blocking for the caller.
            t = threading.Thread(
                target=_fire, args=(langcode,), daemon=True)
            t.start()
        else:
            t = threading.Timer(
                debounce_s, _fire, args=(langcode,))
            t.daemon = True
            _pending_timers[langcode] = t
            t.start()
        return job.id


def _fire(langcode):
    with _lock:
        _pending_timers.pop(langcode, None)
        job = _pending_jobs.pop(langcode, None)
    if job is None:
        return
    job.state = JobState.RUNNING
    job.started_at = time.time()

    try:
        result = _run_sync(job.langcode, job.contributor)
    except Exception as ex:
        result = Result().add(S.PUSH_FAILED, error=str(ex))
    job.result = result
    job.state = JobState.DONE
    job.finished_at = time.time()

    codes = result.codes()
    if 'PUSHED' in codes or 'COMMITTED_AND_PUSHED' in codes:
        _set_pending_push(langcode, False)
    elif 'COMMITTED_OFFLINE' in codes or 'COMMITTED_NO_REMOTE' in codes \
            or 'COMMITTED_LOCAL' in codes:
        _set_pending_push(langcode, True)


def _run_sync(langcode, contributor):
    from . import store as _store
    contributor = _store.resolve_contributor(contributor)
    p = projects.get(langcode)
    if p is None:
        return Result().add(S.NO_REPO)
    git_user, token = get_sync_credentials(p.remote_url)
    if not token:
        return Result().add(S.AUTH_REQUIRED)
    if not _has_internet():
        return Result().add(S.COMMITTED_OFFLINE)
    res = _sync_repo(p.working_dir, git_user, token, contributor)
    codes = res.codes()
    if 'PUSHED' in codes or 'PULLED' in codes \
            or 'COMMITTED_AND_PUSHED' in codes:
        projects.set_last_sync(langcode)
    return res


def get_job(job_id):
    with _lock:
        return _jobs.get(job_id)


# ── Connectivity watcher ────────────────────────────────────────────────────

def start_watcher():
    """Start the offline→online watcher. Idempotent."""
    global _watcher_thread, _watcher_stop, _last_online_state
    if _watcher_thread is not None and _watcher_thread.is_alive():
        return
    _watcher_stop = threading.Event()
    _last_online_state = None
    _watcher_thread = threading.Thread(
        target=_watcher_loop, daemon=True, name='azt_collabd-watcher')
    _watcher_thread.start()


def stop_watcher():
    if _watcher_stop is not None:
        _watcher_stop.set()


def _watcher_loop():
    global _last_online_state
    while _watcher_stop is not None and not _watcher_stop.is_set():
        try:
            online = _has_internet()
        except Exception:
            online = False
        prev = _last_online_state
        _last_online_state = online
        # Offline → online edge: drain pending_push projects
        if prev is False and online is True:
            _drain_pending_push()
        # Sleep with periodic checks of the stop event
        interval = max(5.0, float(_settings.connectivity_poll_s()))
        if _watcher_stop.wait(timeout=interval):
            break


def _drain_pending_push():
    try:
        data = projects._load_raw()
    except Exception:
        return
    for langcode, entry in list(data.items()):
        if entry.get('pending_push'):
            request_sync(langcode, 'Recorder')
