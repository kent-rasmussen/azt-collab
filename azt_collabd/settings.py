"""
Runtime configuration backed by ``$AZT_HOME/config.json`` (separate
from azt_collabd.config which holds the static GitHub App identity).

Keys:
    sync.debounce_ms          — debounce window for commit_project (ms)
    sync.merge_retry_max      — placeholder for the merge driver step
    sync.connectivity_poll_s  — interval for the connectivity watcher (s)
    sync.post_online_grace_s  — wait this long after an offline→online edge
                                before the watcher drains pending pushes
                                (avoids burning the user's MB on a brief
                                tether they enabled for some other reason)
    sync.work_offline         — daemon-wide bool. When true, the watcher's
                                drain is a no-op and the user-initiated
                                Sync button returns S.WORK_OFFLINE_ENABLED
                                without attempting any push. Commits still
                                happen normally; only push is suppressed.
    sync.push_budget_s        — wall-clock cap (seconds) on the adaptive
                                push loop in repo._push_step_locked. When
                                exceeded the loop emits SYNC_GIVING_UP_
                                TRANSIENT + PUSH_FAILED and bails so the
                                project lock frees for the next sync run.
                                0 disables the cap. Default 300 (5 min);
                                bumped from "no cap" to bound the field-
                                observed 35-minute chunk-halving storm on
                                flaky-DNS networks.

Env-var overrides take precedence at startup:
    AZT_SYNC_DEBOUNCE_MS
    AZT_SYNC_MERGE_RETRY_MAX
    AZT_SYNC_CONNECTIVITY_POLL_S
    AZT_SYNC_POST_ONLINE_GRACE_S
    AZT_SYNC_WORK_OFFLINE
    AZT_SYNC_PUSH_BUDGET_S
"""

import contextlib
import json
import os
import threading
import time as _time

from .paths import azt_home


_FILENAME = 'config.json'
_DEFAULTS = {
    'sync.debounce_ms': 500,
    'sync.merge_retry_max': 3,
    'sync.connectivity_poll_s': 30,
    'sync.post_online_grace_s': 60,
    'sync.work_offline': False,
    'sync.push_budget_s': 300,
    # Wedge self-monitor (0.54.90) — see azt_collabd/watchdog.py.
    # Thresholds are in MINUTES on purpose: a merge or a slow push is
    # not a wedge, and restarting legitimate work would trade one bad
    # evening for a livelock on slow machines. Set
    # ``watchdog.restart_s`` to 0 to keep the diagnostics (traceback
    # dump) without the automatic restart.
    'watchdog.enabled': True,
    'watchdog.warn_s': 120,
    'watchdog.restart_s': 600,
    'watchdog.interval_s': 30,
    # LAN sync, 0.50+ semantics.
    #
    # ``lan.autodiscovery`` (default False since 0.50.2): when
    # True the daemon keeps mDNS advertise + browse +
    # MulticastLock + FGS running continuously so paired peers
    # can find us cold and we auto-recover on Wi-Fi-change events
    # without a user gesture. When False, discovery happens only
    # during burst windows triggered by ``sync_nudge`` (user
    # tapped the sync icon) or by ``_run_commit`` (just made a
    # local commit). Both phones' bursts must overlap to
    # rendezvous; the user gesture is the synchronization
    # primitive.
    #
    # Migration from pre-0.50 ``lan.allow_sync`` is handled by
    # ``lan_autodiscovery()`` reading the old key when the new
    # one isn't present — existing 'on' users keep being on, and
    # the migration is invisible.
    #
    # Hot-applied — flipping does NOT require a daemon restart.
    'lan.autodiscovery': False,
}
_ENV_MAP = {
    'sync.debounce_ms': 'AZT_SYNC_DEBOUNCE_MS',
    'sync.merge_retry_max': 'AZT_SYNC_MERGE_RETRY_MAX',
    'sync.connectivity_poll_s': 'AZT_SYNC_CONNECTIVITY_POLL_S',
    'sync.post_online_grace_s': 'AZT_SYNC_POST_ONLINE_GRACE_S',
    'sync.work_offline': 'AZT_SYNC_WORK_OFFLINE',
    'sync.push_budget_s': 'AZT_SYNC_PUSH_BUDGET_S',
    'lan.autodiscovery': 'AZT_LAN_AUTODISCOVERY',
}

_lock = threading.Lock()

# Sentinel for "config.json is unreadable" (truncated JSON, corrupt
# bytes). Distinct from "file missing" (empty dict) so callers don't
# clobber the file with a defaults-only payload after a failed read.
class _LoadFailed:
    __slots__ = ('error',)

    def __init__(self, error):
        self.error = error


def _path():
    return os.path.join(azt_home(), _FILENAME)


def _load_raw():
    """Return the parsed config dict, ``{}`` if the file is missing,
    or ``_LoadFailed(error)`` if the file exists but can't be parsed.
    Callers MUST distinguish the third case — overwriting a corrupt
    file with a dict built from defaults silently wipes every other
    persisted key (see the APK-update-killed-mid-write failure mode
    that surfaced 2026-05-26)."""
    try:
        with open(_path()) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as ex:
        # Loud — this is the kind of failure that silently reverts
        # user toggles unless we surface it. The on-disk file is
        # preserved (no clobber) and callers refuse to write until
        # the user repairs / removes it.
        print(f'[collab.settings] load failed (config.json '
              f'unreadable; preserving on disk, refusing writes '
              f'until repaired): {ex!r}', flush=True)
        return _LoadFailed(ex)


def _save_raw(data):
    """Atomically persist *data* to config.json. Write to a tmp file
    in the same directory, ``fsync``, then ``os.replace`` — so an
    interrupted write (APK update kill, OOM) never leaves a truncated
    config.json that the next boot reads as ``{}`` and resolves every
    key to its default."""
    with _lock:
        _save_raw_locked(data)


def _save_raw_locked(data):
    """``_save_raw`` body, assuming ``_lock`` is already held.

    Split out for ``update()`` (0.55.33), which must hold the lock across
    read + mutate + write; calling ``_save_raw`` from inside that span
    would deadlock on a non-reentrant ``Lock``.

    Per-pid temp name: two processes never share a temp path, and within
    a process the lock serializes, so a partially-written temp can never
    be published by another writer."""
    p = _path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f'{p}.tmp.{os.getpid()}'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            # Some filesystems (notably tmpfs on older Android)
            # reject fsync. Atomic-replace still works there;
            # we just lose the durability guarantee on power
            # loss, which is the smaller of the two risks.
            pass
    os.replace(tmp, p)


def get(key, default=None):
    """Return the current value for *key*. Resolution order:
    env-var override → config.json → DEFAULTS → *default*."""
    env_name = _ENV_MAP.get(key)
    if env_name and env_name in os.environ:
        raw = os.environ[env_name]
        try:
            return _coerce(key, raw)
        except (TypeError, ValueError):
            pass
    data = _load_raw()
    if isinstance(data, _LoadFailed):
        # Treat unreadable as "use defaults for the read", but DON'T
        # let set_() write through this state — that's enforced in
        # set_() itself.
        return _DEFAULTS.get(key, default)
    if key in data:
        return _coerce(key, data[key])
    return _DEFAULTS.get(key, default)


def update(mutate, what='update'):
    """**The** serialized read-modify-write for config.json (0.55.33).

    Holds ``_lock`` across read + mutate + write. That span is the whole
    point: ``_save_raw`` already took the lock for the write alone, but
    every caller did its READ outside it, so two setters could each read
    the file, each change their own key, and each write the whole dict —
    the second one silently dropping the first one's change. A plain lost
    update; no corruption required.

    Field 2026-07-27: Kent's contributor name vanished on five devices
    and would not stay saved. He'd type it (``store.set_contributor``
    → read, set, write) while ``iface-watch`` applied the LAN toggle
    (``settings.set_lan_allow_sync`` → read, set, write) from a snapshot
    taken before his save. Whichever wrote last won, and the loser's key
    was gone. It also explains why ``lan.allow_sync`` always looked fine:
    the toggle rewrites its own key constantly, while nothing rewrites a
    person's name.

    ``store.py`` writes the same file (``collab.contributor``,
    ``device_name``, CAWL prefs) and had its own separate lock, which is
    no lock at all for this purpose — two locks guarding one file
    serialize nothing. It now delegates here, so there is exactly one
    mutex and one write path.

    Refuses on an unreadable config rather than rebuilding it from
    defaults, preserving the pre-existing ``_LoadFailed`` contract.
    Returns True if written."""
    with _lock, _config_file_lock():
        data = _load_raw()
        if isinstance(data, _LoadFailed):
            print(f'[collab.settings] refusing {what}: config.json is '
                  f'unreadable ({data.error!r}). Repair or remove it '
                  f'and retry.', flush=True)
            return False
        mutate(data)
        _save_raw_locked(data)
        return True


@contextlib.contextmanager
def _config_file_lock(timeout=5.0):
    """``flock`` around config.json's read-modify-write — CROSS-PROCESS
    (0.55.33).

    A ``threading.Lock`` is not enough here, and that isn't theoretical.
    Kent 2026-07-27: *"four android devices lost this field at the same
    time, immediately after an update."* Four devices simultaneously is
    not a race being lost by chance; it is something an update does every
    time.

    On startup **two processes** write this file: the picker Activity's
    jnius prewarm (``server_apk/main.py`` step 2a calls
    ``store.get_device_name()``, which its own comment notes "caches
    device_name in config.json") and ``:provider``'s own boot. Each does
    read-modify-write. A thread lock in one process is invisible to the
    other, so the second writer publishes a dict built from a snapshot
    taken before the first writer's change — and the first writer's key
    is gone. An update restarts both processes together, on every device,
    which is precisely the observed signature. It also explains the
    survivors: ``device_name`` and ``lan.allow_sync`` get rewritten
    constantly, while a person's name is written once and never again.

    Best-effort by design: if ``fcntl`` is unavailable or the lock can't
    be taken in *timeout*, proceed anyway. An unserialized write is the
    status quo we're improving on — refusing to save the user's name
    would be a worse failure than a rare lost update."""
    lock_path = os.path.join(azt_home(), 'config.lock')
    fh = None
    try:
        import fcntl
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        fh = open(lock_path, 'a+')
        deadline = _time.time() + timeout
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if _time.time() >= deadline:
                    print(f'[collab.settings] config.lock busy for '
                          f'{timeout:.0f}s; writing unserialized',
                          flush=True)
                    break
                _time.sleep(0.05)
    except Exception:
        fh = None
    try:
        yield
    finally:
        if fh is not None:
            try:
                import fcntl as _f
                _f.flock(fh.fileno(), _f.LOCK_UN)
            except Exception:
                pass
            try:
                fh.close()
            except Exception:
                pass


def set_(key, value):
    """Persist a value for *key* in config.json. Refuses to write
    when the existing config.json is unreadable, so a corrupt file
    isn't replaced with a one-key dict that silently reverts every
    other persisted setting."""
    def _mutate(data):
        data[key] = value
    update(_mutate, what=f'set {key!r}')


def _coerce(key, value):
    """Convert *value* to the type implied by the default."""
    default = _DEFAULTS.get(key)
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.lower() in ('1', 'true', 'yes', 'on')
        return bool(value)
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return value


# Convenience accessors
def debounce_ms():
    return max(0, int(get('sync.debounce_ms', 500)))


def merge_retry_max():
    return max(1, min(10, int(get('sync.merge_retry_max', 3))))


def connectivity_poll_s():
    return max(5, int(get('sync.connectivity_poll_s', 30)))


def post_online_grace_s():
    return max(0, int(get('sync.post_online_grace_s', 60)))


def work_offline():
    """Deprecated 0.50: WAN backoff replaces the work_offline
    toggle. Always returns False. The persisted ``sync.work_offline``
    key is left on disk for back-compat with older peers / a
    possible downgrade; this accessor ignores it.

    Callers were either:
      - gating push attempts (now handled by ``wan_backoff``), or
      - surfacing ``S.WORK_OFFLINE_ENABLED`` to the user.
    Both flows go away. The "save power on metered network" use
    case is left to a future per-network policy (see audit doc)."""
    return False


def push_budget_s():
    """Wall-clock cap on the adaptive push loop, in seconds. 0 disables
    the cap (preserves pre-0.43.22 behaviour where the loop only exited
    on logical-attempts cap)."""
    return max(0, int(get('sync.push_budget_s', 300)))


def min_free_mem_mb_for_merge():
    """Minimum ``MemAvailable`` (from ``/proc/meminfo``) the daemon
    will allow before starting a three-way merge. Default 200 MB —
    the LIFT XML parse + merge step itself wants ~100–150 MB peak,
    plus headroom for Python interpreter + dulwich packfile reads.
    0 disables the check (desktop, or if the user knows their RAM
    headroom and accepts OOM-kill risk).

    Surfaced as ``S.INSUFFICIENT_MEMORY_FOR_MERGE`` when the check
    refuses; the next drain cycle re-reads memory and proceeds when
    it recovers. The check is a pre-flight only — we don't track
    memory mid-merge."""
    return max(0, int(get('sync.min_free_mem_mb_for_merge', 200)))


def topic_branch_chunk_size():
    """Initial chunk_n for the Phase A topic-branch push (used when
    ``_all_commits_descend_from`` reports non-FF and the direct push
    to main can't chunk-halve effectively). Lower for slower
    networks; the helper halves adaptively on per-chunk failure
    like the existing direct-push chunking. Default 50 — sized so a
    chunk of average audio-bearing commits is well under GitHub's
    per-request timeout (typical audio commit ~200–500 KB, so 50
    commits ≈ 10–25 MB pack)."""
    return max(1, int(get('sync.topic_branch_chunk_size', 50)))


def large_audio_byte_threshold():
    """Files added or modified in a commit whose size exceeds this
    threshold are flagged as ``S.LARGE_AUDIO_FILE_DETECTED`` and
    written to the daemon log as ``[data-quality]`` lines. The suite
    recorder is for word-list elicitation; legitimate audio files
    should be ~tens to a few hundreds of KB. Files in the multi-MB
    range almost certainly mean someone recorded a phrase or text by
    mistake — worth surfacing so the user can review.

    Default 500 KB (covers a generous ~10 s of 16-bit 48 kHz mono WAV
    or ~40 s of 96 kbps MP3). 0 disables the check. Tune via
    ``data_quality.large_audio_byte_threshold``."""
    return max(0, int(get('data_quality.large_audio_byte_threshold',
                          500 * 1024)))


def commit_pack_byte_budget():
    """First of two gates that surface ``S.COMMIT_PACK_EXCEEDS_NETWORK_BUDGET``
    at ``chunk_n=1`` in Phase A. When the pre-flight estimate for the
    single commit's pack exceeds this budget, the helper bails on the
    first chunk_n=1 failure — no point retrying a unit we've already
    measured as too big. The second gate (second chunk_n=1 failure
    regardless of size) is separate and not tunable.

    Default 3 MB. Empirically (baf field log, 0.44.11) GitHub's
    git-receive-pack edge returns 408 in ~30 s on slow field connections
    even for ~7 MB packs; 3 MB is the conservative envelope that catches
    big audio-heavy commits as clearly too big without rejecting small
    ones that might succeed on the second try. Tune to 0 to disable
    this gate (the second-failure gate still fires). The trace line
    emitted pre-push reports raw bytes regardless of this knob."""
    return max(0, int(get('sync.commit_pack_byte_budget', 3 * 1024 * 1024)))


def log_retention_days():
    """Retention window for daily daemon log files (since 0.52.5).
    Files older than this are pruned on every daemon-startup tee
    install AND on every midnight rotation. Default 3 — matches
    the peer-recorder team's retention so a cross-process bundle
    has parallel coverage. Tune via ``logging.retention_days``.
    Minimum 1 (today's file is never pruned)."""
    return max(1, int(get('logging.retention_days', 3)))


def set_work_offline(value: bool):
    """Persist the work-offline toggle. Triggering an immediate
    drain on transition OFF is the scheduler's responsibility —
    this setter just writes the bit."""
    set_('sync.work_offline', bool(value))


def lan_autodiscovery():
    """Read the daemon-wide LAN auto-discovery toggle (0.50+).
    Default True (in 0.50.0; will flip to False once burst-mode
    discovery lands in 0.50.1). When True, mDNS advertise +
    browse + MulticastLock + FGS run continuously so paired peers
    can find us automatically. When False, none of those run —
    discovery only happens during burst windows triggered by a
    user gesture (and in 0.50.0 the burst-mode fallback isn't
    implemented yet, so False effectively means "LAN off
    entirely").

    Name: "autodiscovery" rather than "passive_discovery" because
    "passive" reads from the user's perspective (the user is
    passive — the device discovers automatically for them).
    True = automatic; False = the user has to nudge.

    Migration from pre-0.50 ``lan.allow_sync``: if the new key
    is absent and the old key is present, return the old value.
    The migration is read-only here — actual rewriting happens
    on the next ``set_lan_autodiscovery`` call. Old peers that
    still write ``lan.allow_sync`` keep working until they
    upgrade."""
    data = _load_raw()
    if isinstance(data, _LoadFailed):
        return _DEFAULTS['lan.autodiscovery']
    if 'lan.autodiscovery' in data:
        return bool(_coerce('lan.autodiscovery',
                            data['lan.autodiscovery']))
    if 'lan.allow_sync' in data:
        # Old key present, new key absent: existing user upgrading.
        # Preserve their setting (an existing 'on' user keeps
        # auto-discovery; an existing 'off' user keeps the same
        # quiet behaviour) — the migration is invisible.
        return bool(data['lan.allow_sync'])
    # Env override on the new key
    env_name = _ENV_MAP.get('lan.autodiscovery')
    if env_name and env_name in os.environ:
        return _coerce('lan.autodiscovery', os.environ[env_name])
    return _DEFAULTS['lan.autodiscovery']


def set_lan_autodiscovery(value: bool):
    """Persist the LAN auto-discovery toggle. Writes the new key;
    leaves any pre-0.50 ``lan.allow_sync`` value untouched so a
    downgrade still finds something sensible. The actual
    start/stop of mDNS + locks is the ``lan_listener`` module's
    responsibility — this setter just writes the bit."""
    set_('lan.autodiscovery', bool(value))


# Back-compat shim: existing callers continue to read
# ``lan_allow_sync``. Phase out the alias once all sites have
# migrated to the new name.
def lan_allow_sync():
    """Deprecated 0.50: use ``lan_autodiscovery``. Returns the
    same value via the migration path."""
    return lan_autodiscovery()


def set_lan_allow_sync(value: bool):
    """Deprecated 0.50: use ``set_lan_autodiscovery``."""
    set_lan_autodiscovery(value)
