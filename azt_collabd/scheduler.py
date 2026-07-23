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
from . import sync_flight
from . import wan_backoff
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
    DONE).

    Also runs the legacy-orphan sweeps (see
    ``_sweep_legacy_orphans``) — currently just the pre-0.37
    ``.cawl_image_urls.json`` files left behind in project
    working_dirs when CAWL ownership moved from peer to daemon.
    """
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
    # WAN backoff: do NOT clear ``next_attempt_at`` on startup
    # (0.50.45). Previously the restart counted as a free immediate
    # retry on the theory that "user-initiated restart" was an
    # intent-equivalent signal. In practice on Android the daemon
    # respawns frequently for reasons unrelated to user intent
    # (OOM, sticky-service restart, APK self-update), and each one
    # gave a free WAN attempt — undermining the 24h cap the curve
    # was designed for. Only Sync (user-equivalent nudge via
    # ``wan_backoff.nudge``) and actual success now reset the
    # curve. Daemon lifecycle does not.
    # Eager-init the per-device peer_id (0.50.9). Pre-0.50.9
    # ``peer_id.ensure()`` only ran when LAN sync was enabled or
    # the QR generator opened — so on builds that never enabled
    # LAN, ``lan_peer_id()`` returned ``''`` and slot claims got
    # empty-peer_id entries, forcing the peer-side fallback chain
    # to match on the more fragile ``device_name``. Eager-init
    # makes peer_id always available so it's the stable identity
    # for slot claims, future per-device state, and the audit-#9
    # tiebreaker in the slot merge driver. Best-effort: a build
    # without the cryptography package logs a warning and falls
    # through to the pre-0.50.9 empty-peer_id behaviour rather
    # than refusing to start the daemon.
    try:
        from . import peer_id as _peer_id
        _peer_id.ensure()
    except RuntimeError as ex:
        print(f'[scheduler] peer_id.ensure on startup failed: '
              f'{ex!r}; slot claims will use empty peer_id and '
              f'fall back to device_name matching',
              file=sys.stderr, flush=True)
    except Exception as ex:
        print(f'[scheduler] peer_id.ensure unexpected: {ex!r}',
              file=sys.stderr, flush=True)
    if interrupted or stale:
        print(f'[scheduler] reconcile_on_startup: '
              f'interrupted={interrupted} gc={len(stale)}', flush=True)
    # Layer 2 (0.52.21): any project whose WAN push was still marked
    # in-flight when the previous daemon died had its push killed
    # before it could bank progress (Android idle-stop / OOM / APK
    # update). Bump its interrupted_count so the drain escalates to
    # run-to-completion (Layer 3) instead of politely restarting the
    # same doomed attempt. Best-effort; never blocks startup.
    try:
        killed = wan_backoff.note_interrupted_on_startup()
        if killed:
            print(f'[scheduler] reconcile_on_startup: push interrupted '
                  f'mid-flight for {killed!r} (will escalate)',
                  flush=True)
    except Exception as ex:
        print(f'[scheduler] note_interrupted_on_startup raised: {ex!r}',
              file=sys.stderr, flush=True)
    # Run outside the scheduler lock — touches the filesystem, not
    # the jobs registry. Best-effort; never raises.
    _sweep_legacy_orphans()
    # Daemon-startup orphan-tracking-ref sweep (0.50.49). Walk
    # every registered project and fire
    # ``strip_lan_origin_if_present(scope_to_paired_peers=True)``
    # to clean any stale ``refs/remotes/origin/*`` left over from
    # earlier daemons that stripped the URL but not the refs (the
    # half-stripped state that misled ``_wan_unshared`` pre-
    # 0.50.48). Without this, orphans persist on projects the
    # user doesn't currently open — visible in lan_debug as
    # nonzero ``remote_refs_present`` despite ``has_origin_url:
    # false``. Cleanup runs once per startup; subsequent
    # ``_h_project_status`` polls handle steady state.
    try:
        from . import projects as _proj
        from . import repo as _repo
        data = _proj._load_raw()
        for langcode in sorted(data.keys()):
            entry = data.get(langcode) or {}
            wd = entry.get('working_dir', '') or ''
            if not wd:
                continue
            try:
                _repo.strip_lan_origin_if_present(
                    wd, scope_to_paired_peers=True)
            except Exception as ex:
                print(f'[scheduler] startup orphan sweep '
                      f'{langcode!r} raised: {ex!r}',
                      file=sys.stderr, flush=True)
    except Exception as ex:
        print(f'[scheduler] startup orphan-sweep dispatch '
              f'raised: {ex!r}', file=sys.stderr, flush=True)
    # Re-populate the deferred post-receive-reset queue from disk so
    # a daemon restart while a reset was pending doesn't lose track.
    # The next watcher tick will drain whatever was queued; in the
    # meantime ``_commit_repo_locked`` absorbs entries on commit too.
    try:
        from . import lan_listener as _lan_listener
        _lan_listener.load_pending_resets_from_disk()
    except Exception as ex:
        print(f'[scheduler] reconcile_on_startup: '
              f'lan_listener.load_pending_resets_from_disk raised '
              f'{ex!r}', file=sys.stderr, flush=True)


# ── legacy-orphan sweeps ────────────────────────────────────────────────────
#
# Migrations that moved ownership of a per-project file from peer to
# daemon (or just changed where a file lives) leave detritus behind
# on devices that crossed the migration boundary. Every commit step
# then flags the orphan as ``DATA_LOSS_RISK`` ("you're writing to a
# path we won't back up") — a true statement about the daemon's
# staging filter but a false alarm in this case, since the writer is
# long gone. The sweep deletes known orphans at daemon startup,
# scoped to project working_dirs read from projects.json.
#
# Add new orphan patterns here as future migrations create them.
# Each one needs:
#   - the relative path inside a project working_dir
#   - the migration version that obsoleted it (in the comment, for
#     future archaeologists)
# Idempotent: missing files are a no-op; the function is safe to
# re-run on every daemon spawn.

_LEGACY_ORPHAN_PATHS = (
    # CAWL URL cache: pre-0.37 peer-owned per-project cache. Moved
    # to daemon-owned ``$AZT_HOME/cawl/index.json`` in 0.37 (May
    # 2026). Files in project working_dirs are detritus from that
    # transition; nothing in current code reads or writes them.
    '.cawl_image_urls.json',
)


def _sweep_legacy_orphans():
    """Remove known-stale per-project files left behind by past
    migrations. See ``_LEGACY_ORPHAN_PATHS`` for the catalogue.

    Best-effort: any per-file failure (permission denied, FS error)
    is logged but doesn't block other projects or the daemon
    startup itself."""
    try:
        all_projects = projects._load_raw()
    except Exception as ex:
        print(f'[scheduler] orphan sweep skipped (projects.json '
              f'read failed): {ex}', flush=True)
        return
    removed = 0
    for langcode, raw in (all_projects or {}).items():
        if not isinstance(raw, dict):
            continue
        working_dir = raw.get('working_dir') or ''
        if not working_dir or not os.path.isdir(working_dir):
            continue
        for rel in _LEGACY_ORPHAN_PATHS:
            target = os.path.join(working_dir, rel)
            if not os.path.isfile(target):
                continue
            try:
                os.remove(target)
                removed += 1
                print(f'[scheduler] orphan sweep: removed '
                      f'{langcode!r}/{rel} (pre-migration '
                      f'leftover)', flush=True)
            except OSError as ex:
                print(f'[scheduler] orphan sweep: could not remove '
                      f'{target!r}: {ex}', flush=True)
    if removed:
        print(f'[scheduler] orphan sweep: removed={removed} '
              f'across {len(all_projects or {})} project(s)',
              flush=True)


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
    # be waiting; the drain loop already knows from wan_unshared).
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
        after_committed_local(langcode, p)
    return res


def after_committed_local(langcode, p):
    """Post-``COMMITTED_LOCAL`` side effects, shared by the
    debounced commit worker (``_run_commit``) and the synchronous
    ``submit_file`` path (``server._h_project_submit_file``) so a
    desktop whole-file commit converges over LAN exactly like a
    recorder commit. Never raises.

    Eagerly fan-out to paired LAN peers when the LAN toggle
    is on. Without this, ``last_lan_pushed_sha`` only
    advances on the watcher loop's drain tick (every
    ``sync.connectivity_poll_s``, default 30 s) — so any
    commit landing between ticks isn't recorded as "shared
    somewhere" until the next tick, and the peer-side LANOK
    indicator stays dark because ``lan_unshared`` is
    almost never 0 under any sustained editing pace (typical
    field cadence is several commits per minute, > 30 s
    cycle). Firing here makes the latency from
    ``COMMITTED_LOCAL`` to ``last_lan_pushed_sha`` ≈ one
    listener round-trip on the LAN (single-digit ms when
    both phones are up) instead of up to 30 s, so LANOK
    surfaces on the next peer poll. Idempotent: fan_out
    internally peeks each peer's main first and no-ops if
    they're already at our HEAD. 0.45.32.

    Post-commit LAN fan-out: when autodiscovery is on, peers
    are reachable directly; when it's off we kick off a
    burst so paired peers' parallel bursts can rendezvous
    with us during the window. ``start_burst`` is cheap when
    the LAN is already up.

    Backoff (0.50.45): only fire the burst when the per-
    project counter hits a power of two. ``record_commit``
    increments + persists + returns the new count. The
    counter resets on actual LAN delivery (see below). A
    lone worker still attempts ``fan_out`` (cheap, no-ops
    if nobody's reachable) but the radio-wakening burst is
    rare. The Sync button always bursts (separate path via
    ``lan_backoff.nudge``); online-edge and lifecycle
    bursts (0.50.45) also bypass this gate because they
    reflect intent, not routine activity."""
    try:
        projects.set_last_commit(langcode)
    except Exception as ex:
        print(f'[commit] {langcode!r} set_last_commit raised: {ex!r}',
              file=sys.stderr, flush=True)
    try:
        from . import lan_backoff as _lan_backoff
        from . import lan_burst as _lan_burst
        n = _lan_backoff.record_commit(langcode)
        if _lan_backoff._is_power_of_two(n):
            _lan_burst.start_burst()
            print(f'[commit] {langcode!r} burst fired '
                  f'(commits_since_lan_success={n}, '
                  f'power-of-two)',
                  file=sys.stderr, flush=True)
        else:
            print(f'[commit] {langcode!r} burst skipped '
                  f'(commits_since_lan_success={n}, '
                  f'not power-of-two)',
                  file=sys.stderr, flush=True)
    except Exception as ex:
        print(f'[commit] {langcode!r} post-commit burst '
              f'raised: {ex!r}',
              file=sys.stderr, flush=True)
    try:
        from . import lan_push as _lan_push
        results = _lan_push.fan_out(p)
        # If ANY peer received the push, the backoff curve
        # has done its job — reset. Per-project; other
        # projects' curves are untouched.
        if results and any(results.values()):
            try:
                from . import lan_backoff as _lan_backoff
                _lan_backoff.record_success(langcode)
            except Exception as ex_b:
                print(f'[commit] {langcode!r} lan_backoff '
                      f'record_success raised: {ex_b!r}',
                      file=sys.stderr, flush=True)
    except Exception as ex:
        print(f'[commit] {langcode!r} post-commit LAN fan-out '
              f'raised: {ex!r}',
              file=sys.stderr, flush=True)


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


def drain_pushes_now(langcode=''):
    """User-nudge entry point: clear WAN backoff and fire a push
    pass immediately. Used by ``sync_nudge`` (the unified "try
    everything now" gesture, since 0.50).

    *langcode* targets one project; empty string nudges every
    pending project. ``wan_backoff.nudge`` clears ``next_attempt_at``
    while preserving ``consecutive_failures``, so one bad nudge
    doesn't reset weeks of accumulated curve to zero — a fresh
    failure re-enters the curve at the same step.

    No-op if offline (no point burning the network attempt on a
    cold radio). Caller already routed the user gesture so the UI
    knows they tried; the next ``ConnectivityManager`` event or
    the user's next nudge will drive the actual push."""
    if not _has_internet():
        return
    # User-gestured nudge → the watcher's probe-backoff streak
    # should reset so the next periodic tick fires at base
    # cadence rather than at whatever long interval the idle
    # streak had grown to. Cheap; no-op if streak already 0.
    _reset_probe_backoff(reason='user-nudge')
    try:
        if langcode:
            wan_backoff.nudge(langcode)
        else:
            try:
                data = projects._load_raw()
            except Exception:
                data = {}
            for lang in data:
                wan_backoff.nudge(lang)
        _drain_pending_push(ignore_backoff=True)
    except Exception as ex:
        print(f'[scheduler] drain_pushes_now failed: {ex}',
              file=sys.stderr, flush=True)


_last_net_sig = None


def _net_signature():
    """Cheap fingerprint of the machine's network interfaces, to detect
    a link coming up/down — notably a phone plugged in with USB
    tethering enabled, which brings up a new interface (usb0 / enx… /
    an RNDIS/NCM ethernet adapter). The zeroconf browser was started
    before that interface existed and won't see peers on it, so a
    change here triggers a discovery re-arm + burst.

    Linux: the set of names under ``/sys/class/net`` (catches the new
    interface appearing). Plus a best-effort default-route IP so
    platforms without ``/sys`` still notice an address change. Empty /
    stable elsewhere ⇒ no false triggers; any failure is swallowed."""
    import socket
    sig = set()
    try:
        sig.update(os.listdir('/sys/class/net'))
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('192.0.2.1', 9))   # TEST-NET-1; sends nothing
            sig.add('ip:' + s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    return frozenset(sig)


def _watcher_loop():
    global _last_online_state, _online_since, _last_net_sig
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
        # On offline → online edge: this is Phase 6 of the 0.50
        # sync rebuild — the cheap "ConnectivityManager
        # auto-recovery" surrogate. We don't subscribe to a real
        # Android NetworkCallback (would need Java glue +
        # ACCESS_NETWORK_STATE permission); the existing TCP probe
        # detects the same transition within one poll interval
        # (default 30 s). On the edge we:
        #
        #   1. Reset WAN backoff for every pending-push project so
        #      the next drain tick fires immediately instead of
        #      waiting out a 24 h curve.
        #   2. Fire a LAN burst (cheap when autodiscovery=True;
        #      brings up listener + mDNS for the window when
        #      autodiscovery=False) so paired peers on a freshly
        #      joined Wi-Fi can rendezvous.
        #   3. Nudge CAWL to retry prefetch.
        # Track whether the probed state changed this tick; the
        # adaptive sleep at the bottom of the loop uses it.
        if prev != online:
            _reset_probe_backoff(reason='state-change')
        else:
            _bump_probe_backoff()
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
                data = projects._load_raw()
                for langcode_, entry_ in data.items():
                    if entry_.get('pending_push'):
                        wan_backoff.nudge(langcode_)
            except Exception as ex:
                print(f'[watcher] online-edge backoff reset '
                      f'raised: {ex!r}',
                      file=sys.stderr, flush=True)
            try:
                from . import lan_burst as _lan_burst
                _lan_burst.start_burst()
            except Exception as ex:
                print(f'[watcher] online-edge LAN burst raised: '
                      f'{ex!r}', file=sys.stderr, flush=True)
            try:
                from . import cawl as _cawl
                _cawl.on_online_edge()
            except Exception as ex:
                print(f'[cawl] on_online_edge dispatch failed: {ex}',
                      file=sys.stderr, flush=True)
        # Drain tick (since 0.50): WAN push is gated per-project
        # by ``wan_backoff.is_due(langcode)``. The watcher itself
        # doesn't gate on ``work_offline`` / ``grace`` anymore —
        # those existed to avoid hammering on flaky connections,
        # which the exponential curve does more cleanly. LAN
        # fan-out is no longer fired from this loop; it's driven
        # by user nudge (``sync_nudge``) and by ``_run_commit``
        # after a successful local commit. The drain still gets
        # called every tick but it's cheap when nothing is due.
        github_eligible = online
        # LAN-listener split-brain reconcile. The persisted toggle and
        # the listener thread can drift apart in three ways:
        #   * APK update / kill -9 killed the host while
        #     ``apply_toggle`` was mid-write to config.json — next
        #     boot reads the persisted bit fine but the listener
        #     thread/FGS/WifiLock state is fresh (= empty);
        #   * ``apply_toggle`` raised on FGS or WifiLock acquisition
        #     during boot and aborted; persisted=True, listener=down;
        #   * an inbound socket failure tore the listener down but the
        #     toggle wasn't flipped (paranoia case).
        # In all three the symptom is the same: peers fan-out to a
        # listener that isn't listening, get "Connection refused" in
        # a loop, and the user has to open the pair flow (which calls
        # ``_auto_enable_lan`` and force-re-applies) to recover. Doing
        # the reconcile here heals it without a user gesture. The call
        # is idempotent — apply_toggle no-ops when state matches.
        try:
            from . import lan_listener as _lan_listener
            _lan_listener.apply_toggle()
        except Exception as ex:
            print(f'[scheduler] lan_listener.apply_toggle failed: '
                  f'{ex!r}', file=sys.stderr, flush=True)
        # Link-up nudge (0.54.34) — "plug in and go" for USB tethering.
        # If the interface set changed since last tick (a phone plugged
        # in + USB tethering enabled brings up usb0/enx…), the zeroconf
        # browser — started before that interface existed — won't be
        # browsing it. Re-arm discovery and fire a burst so a paired
        # peer on the new link is found + synced with no user gesture.
        # Only while LAN sync is on, and only on an actual change.
        if _settings.lan_allow_sync():
            try:
                sig = _net_signature()
                if _last_net_sig is not None and sig != _last_net_sig:
                    print('[watcher] network interfaces changed '
                          '(link up/down) — re-arming LAN discovery '
                          '+ burst', file=sys.stderr, flush=True)
                    from . import lan_discovery as _lan_discovery
                    _lan_discovery.restart_browse()
                    from . import lan_burst as _lan_burst
                    _lan_burst.start_burst()
                    _reset_probe_backoff(reason='net-change')
                _last_net_sig = sig
            except Exception as ex:
                print(f'[watcher] link-up nudge raised: {ex!r}',
                      file=sys.stderr, flush=True)
        # Drain the deferred post-receive-reset queue. Entries land
        # here when ``_reset_working_tree_after_receive`` hit a
        # LockTimeout (typically because the local outgoing merge
        # held the project_lock past 5 s). Each retry goes through
        # the full reset path; success removes the entry, continued
        # LockTimeout re-queues. See lan_listener.drain_pending_resets
        # + the matching absorb in repo._commit_repo_locked. The
        # call is a fast no-op when the queue is empty.
        try:
            _lan_listener.drain_pending_resets()
        except Exception as ex:
            print(f'[scheduler] lan_listener.drain_pending_resets '
                  f'failed: {ex!r}', file=sys.stderr, flush=True)
        if github_eligible:
            try:
                _drain_pending_push()
            except Exception as ex:
                print(f'[scheduler] _drain_pending_push failed: {ex}',
                      file=sys.stderr, flush=True)
            # Cheap access re-probe for projects blocked on a remote-
            # fixable access error (0.52.24). Decoupled from the push
            # backoff: one small GET per blocked project, throttled to
            # 5 min, that nudges the real push the moment access is
            # restored (collaborator grant / permission upgrade / invite
            # accepted) instead of waiting out the 24 h curve.
            try:
                _drain_access_reprobe()
            except Exception as ex:
                print(f'[scheduler] _drain_access_reprobe failed: {ex}',
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
        # Adaptive probe interval (audit finding #5, 0.50.15).
        # Pre-0.50.15 we slept exactly ``connectivity_poll_s``
        # every tick — fine on a phone that's about to push, but
        # wasteful on an idle phone in a pocket all day (radio
        # wake every 30 s for ages even when nothing has changed).
        # Grow the interval on consecutive same-state ticks
        # (no online-edge AND no pending pushes ready to fire),
        # cap at 5 min, and reset whenever something interesting
        # happens. The state-change case (offline → online edge)
        # already resets ``_probe_idle_streak`` above; nudge
        # entry points (``drain_pushes_now``) reset it too.
        base = max(5.0, float(_settings.connectivity_poll_s()))
        interval = _adaptive_probe_interval(base)
        # While LAN sync is on, keep the poll brisk (≤15 s) so a
        # link-up (USB tether plug) is caught by the interface-change
        # check above within ~15 s rather than after the idle backoff
        # grows toward its 5-min cap. LAN-on already keeps the listener
        # + mDNS (and, on Android, the FGS + WifiLock) up, so a 15 s
        # probe is negligible incremental radio. (The pocket-battery
        # backoff still applies fully when LAN sync is off.)
        try:
            if _settings.lan_allow_sync():
                interval = min(interval, 15.0)
        except Exception:
            pass
        if _watcher_stop.wait(timeout=interval):
            break


# Adaptive connectivity-probe state (0.50.15, audit finding #5).
# Grows when consecutive ticks find no state change AND nothing
# WAN-pending. Reset on online-edge / user nudge / fresh commit.
# Cap at 5 min — at the cap, a state flip from offline → online
# is detected within one cap interval, which is acceptable for
# the "phone in a pocket all day" use case (the user's gesture
# resets the streak anyway when they touch the app).
_PROBE_BACKOFF_CAP_S = 300.0
_PROBE_BACKOFF_MAX_SHIFT = 4  # 2^4 = 16× base before cap clamps
_probe_idle_streak = 0


def _adaptive_probe_interval(base):
    """Compute the next probe sleep interval. Doubles each
    ``_probe_idle_streak`` step up to ``_PROBE_BACKOFF_CAP_S``."""
    streak = max(0, min(_probe_idle_streak, _PROBE_BACKOFF_MAX_SHIFT))
    interval = base * (1 << streak)
    return min(interval, _PROBE_BACKOFF_CAP_S)


def _reset_probe_backoff(reason=''):
    """Drop ``_probe_idle_streak`` to 0. Call from any path that
    represents "something just happened, the next probe should
    fire promptly" — user nudge, online-edge, fresh commit."""
    global _probe_idle_streak
    if _probe_idle_streak != 0:
        _probe_idle_streak = 0
        if reason:
            print(f'[watcher] probe backoff reset ({reason})',
                  file=sys.stderr, flush=True)


def _bump_probe_backoff():
    """Increment ``_probe_idle_streak`` by 1. Called once per
    no-state-change probe tick."""
    global _probe_idle_streak
    _probe_idle_streak += 1


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


_drain_skip_last_logged = {}


def _log_drain_skip_due_to_backoff(langcode):
    """Rate-limited '[scheduler] drain skipped: <lang> wan_backoff
    next=… (in …)' emission for the case where the curve is
    actively suppressing an attempt. Without this, ``drain
    pushes: ['x']`` is followed by silence and an observer can't
    tell whether the daemon is throttling deliberately, is stuck,
    or never actually fired the push.

    Keyed on (langcode, next_due_at) so a stable backoff state
    logs once and re-logs only when the next-due time moves
    (a record_failure or a record_success since the last skip)."""
    try:
        next_due = wan_backoff.next_due_at(langcode)
        failures = wan_backoff.consecutive_failures(langcode)
    except Exception:
        return
    cache_key = (langcode, int(next_due))
    if _drain_skip_last_logged.get(langcode) == cache_key:
        return
    _drain_skip_last_logged[langcode] = cache_key
    try:
        from datetime import datetime, timezone
        when = datetime.fromtimestamp(
            next_due, tz=timezone.utc).isoformat(timespec='seconds')
    except Exception:
        when = f'+{int(next_due)}s'
    remaining_s = max(0, int(next_due - time.time()))
    if remaining_s >= 3600:
        remaining = f'{remaining_s // 3600}h{(remaining_s % 3600) // 60}m'
    elif remaining_s >= 60:
        remaining = f'{remaining_s // 60}m{remaining_s % 60}s'
    else:
        remaining = f'{remaining_s}s'
    print(f'[scheduler] drain skipped: {langcode!r} '
          f'wan_backoff next={when} (in {remaining}, '
          f'{failures} consecutive failure(s))',
          file=sys.stderr, flush=True)


def _drain_pending_push(ignore_backoff=False):
    """Push any project flagged ``pending_push``. WAN attempts are
    gated by ``wan_backoff.is_due(langcode)`` so an offline-for-
    hours project doesn't wake the radio every connectivity tick
    — the curve doubles up to a 24 h cap. ``ignore_backoff=True``
    is the user-nudge path: ``wan_backoff.nudge()`` has already
    cleared ``next_attempt_at``, and we fire all pending projects
    regardless of the WAN due times so a tap-to-sync is responsive.

    LAN fan-out is independent and (per the design rebuild in
    0.50) is no longer fired from this drain loop — it's fired
    by user nudges and by ``_run_commit`` after a successful
    local commit. The scheduler drain stays WAN-only."""
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
        # Layer 3 (0.52.21): escalate a stuck-but-online project to
        # run-to-completion. Trigger = online AND the push has been
        # killed mid-flight at least ``_ESCALATE_INTERRUPT_THRESHOLD``
        # times (Layer 2 marker). This is the "notice this is
        # happening, not just no internet" gate the field repro (nml,
        # 2167 commits, killed every daemon lifetime) needed.
        escalate = (
            is_online_cached() is True
            and wan_backoff.interrupted_count(langcode)
            >= _ESCALATE_INTERRUPT_THRESHOLD)
        if not escalate and not ignore_backoff \
                and not wan_backoff.is_due(langcode):
            # Curve says wait. Don't bother probing credentials or
            # the network — that's the whole point of the curve.
            # Rate-limited diagnostic so "drain pushes: ['nml']"
            # without a subsequent sync-trace stops looking like
            # the daemon doing nothing — last drain skip is cached
            # per langcode and only re-emitted when the next-due
            # time changes (e.g., after a record_failure shifts
            # the curve out further).
            _log_drain_skip_due_to_backoff(langcode)
            continue
        git_user, token = get_sync_credentials(p.remote_url)
        if not token:
            # No credentials — leave pending_push set; the next
            # user gesture routes AUTH_REQUIRED. Don't advance the
            # backoff curve: nothing failed network-wise.
            continue
        if escalate:
            _run_to_completion(langcode, p, git_user, token)
            continue
        res = _attempt_push(langcode, p, git_user, token)
        if res is None:
            wan_backoff.record_failure(langcode)
            continue
        codes = res.codes()
        print(f'[scheduler] drain push {langcode!r} '
              f'codes={codes!r}',
              file=sys.stderr, flush=True)
        if 'PUSHED' in codes:
            _set_pending_push(langcode, False)
            projects.set_last_sync(langcode)
            wan_backoff.record_success(langcode)
            _clear_access_error(langcode)
        elif 'INVITE_ACCEPTED' in codes:
            # Auto-accepted a pending GitHub invite (the 404 handler).
            # Access should now work — clear the stale error and leave
            # pending_push set so the next tick retries against the now-
            # accessible repo. Don't advance the backoff curve.
            _clear_access_error(langcode)
        elif 'NOTHING_TO_COMMIT' in codes or 'NO_REMOTE' in codes:
            # No-op outcomes don't advance the backoff curve.
            pass
        else:
            wan_backoff.record_failure(langcode)
            _note_access_error(langcode, res)


# ── stuck-diverged-push run-to-completion (0.52.21) ─────────────────
#
# Layers 1-3 of the fix for a large diverged history that never
# converges because the Android idle-stop kills the push before the
# resumable chunked upload (repo._push_chunked_to_ref) can bank
# progress. See azt_collab_client/docs/rationale/sync.md and
# sync_flight.py.

# Escalate after the push has been killed mid-flight this many times.
_ESCALATE_INTERRUPT_THRESHOLD = 2
# Push attempts per escalated drain visit. The chunked push is
# resumable, so remaining work resumes on the next tick; this bounds
# how long one visit holds the wakelock.
_RUN_TO_COMPLETION_MAX_ITERS = 8
# Wall-clock ceiling on a single escalated visit's hold of
# ``project_lock``. Checked BETWEEN iterations so a user Sync tapped
# during escalation waits at most ~one in-flight chunk push
# (``repo._PUSH_TIMEOUT_S``) rather than the whole visit. Without this
# an escalated run could iterate its full budget back-to-back and keep
# every user RPC on ``BUSY`` for minutes (field: nml, aztobt2-ui). The
# push is resumable, so yielding here loses no progress — the next
# drain tick resumes from the server-side topic-branch tip.
_RUN_TO_COMPLETION_DEADLINE_S = 120.0
# Battery safety valve: after this many non-converging escalated
# visits, stop escalating and fall back to the normal backoff curve
# until the next user Sync / online edge re-arms it.
_ESCALATE_MAX_VISITS = 12
# Push outcomes that retrying can't fix — stop escalating immediately.
_PERMANENT_PUSH_CODES = (
    'AUTH_REQUIRED', 'APP_NOT_INSTALLED', 'APP_SUSPENDED',
    'REPO_NOT_AUTHORIZED', 'ACCESS_DENIED', 'NOT_A_REPO',
    'REPO_NO_ACCESS',
)
# Access-class codes worth persisting as the project's last_sync_error so
# project_status surfaces WHY sync is stuck (0.52.24, requirement 1.1 —
# don't silently die on a creds/access problem when creds ARE present).
_ACCESS_ERROR_CODES = _PERMANENT_PUSH_CODES + ('AUTH_REFRESH_STALE',)


def _note_access_error(langcode, res):
    """If *res* carries an access-class status, persist it as the project's
    last_sync_error so ``project_status`` can tell the user WHY sync is
    stuck. No-op for non-access failures (plain network) — those are
    transient and shouldn't nag."""
    try:
        codes = res.codes()
    except Exception:
        return
    for c in _ACCESS_ERROR_CODES:
        if c in codes:
            try:
                projects.set_last_sync_error(langcode, c)
            except Exception:
                pass
            return


def _clear_access_error(langcode):
    """Drop any persisted access error (successful sync / invite accepted)."""
    try:
        projects.clear_last_sync_error(langcode)
    except Exception:
        pass


# Access errors that a *remote* change can fix (a collaborator grant, a
# permission upgrade, an app (re)install / unsuspend) — and that we can
# cheaply RE-PROBE for without running the expensive git op. NOT_A_REPO is
# excluded (local / publish-flow); AUTH_* are excluded (fixed by the local
# credential-save event, handled by nudge_access_blocked_projects).
_ACCESS_REPROBE_CODES = (
    'REPO_NO_ACCESS', 'REPO_NOT_AUTHORIZED',
    'APP_NOT_INSTALLED', 'APP_SUSPENDED', 'ACCESS_DENIED',
)
# The cheap probe is one small API call, so it can run far more often than
# the expensive push — but not every 30 s tick. Once every 5 min per
# blocked project is plenty to self-heal within minutes of an out-of-band
# grant, at negligible cost.
_ACCESS_REPROBE_MIN_INTERVAL_S = 300.0
_access_reprobe_last = {}   # langcode -> monotonic time of last probe


def nudge_access_blocked_projects(codes=None):
    """Clear WAN backoff for every project whose ``last_sync_error`` is in
    *codes*. Called from events that plausibly fix access — credentials
    (re)saved, collaborator granted — so a blocked push retries at once
    instead of waiting out the 24 h curve. Doesn't clear the error itself;
    the next successful sync does. Returns the count nudged."""
    want = set(codes) if codes is not None else set(_ACCESS_ERROR_CODES)
    try:
        data = projects._load_raw()
    except Exception:
        return 0
    n = 0
    for langcode, entry in data.items():
        if (entry.get('last_sync_error') or '') in want:
            try:
                wan_backoff.nudge(langcode)
                n += 1
            except Exception:
                pass
    if n:
        _reset_probe_backoff('access-event-nudge')
        print(f'[scheduler] access-event nudge: {n} blocked project(s)',
              file=sys.stderr, flush=True)
    return n


def nudge_project(langcode):
    """Clear WAN backoff for one project so its next drain fires now. Used
    by the grant-collaborator success path (the grant may have just
    created the invite / access this project was blocked on)."""
    try:
        wan_backoff.nudge(langcode)
        _reset_probe_backoff('project-nudge')
    except Exception:
        pass


def _drain_access_reprobe():
    """For each project blocked on a remote-fixable access error, run a
    CHEAP probe — accept a now-pending invite, or ``GET /repos`` for
    existence + ``permissions.push`` — decoupled from the expensive-push
    backoff. When the probe flips to OK, that's the event that ends the
    wait: clear the error and nudge the real push. Throttled per project
    (`_ACCESS_REPROBE_MIN_INTERVAL_S`); online-gated by the caller."""
    try:
        data = projects._load_raw()
    except Exception:
        return
    blocked = [lang for lang, e in data.items()
               if (e.get('last_sync_error') or '') in _ACCESS_REPROBE_CODES]
    if not blocked:
        return
    from . import auth as _auth
    now = time.monotonic()
    for langcode in blocked:
        last = _access_reprobe_last.get(langcode, 0.0)
        if (now - last) < _ACCESS_REPROBE_MIN_INTERVAL_S:
            continue
        _access_reprobe_last[langcode] = now
        p = projects.get(langcode)
        if p is None or not p.remote_url:
            continue
        git_user, token = get_sync_credentials(p.remote_url)
        if not token:
            continue
        # A matching invitation may have appeared since we last failed —
        # accept it (cheap) and nudge.
        try:
            if _auth.try_accept_repo_invitation(token, p.remote_url):
                print(f'[scheduler] access re-probe {langcode!r}: accepted '
                      f'pending invite → nudge',
                      file=sys.stderr, flush=True)
                _clear_access_error(langcode)
                wan_backoff.nudge(langcode)
                _reset_probe_backoff('access-restored')
                continue
        except Exception as ex:
            print(f'[scheduler] access re-probe {langcode!r} invite '
                  f'check raised: {ex!r}', file=sys.stderr, flush=True)
        # Otherwise: can we now see AND push the repo?
        try:
            probe = _auth.probe_repo_access(token, p.remote_url)
        except Exception as ex:
            print(f'[scheduler] access re-probe {langcode!r} raised: {ex!r}',
                  file=sys.stderr, flush=True)
            continue
        if probe.get('ok'):
            print(f'[scheduler] access re-probe {langcode!r}: access '
                  f'restored (can_push) → nudge',
                  file=sys.stderr, flush=True)
            _clear_access_error(langcode)
            wan_backoff.nudge(langcode)
            _reset_probe_backoff('access-restored')


def _attempt_push(langcode, p, git_user, token):
    """One WAN push attempt, guarded so the Android idle-stop won't
    kill the process mid-push (Layer 1: ``sync_flight.guard``) and
    marked so a process death mid-push is detectable on the next
    daemon startup (Layer 2: ``wan_backoff.mark_push_started`` /
    ``mark_push_finished``). Returns the ``Result``, or None if
    ``_push_repo`` raised. Outcome handling is the caller's job."""
    with sync_flight.guard():
        wan_backoff.mark_push_started(langcode)
        try:
            return _push_repo(p.working_dir, git_user, token)
        except Exception as ex:
            print(f'[scheduler] push {langcode!r} raised: {ex!r}',
                  file=sys.stderr, flush=True)
            return None
        finally:
            # Clean return (any outcome) ⇒ NOT a process-death
            # interruption. If the process is instead killed inside
            # ``_push_repo`` (OOM), this ``finally`` never runs, the
            # marker survives, and startup counts it as interrupted.
            wan_backoff.mark_push_finished(langcode)


def _run_to_completion(langcode, p, git_user, token):
    """Layer 3: drive a stuck-but-online diverged push to completion.

    Holds an Android foreground service + WifiLock for the duration
    (``lan_fgs.arm_for_transfer`` — keeps the process alive and the
    radio in high-perf so the pack doesn't stall) and loops the
    resumable chunked push, bypassing the radio-friendly
    ``wan_backoff`` curve. Bounded per invocation
    (``_RUN_TO_COMPLETION_MAX_ITERS``); the chunked push is resumable
    so any remaining work continues on the next drain tick / daemon
    lifetime. ``_ESCALATE_MAX_VISITS`` is the battery giveup valve."""
    print(f'[scheduler] run-to-completion {langcode!r}: escalating '
          f'(interrupted={wan_backoff.interrupted_count(langcode)}, '
          f'visits={wan_backoff.snapshot().get(langcode, {}).get("escalation_attempts", 0)})',
          file=sys.stderr, flush=True)
    try:
        from .android_cp import lan_fgs as _fgs
    except Exception:
        _fgs = None
    if _fgs is not None:
        try:
            _fgs.arm_for_transfer()
        except Exception as ex:
            print(f'[scheduler] run-to-completion arm_for_transfer '
                  f'raised: {ex!r}', file=sys.stderr, flush=True)
    outcome = 'incomplete'
    deadline = time.monotonic() + _RUN_TO_COMPLETION_DEADLINE_S
    try:
        for i in range(_RUN_TO_COMPLETION_MAX_ITERS):
            if is_online_cached() is not True:
                outcome = 'offline'
                break
            if time.monotonic() >= deadline:
                # Yield project_lock so a waiting user Sync / commit
                # isn't starved with BUSY. Reaching the wall-clock
                # deadline means iterations were SLOW — i.e. the chunked
                # push was actually transferring (progress), not spinning
                # on a fast-failing chunk. So this is 'yielded', NOT a
                # non-converging visit: it must not count against the
                # battery giveup valve. The push is resumable; remaining
                # work continues next tick.
                print(f'[scheduler] run-to-completion {langcode!r}: '
                      f'visit deadline reached (iter={i}), yielding lock '
                      f'— resumes next tick', file=sys.stderr, flush=True)
                outcome = 'yielded'
                break
            res = _attempt_push(langcode, p, git_user, token)
            if res is None:
                # Transient raise — loop and let the chunked push
                # resume; don't advance the curve (irrelevant while
                # escalated).
                continue
            codes = res.codes()
            print(f'[scheduler] run-to-completion {langcode!r} '
                  f'iter={i} codes={codes!r}',
                  file=sys.stderr, flush=True)
            if 'PUSHED' in codes:
                _set_pending_push(langcode, False)
                projects.set_last_sync(langcode)
                wan_backoff.record_success(langcode)   # clears all state
                _clear_access_error(langcode)
                outcome = 'converged'
                break
            if 'INVITE_ACCEPTED' in codes:
                # The 404 handler just auto-accepted a pending GitHub
                # invitation — access should now be granted. Clear the
                # stale access error and retry immediately (don't count
                # it as a failure).
                _clear_access_error(langcode)
                print(f'[scheduler] run-to-completion {langcode!r}: '
                      f'accepted pending invite, retrying',
                      file=sys.stderr, flush=True)
                continue
            if 'NOTHING_TO_COMMIT' in codes or 'NO_REMOTE' in codes:
                outcome = 'noop'
                break
            if any(c in codes for c in _PERMANENT_PUSH_CODES):
                _note_access_error(langcode, res)
                outcome = 'permanent'
                break
            # Network / partial-progress failure: the chunked push
            # banked whatever chunks it could — loop to resume.
    finally:
        if _fgs is not None:
            try:
                _fgs.disarm_for_transfer()
            except Exception as ex:
                print(f'[scheduler] run-to-completion disarm raised: '
                      f'{ex!r}', file=sys.stderr, flush=True)
    if outcome == 'converged':
        print(f'[scheduler] run-to-completion {langcode!r}: converged',
              file=sys.stderr, flush=True)
        return
    if outcome == 'noop':
        return
    if outcome == 'offline':
        # Not the push's fault — resume when online, don't count it
        # against the giveup valve.
        print(f'[scheduler] run-to-completion {langcode!r}: went '
              f'offline, will resume', file=sys.stderr, flush=True)
        return
    if outcome == 'yielded':
        # Progressing but polite about the lock — resume next tick.
        # Does NOT count against the battery giveup valve (that's for
        # non-converging visits, not slow-but-transferring ones).
        return
    if outcome == 'permanent':
        # No amount of retrying fixes no-access / not-a-repo. Stop
        # escalating and let the normal backoff surface the reason on
        # the next user Sync.
        wan_backoff.clear_interrupted(langcode)
        wan_backoff.record_failure(langcode)
        print(f'[scheduler] run-to-completion {langcode!r}: permanent '
              f'failure, reverting to normal backoff',
              file=sys.stderr, flush=True)
        return
    # incomplete: exhausted the per-visit iteration budget while
    # online and still not converged. Count the visit; give up
    # escalating after the cap so we don't hold the radio forever on
    # a push that genuinely can't complete (e.g. a single blob larger
    # than the remote accepts).
    n = wan_backoff.bump_escalation_attempts(langcode)
    if n >= _ESCALATE_MAX_VISITS:
        print(f'[scheduler] run-to-completion {langcode!r}: giving up '
              f'after {n} non-converging visits — reverting to normal '
              f'backoff until next Sync/online-edge',
              file=sys.stderr, flush=True)
        wan_backoff.clear_interrupted(langcode)
        wan_backoff.record_failure(langcode)
    else:
        print(f'[scheduler] run-to-completion {langcode!r}: visit '
              f'{n}/{_ESCALATE_MAX_VISITS} incomplete, resumes next tick',
              file=sys.stderr, flush=True)
