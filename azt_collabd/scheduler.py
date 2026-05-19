"""
Async commit scheduler + push drain loop.

Four responsibilities:

1. **Debounced commit queue.** ``commit_project(langcode)``
   schedules a commit to run after ``settings.debounce_ms``.
   Subsequent calls within the window reset the timer (trailing-edge
   debounce) so bursts of rapid edits — recording a clip writes both
   the .wav and the .lift — collapse into one commit. As of 0.43.0
   the queue no longer attempts push; push is driven entirely by the
   connectivity watcher's drain loop based on online state +
   post-online grace + work_offline. Peers call ``commit_project``
   per group of related changes; the daemon decides when to push.

2. **Connectivity watcher.** A background thread polls
   ``net._has_internet`` every ``settings.connectivity_poll_s``. On
   the offline → online edge a timestamp is recorded; the drain only
   fires once ``now - online_since >= settings.post_online_grace_s``
   so brief tethers the user enabled for some other reason don't
   immediately burn their MB on pending pushes.

3. **Push drain loop.** Every watcher tick, if online for >= grace
   AND ``sync.work_offline`` is False, projects with unpushed
   commits get pushed. Stuck-commit retry continues to run on every
   tick (no online gate — the commit half is local).

4. **Disk-persisted job table.** Jobs are mirrored to
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
from .repo import (
    commit_repo as _commit_repo,
    push_repo as _push_repo,
    sync_repo as _sync_repo,
)
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
    # As of 0.40.0 Job no longer carries a contributor name: the
    # daemon resolves contributor from store at exec time (sole-
    # authoritative-source rule). Pre-0.40 jobs.json entries that
    # still have a ``contributor`` field decode cleanly — we ignore
    # it on load and stop writing it on save.
    __slots__ = ('id', 'langcode', 'state', 'result',
                 'created_at', 'started_at', 'finished_at')

    def __init__(self, langcode):
        self.id = uuid.uuid4().hex[:12]
        self.langcode = langcode
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

    @classmethod
    def from_dict(cls, d):
        j = cls.__new__(cls)
        j.id = d.get('job_id', '') or uuid.uuid4().hex[:12]
        j.langcode = d.get('langcode', '')
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
# Wall-clock seconds at which we last observed an offline → online
# transition (or watcher startup with online == True). Reset to
# None when we go offline. The push-drain loop gates on
# now - _online_since >= settings.post_online_grace_s so a brief
# blip doesn't immediately fire pending pushes.
_online_since = None


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

def commit_project(langcode):
    """Schedule a debounced commit for *langcode*. Returns the
    job id of the eventual run (the same id is returned for
    subsequent calls within the debounce window — the timer just
    resets).

    Commit-only: does NOT attempt push. Push is driven by the
    connectivity watcher's drain loop (online + post-online grace
    + work_offline=False). Peers call this per group of related
    changes; the daemon decides when to push. Pre-0.43 this was
    ``request_sync`` and did both halves.

    Contributor is read from ``store.get_contributor()`` at exec
    time. Unset contributor → the job result carries
    ``S.CONTRIBUTOR_UNSET``; peers poll via ``poll_job(job_id)``."""
    debounce_s = _settings.debounce_ms() / 1000.0
    with _lock:
        existing_timer = _pending_timers.pop(langcode, None)
        if existing_timer is not None:
            existing_timer.cancel()
        job = _pending_jobs.get(langcode)
        if job is None:
            job = Job(langcode)
            _pending_jobs[langcode] = job
            _store_job(job)
        print(f'[commit-debounce] {langcode!r} debounce={debounce_s}s '
              f'job_id={job.id!r}',
              file=sys.stderr, flush=True)
        if debounce_s <= 0:
            # Run immediately on a worker thread so commit_project
            # stays non-blocking for the caller. Named so a crash
            # backtrace in this thread identifies the commit flow.
            t = threading.Thread(
                target=_fire, args=(langcode,), daemon=True,
                name=f'commit-fire-{langcode}')
            t.start()
        else:
            t = threading.Timer(
                debounce_s, _fire, args=(langcode,))
            t.daemon = True
            t.name = f'commit-fire-{langcode}'
            _pending_timers[langcode] = t
            t.start()
        return job.id


def _fire(langcode):
    with _lock:
        _pending_timers.pop(langcode, None)
        job = _pending_jobs.pop(langcode, None)
    if job is None:
        print(f'[commit-fire] {langcode!r} debounce timer fired but '
              f'no pending job — already drained?',
              file=sys.stderr, flush=True)
        return
    print(f'[commit-fire] {langcode!r} debounce timer fired, '
          f'running job_id={job.id!r}',
          file=sys.stderr, flush=True)
    with _lock:
        job.state = JobState.RUNNING
        job.started_at = time.time()
        _persist_locked()

    try:
        result = _run_commit(job.langcode)
    except Exception as ex:
        result = Result().add(S.COMMIT_FAILED, error=str(ex))
    with _lock:
        job.result = result
        job.state = JobState.DONE
        job.finished_at = time.time()
        _persist_locked()

    codes = result.codes()
    # commit_project does not push. Any successful commit means
    # there's now (or already was) a local commit waiting for the
    # drain loop to push. NOTHING_TO_COMMIT means the index was
    # clean — leave pending_push as-is (a prior commit may still
    # be waiting; the drain loop already knows from commits_ahead).
    if 'COMMITTED_LOCAL' in codes:
        _set_pending_push(langcode, True)


def _run_commit(langcode):
    """Debounced-commit worker. Reads contributor at exec time,
    invokes commit_repo, stamps last_commit on success. No network
    activity — push is the drain loop's job."""
    from . import store as _store
    contributor = _store.get_contributor()
    if not contributor:
        print(f'[commit] {langcode!r} → CONTRIBUTOR_UNSET '
              f'(refused at exec time)',
              file=sys.stderr, flush=True)
        return Result().add(S.CONTRIBUTOR_UNSET)
    # Re-touch project as recent — cheap, idempotent, and keeps the
    # invariant "every commit attempt marks the project recent".
    _store.set_last_langcode(langcode)
    print(f'[commit] {langcode!r} contributor={contributor!r} starting',
          file=sys.stderr, flush=True)
    p = projects.get(langcode)
    if p is None:
        print(f'[commit] {langcode!r} → NO_REPO',
              file=sys.stderr, flush=True)
        return Result().add(S.NO_REPO)
    res = _commit_repo(p.working_dir, contributor)
    codes = res.codes()
    print(f'[commit] {langcode!r} done: codes={codes!r}',
          file=sys.stderr, flush=True)
    if 'COMMITTED_LOCAL' in codes:
        projects.set_last_commit(langcode)
    return res


def get_job(job_id):
    with _lock:
        return _jobs.get(job_id)


# ── Connectivity watcher ────────────────────────────────────────────────────

def start_watcher():
    """Start the offline→online watcher. Idempotent."""
    global _watcher_thread, _watcher_stop, _last_online_state, _online_since
    if _watcher_thread is not None and _watcher_thread.is_alive():
        return
    _watcher_stop = threading.Event()
    _last_online_state = None
    _online_since = None
    _watcher_thread = threading.Thread(
        target=_watcher_loop, daemon=True, name='azt_collabd-watcher')
    _watcher_thread.start()


def stop_watcher():
    if _watcher_stop is not None:
        _watcher_stop.set()


def is_online_cached():
    """Return the watcher's most recent online observation, or
    None if it hasn't probed yet. Cheap: a module-global read.
    Up to ``settings.connectivity_poll_s`` seconds stale; callers
    that need a fresh probe should call ``_has_internet`` directly
    (paying the 3–6 s TCP-timeout cost on offline networks).
    """
    return _last_online_state


def drain_pushes_now():
    """Public entry point: fire a push-drain pass immediately,
    bypassing the post-online grace gate. Called when the user
    toggles work_offline OFF — they just expressed intent to push,
    so waiting for the next watcher tick is the wrong UX.

    Respects work_offline (no-op if still on) and online state
    (no-op if offline)."""
    if _settings.work_offline():
        return
    if not _has_internet():
        return
    try:
        _drain_pending_push()
    except Exception as ex:
        print(f'[scheduler] drain_pushes_now failed: {ex}',
              file=sys.stderr, flush=True)


def _watcher_loop():
    global _last_online_state, _online_since
    while _watcher_stop is not None and not _watcher_stop.is_set():
        try:
            online = _has_internet()
        except Exception:
            online = False
        prev = _last_online_state
        _last_online_state = online
        now = time.time()
        # Track offline → online edges with a timestamp so the
        # push drain can enforce a grace period before firing.
        if online and prev is not True:
            _online_since = now
        elif not online:
            _online_since = None
        # On offline → online edge, nudge CAWL to retry any prefetch
        # that was offline-skipped or circuit-broken while we were
        # offline. Push drain has its own gate (grace + work_offline)
        # and runs every tick, not just on edges.
        if prev is False and online is True:
            # Log which resolver path served the probe — useful when
            # debugging field reports of "browser works but sync
            # doesn't". 'system' on the healthy path; 'doh' when the
            # ``net.py`` DoH fallback is what got us through; 'fail'
            # means we somehow recorded online despite both failing
            # (shouldn't happen via the probe — a sanity tag).
            try:
                from . import net as _net
                print(f'[watcher] online edge — resolver path: '
                      f'{_net.resolver_state()}',
                      file=sys.stderr, flush=True)
            except Exception:
                pass
            try:
                from . import cawl as _cawl
                _cawl.on_online_edge()
            except Exception as ex:
                print(f'[cawl] on_online_edge dispatch failed: {ex}',
                      file=sys.stderr, flush=True)
        # Push drain: every tick, gated by online + post-online
        # grace + work_offline. The grace gate avoids burning the
        # user's MB if they enabled a brief tether for some other
        # reason.
        if online and not _settings.work_offline():
            grace = _settings.post_online_grace_s()
            if _online_since is not None and (now - _online_since) >= grace:
                try:
                    _drain_pending_push()
                except Exception as ex:
                    print(f'[scheduler] _drain_pending_push failed: {ex}',
                          file=sys.stderr, flush=True)
        # Every tick (not just on edges): retry stuck commits with
        # exponential backoff so an idle device discovers a
        # persistent failure without needing the user to gesture
        # the peer. The drain itself enforces the per-project
        # backoff and no-ops projects that aren't stuck. Local-
        # only — no network required.
        try:
            _drain_stuck_commits()
        except Exception as ex:
            print(f'[scheduler] _drain_stuck_commits failed: {ex}',
                  file=sys.stderr, flush=True)
        # Every tick: walk each project's .azt_atomic_pending/ for
        # orphans (atomic_open_write Phase 1 completed, Phase 2
        # never ran). Auto-merge recoverable orphans into the
        # current LIFT; delete confirmed-garbage orphans; stash
        # unmergeable orphans for manual inspection. No-op when
        # no orphans are present (single os.listdir on a
        # typically-empty directory).
        try:
            _drain_atomic_orphans()
        except Exception as ex:
            print(f'[scheduler] _drain_atomic_orphans failed: {ex}',
                  file=sys.stderr, flush=True)
        # Sleep with periodic checks of the stop event
        interval = max(5.0, float(_settings.connectivity_poll_s()))
        if _watcher_stop.wait(timeout=interval):
            break


# Doubling backoff for stuck-commit retry, capped at 1 hour. Base
# pegged to ``connectivity_poll_s`` so retries align with the
# watcher tick: first retry ~30 s after the failure, then 60,
# 120, 240, … s. After roughly half an hour of failing we settle
# at one retry per hour — enough to catch a self-healing
# condition (disk freed, lock released, daemon restarted) without
# spamming the log forever.
_STUCK_COMMIT_BACKOFF_CAP_S = 3600


def _stuck_commit_backoff_s(count):
    base = max(5.0, float(_settings.connectivity_poll_s()))
    # 2 ** (count-1): 1, 2, 4, 8, … ; clamp the exponent to keep
    # the product in int range for very large stuck counts.
    shift = min(max(int(count) - 1, 0), 20)
    return min(base * (1 << shift), _STUCK_COMMIT_BACKOFF_CAP_S)


def _drain_atomic_orphans():
    """Walk every registered project for orphaned
    ``.azt_atomic_pending/<token>`` LIFT scratches and dispose
    of them per the contract in ``atomic_recovery.py``. Cheap
    when nothing is pending (one ``os.listdir`` on an empty
    directory). Logs each non-trivial outcome."""
    from . import atomic_recovery
    try:
        data = projects._load_raw()
    except Exception:
        return
    for langcode, entry in list(data.items()):
        working_dir = entry.get('working_dir') or ''
        lift_path = entry.get('lift_path') or ''
        if not working_dir or not lift_path:
            continue
        try:
            summary = atomic_recovery.recover_project_orphans(
                working_dir, lift_path, langcode)
        except Exception as ex:
            print(f'[scheduler] recover_project_orphans '
                  f'{langcode!r} raised: {ex!r}',
                  file=sys.stderr, flush=True)
            continue
        if summary.get('recovered') or summary.get('unmergeable') \
                or summary.get('errors'):
            print(f'[scheduler] atomic orphans {langcode!r}: '
                  f'{summary!r}',
                  file=sys.stderr, flush=True)


def _drain_stuck_commits():
    """Find any project with ``commit_failure_count >= 1`` whose
    backoff window has elapsed and re-attempt the commit. Local-
    only — push will happen on the next drain pass when conditions
    allow. ``commit_repo`` handles the stage + commit and the
    failure-counter bookkeeping in repo.py.
    """
    try:
        data = projects._load_raw()
    except Exception:
        return
    now = time.time()
    candidates = []
    for langcode, entry in data.items():
        count = int(entry.get('commit_failure_count', 0) or 0)
        if count < 1:
            continue
        last = float(entry.get('last_commit_failure_at', 0.0) or 0.0)
        if (now - last) < _stuck_commit_backoff_s(count):
            continue
        candidates.append(langcode)
    if not candidates:
        return
    print(f'[scheduler] retry stuck commits: '
          f'{candidates!r}', file=sys.stderr, flush=True)
    from . import store as _store
    contributor = _store.get_contributor()
    if not contributor:
        # No contributor yet — every retry would emit
        # CONTRIBUTOR_UNSET. Wait until the user sets one (a
        # subsequent gesture will trigger the user-visible
        # refusal explicitly).
        return
    for langcode in candidates:
        p = projects.get(langcode)
        if p is None:
            continue
        try:
            res = _commit_repo(p.working_dir, contributor)
        except Exception as ex:
            print(f'[scheduler] retry stuck commit {langcode!r} '
                  f'raised: {ex!r}', file=sys.stderr, flush=True)
            continue
        codes = res.codes()
        print(f'[scheduler] retry stuck commit {langcode!r} '
              f'codes={codes!r}', file=sys.stderr, flush=True)
        if 'COMMITTED_LOCAL' in codes:
            _set_pending_push(langcode, True)


def _drain_pending_push():
    """Push any project flagged ``pending_push`` (or with local
    commits ahead of remote). Called every watcher tick from
    ``_watcher_loop`` once the post-online grace has elapsed and
    ``sync.work_offline`` is off. Skips projects with no
    contributor / no credentials / no remote — they'll get
    surfaced through a future user gesture instead."""
    try:
        data = projects._load_raw()
    except Exception:
        return
    candidates = [lang for lang, entry in data.items()
                  if entry.get('pending_push')]
    if not candidates:
        return
    print(f'[scheduler] drain pushes: {candidates!r}',
          file=sys.stderr, flush=True)
    for langcode in candidates:
        p = projects.get(langcode)
        if p is None:
            continue
        git_user, token = get_sync_credentials(p.remote_url)
        if not token:
            # No credentials — leave pending_push set; next user
            # gesture will route the AUTH_REQUIRED prompt.
            continue
        try:
            res = _push_repo(p.working_dir, git_user, token)
        except Exception as ex:
            print(f'[scheduler] drain push {langcode!r} raised: {ex!r}',
                  file=sys.stderr, flush=True)
            continue
        codes = res.codes()
        print(f'[scheduler] drain push {langcode!r} codes={codes!r}',
              file=sys.stderr, flush=True)
        if 'PUSHED' in codes:
            _set_pending_push(langcode, False)
            projects.set_last_sync(langcode)
