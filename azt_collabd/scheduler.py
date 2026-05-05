"""
Async sync scheduler.

Three responsibilities:

1. **Debounced job queue.** ``request_sync(langcode, contributor)``
   schedules a sync to run after ``settings.debounce_ms``. Subsequent
   calls within the window reset the timer (trailing-edge debounce) so
   bursts of rapid edits — recording a clip writes both the .wav and
   the .lift — collapse into one commit/push.

2. **Connectivity watcher.** A background thread polls
   ``net._has_internet`` every ``settings.connectivity_poll_s``. On
   the offline → online edge, projects flagged ``pending_push`` get
   re-synced.

3. **Disk-persisted job table.** Jobs are mirrored to
   ``$AZT_HOME/jobs.json`` at every state transition so ``poll_job``
   from a peer still works after the daemon was killed (Android OOM,
   ``kill -9``, container restart) and respawned by the next client
   call. ``reconcile_on_startup`` is called from the daemon's startup
   hook and flips any ``PENDING`` / ``RUNNING`` entries it finds to
   ``DONE`` + ``JOB_INTERRUPTED`` — their worker threads died with
   the previous process and will not be resumed. Old ``DONE`` entries
   are GC'd past ``_GC_AGE_SECONDS`` at the same pass.

Jobs are remembered in a process-local dict keyed by ``job_id`` so
clients can poll status. Old jobs are pruned past _MAX_JOBS.
"""

import json
import os
import sys
import threading
import time
import uuid
from collections import OrderedDict

from . import projects
from . import settings as _settings
from . import status as S
from .net import _has_internet
from .paths import azt_home
from .repo import sync_repo as _sync_repo
from .status import Result, Status
from .store import get_sync_credentials


_MAX_JOBS = 100
_GC_AGE_SECONDS = 3600


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
            'contributor': self.contributor,
            'state': self.state,
            'result': self.result.to_dict() if self.result else None,
            'created_at': self.created_at,
            'started_at': self.started_at,
            'finished_at': self.finished_at,
        }

    @classmethod
    def from_dict(cls, d):
        j = cls.__new__(cls)
        j.id = d.get('job_id', '') or uuid.uuid4().hex[:12]
        j.langcode = d.get('langcode', '')
        j.contributor = d.get('contributor', '')
        j.state = d.get('state', JobState.PENDING)
        raw_result = d.get('result')
        j.result = Result.from_dict(raw_result) if raw_result else None
        j.created_at = float(d.get('created_at', 0.0) or 0.0)
        j.started_at = float(d.get('started_at', 0.0) or 0.0)
        j.finished_at = float(d.get('finished_at', 0.0) or 0.0)
        return j


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
        _persist_locked()


# ── Persistence ─────────────────────────────────────────────────────────────

def _jobs_path():
    return os.path.join(azt_home(), 'jobs.json')


def _persist_locked():
    """Caller must hold _lock. Atomically writes _jobs to
    $AZT_HOME/jobs.json. Best-effort: an IO failure logs and falls
    through, never raises into a worker thread."""
    try:
        path = _jobs_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        payload = {'jobs': [j.to_dict() for j in _jobs.values()]}
        with open(tmp, 'w') as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except Exception as ex:
        print(f'[scheduler] persist failed: {ex}')


def _load_persisted_locked():
    """Caller must hold _lock. Read jobs.json into an OrderedDict
    keyed by job_id. Empty on missing/unreadable file."""
    out = OrderedDict()
    path = _jobs_path()
    try:
        with open(path) as f:
            payload = json.load(f)
    except FileNotFoundError:
        return out
    except Exception as ex:
        print(f'[scheduler] load failed: {ex}')
        return out
    for raw in payload.get('jobs', []):
        try:
            j = Job.from_dict(raw)
        except Exception as ex:
            print(f'[scheduler] skipping malformed job entry: {ex}')
            continue
        if j.id:
            out[j.id] = j
    return out


def reconcile_on_startup():
    """Daemon-startup hook. Reads jobs.json, marks PENDING/RUNNING
    entries as DONE + JOB_INTERRUPTED (their worker threads died with
    the previous daemon process), and GC's DONE entries older than
    _GC_AGE_SECONDS. Called from the loopback HTTP startup path
    (azt_collabd.server.run) and the Android service entry
    (server_apk/service.py). Idempotent — re-running on a daemon
    that's already done one pass is a no-op (everything is already
    DONE)."""
    with _lock:
        loaded = _load_persisted_locked()
        now = time.time()
        interrupted = 0
        for job in loaded.values():
            if job.state in (JobState.PENDING, JobState.RUNNING):
                prev = job.state
                job.state = JobState.DONE
                job.result = Result(statuses=[Status(
                    code=S.JOB_INTERRUPTED,
                    params={'kind': 'sync',
                            'langcode': job.langcode,
                            'previous_state': prev})])
                job.finished_at = now
                interrupted += 1
        # GC ancient DONE entries
        stale = [k for k, v in loaded.items()
                 if v.state == JobState.DONE
                 and v.finished_at
                 and (now - v.finished_at) > _GC_AGE_SECONDS]
        for k in stale:
            loaded.pop(k)
        _jobs.clear()
        _jobs.update(loaded)
        _persist_locked()
        if interrupted or stale:
            print(f'[scheduler] reconcile_on_startup: '
                  f'interrupted={interrupted} gc={len(stale)}', flush=True)


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
    with _lock:
        job.state = JobState.RUNNING
        job.started_at = time.time()
        _persist_locked()

    try:
        result = _run_sync(job.langcode, job.contributor)
    except Exception as ex:
        result = Result().add(S.PUSH_FAILED, error=str(ex))
    with _lock:
        job.result = result
        job.state = JobState.DONE
        job.finished_at = time.time()
        _persist_locked()

    codes = result.codes()
    if 'PUSHED' in codes or 'COMMITTED_AND_PUSHED' in codes:
        _set_pending_push(langcode, False)
    elif 'COMMITTED_OFFLINE' in codes or 'COMMITTED_NO_REMOTE' in codes \
            or 'COMMITTED_LOCAL' in codes:
        _set_pending_push(langcode, True)


def _run_sync(langcode, contributor):
    from . import store as _store
    contributor = _store.resolve_contributor(contributor)
    # Async sync runs from a worker thread well after the request_sync
    # call returned, so the request-time _touch_project on the server
    # handler has already fired. We re-touch here just to keep the
    # invariant "every sync attempt marks the project recent" — the
    # write is cheap and idempotent.
    _store.set_last_langcode(langcode)
    print(f'[sync] {langcode!r} contributor={contributor!r} starting',
          file=sys.stderr, flush=True)
    p = projects.get(langcode)
    if p is None:
        print(f'[sync] {langcode!r} → NO_REPO',
              file=sys.stderr, flush=True)
        return Result().add(S.NO_REPO)
    git_user, token = get_sync_credentials(p.remote_url)
    if not token:
        print(f'[sync] {langcode!r} → AUTH_REQUIRED (no token for '
              f'remote_url={p.remote_url!r})',
              file=sys.stderr, flush=True)
        return Result().add(S.AUTH_REQUIRED)
    if not _has_internet():
        print(f'[sync] {langcode!r} → COMMITTED_OFFLINE',
              file=sys.stderr, flush=True)
        return Result().add(S.COMMITTED_OFFLINE)
    res = _sync_repo(p.working_dir, git_user, token, contributor)
    codes = res.codes()
    print(f'[sync] {langcode!r} done: codes={codes!r}',
          file=sys.stderr, flush=True)
    if 'PUSHED' in codes or 'PULLED' in codes \
            or 'COMMITTED_AND_PUSHED' in codes:
        projects.set_last_sync(langcode)
    if ('COMMITTED_LOCAL' in codes or 'COMMITTED_NO_REMOTE' in codes
            or 'COMMITTED_AND_PUSHED' in codes):
        projects.set_last_commit(langcode)
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
