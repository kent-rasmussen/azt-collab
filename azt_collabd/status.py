"""
Structured return protocol for all azt_collabd backend ops.

The backend emits Status codes + params; the frontend translates to
displayable strings. No i18n inside the backend.

Status codes are plain uppercase strings (not an Enum) so they
round-trip through JSON cleanly.
"""

from dataclasses import dataclass, field


# ── Success / progress ─────────────────────────────────────────────────────
INITIALIZED = 'INITIALIZED'
ALREADY_INITIALIZED = 'ALREADY_INITIALIZED'
GITIGNORE_CREATED = 'GITIGNORE_CREATED'
COMMITTED = 'COMMITTED'
COMMITTED_LOCAL = 'COMMITTED_LOCAL'
COMMITTED_OFFLINE = 'COMMITTED_OFFLINE'
COMMITTED_NO_REMOTE = 'COMMITTED_NO_REMOTE'
COMMITTED_AND_PUSHED = 'COMMITTED_AND_PUSHED'
NOTHING_TO_COMMIT = 'NOTHING_TO_COMMIT'
# Files in the project's working dir that don't match the audio/
# / images/ / .lift staging filter and therefore won't reach git.
# A peer wrote to an unexpected location — data is on the
# device's daemon-private filesDir but will never be backed up.
# Surface loudly: this is a data-loss-class condition, not silent
# config drift. ``count`` and ``sample`` (up to 5 paths) carried
# in params so a peer's toast / banner can render usefully
# without parsing the daemon log.
DATA_LOSS_RISK = 'DATA_LOSS_RISK'
REMOTE_SET = 'REMOTE_SET'
REMOTE_UPDATED = 'REMOTE_UPDATED'
REMOTE_UNCHANGED = 'REMOTE_UNCHANGED'
REMOTE_REPO_CREATED = 'REMOTE_REPO_CREATED'
PUSHED = 'PUSHED'
PULLED = 'PULLED'
CLONED = 'CLONED'
LIFT_FOUND = 'LIFT_FOUND'
LIFT_NOT_FOUND = 'LIFT_NOT_FOUND'
ON_BRANCH = 'ON_BRANCH'
STAGED_ALL = 'STAGED_ALL'
OPEN_PR = 'OPEN_PR'
NO_AUDIO = 'NO_AUDIO'
NO_REPO = 'NO_REPO'

# Successful response from /v1/projects/<lang>/atomic_commit.
# Carries ``params['bytes_written']`` and ``params['sha256']`` so
# the caller can verify the bytes that landed match the bytes it
# sent. See ``server._h_project_atomic_commit`` and the client
# wrapper ``atomic_commit_bytes``.
ATOMIC_COMMITTED = 'ATOMIC_COMMITTED'


# ── Failures / warnings ────────────────────────────────────────────────────
NOT_A_REPO = 'NOT_A_REPO'
NO_REMOTE = 'NO_REMOTE'
COMMIT_FAILED = 'COMMIT_FAILED'
# Persistent COMMIT_FAILED: the daemon has hit COMMIT_FAILED on
# two-or-more successive commit attempts for this project.
# dulwich's ``porcelain.commit`` essentially only raises on
# persistent conditions (index corruption, refs problem, disk
# full, broken repo state) — a single failure can be a fluke;
# two in a row means the underlying problem is not self-healing
# and the user's data is accumulating on the daemon's filesDir
# without entering git history. Routed peer-side as a never-
# silenced, data-loss-class condition (same bucket as
# DATA_LOSS_RISK). Params carry ``count`` (the running streak)
# and ``error`` (the last dulwich message). Counter persisted
# in projects.json :: <langcode>.commit_failure_count; cleared
# on the next successful commit.
COMMIT_REPEATEDLY_FAILED = 'COMMIT_REPEATEDLY_FAILED'
PUSH_FAILED = 'PUSH_FAILED'
# Both system DNS and the DoH fallback failed to translate a sync
# host (github.com / gitlab.com / etc.) to an IP. Distinct from
# PUSH_FAILED so peers can route silently in the auto-sync path:
# the daemon's scheduler will keep retrying and resolve itself when
# the underlying problem clears, and a toast that says "DNS is
# broken on this device" mid-recording is useless. Real-world
# causes are typically *not* "no internet" (browsers keep working):
# per-app data-restriction, captive-portal limbo, broken
# system-level Private DNS, IPv6-only network where AAAA records
# are missing — see ``net.py`` for the DoH fallback that absorbs
# most of these before this code ever fires. If you see this code,
# *both* the system resolver and Cloudflare DoH-via-1.1.1.1 failed
# for the same hostname.
DNS_RESOLUTION_FAILED = 'DNS_RESOLUTION_FAILED'
# Wall-clock cap on the adaptive push loop (``sync.push_budget_s``,
# default 300 s) was hit before the loop could drain the local
# queue. The local commits stay queued; the next sync run picks
# them up. Params: ``budget_s`` (the cap that fired) and
# ``commits_pending`` (commits still ahead of remote). Distinct
# from PUSH_FAILED so peers can route the user-initiated path
# differently — "we gave up on this attempt, try again later"
# is actionable; the generic PUSH_FAILED + dulwich error blob is
# not. Auto-sync paths silence this code per the auto/user
# contract; user-initiated sync surfaces a toast naming the
# retry-on-next-run behaviour.
SYNC_GIVING_UP_TRANSIENT = 'SYNC_GIVING_UP_TRANSIENT'
PULL_FAILED = 'PULL_FAILED'
CLONE_FAILED = 'CLONE_FAILED'
CLONE_AUTH_REQUIRED = 'CLONE_AUTH_REQUIRED'
BRANCH_ERROR = 'BRANCH_ERROR'
REMOTE_CREATE_FAILED = 'REMOTE_CREATE_FAILED'
BUSY = 'BUSY'
CONFLICTS = 'CONFLICTS'
SERVICE_RESTARTED = 'SERVICE_RESTARTED'
# Scheduler job whose worker died with the previous daemon process
# (e.g. OOM-kill on Android, kill -9 on desktop). The respawned
# daemon's ``reconcile_on_startup`` flips PENDING/RUNNING jobs to
# DONE+JOB_INTERRUPTED so peers polling on a stale job_id receive a
# typed transient-failure result instead of silence. Treat as
# retryable; the underlying operation is idempotent.
JOB_INTERRUPTED = 'JOB_INTERRUPTED'
# Pre-flight memory check failed: ``MemAvailable`` from
# ``/proc/meminfo`` was below ``sync.min_free_mem_mb`` (default 200 MB)
# when ``_merge_diverged`` was about to start. The merge needs ~150 MB
# peak (parsed LIFT XML + merge state) and would OOM-kill the
# ``:provider`` service silently if it ran. Params: ``mem_available_mb``,
# ``min_required_mb``. Treat as transient + retryable; next drain cycle
# re-reads memory and proceeds when it recovers. Distinct from
# PULL_FAILED so peers can route silently in the auto-sync path —
# nothing the user can do mid-session, the daemon will retry.
INSUFFICIENT_MEMORY_FOR_MERGE = 'INSUFFICIENT_MEMORY_FOR_MERGE'
# Topic-branch (``azt-pending-<lang>-<device>``) we use for chunked
# upload of diverged history already exists on the server with content
# that isn't an ancestor of the SHA we want to push. Most likely cause:
# two devices share the same ``device_name`` and are stepping on each
# other's topic-branch refs. The daemon refuses to force-push someone
# else's work and surfaces this so the user can set a unique device
# name. Params: ``topic_branch`` (the ref name), ``server_tip``
# (the foreign SHA we saw, hex prefix). Since 0.44.8.
TOPIC_BRANCH_CONFLICT = 'TOPIC_BRANCH_CONFLICT'

# ── 403 diagnosis ──────────────────────────────────────────────────────────
AUTH_REQUIRED = 'AUTH_REQUIRED'
APP_NOT_INSTALLED = 'APP_NOT_INSTALLED'
APP_SUSPENDED = 'APP_SUSPENDED'
REPO_NOT_AUTHORIZED = 'REPO_NOT_AUTHORIZED'
ACCESS_DENIED = 'ACCESS_DENIED'
# Refresh-token broken: the daemon attempted a proactive
# refresh against the GitHub OAuth endpoint and got
# ``incorrect_client_credentials`` (or any other refresh-side
# failure). The current access token still works until its
# 8h-from-issuance expiry; ``params['expires_at']`` carries that
# unix timestamp so peers can format a deadline-aware toast in
# the user-initiated sync path (per the auto/user contract in
# azt_collab_client/CLAUDE.md). Auto-sync ignores this status;
# user-initiated sync surfaces "re-auth by <deadline>" so the
# user can act before the access token cliff.
AUTH_REFRESH_STALE = 'AUTH_REFRESH_STALE'

# ── Device flow ────────────────────────────────────────────────────────────
AUTH_EXPIRED = 'AUTH_EXPIRED'
AUTH_DENIED = 'AUTH_DENIED'
AUTH_TIMEOUT = 'AUTH_TIMEOUT'

# ── Collaborator grant ─────────────────────────────────────────────────────
# Outcomes from POST /v1/projects/<lang>/collaborators. Wraps the GitHub
# PUT /repos/.../collaborators/{user} call: 201 → COLLABORATOR_INVITED,
# 204 or 422 → COLLABORATOR_ALREADY (collaborator or pending invite).
COLLABORATOR_INVITED = 'COLLABORATOR_INVITED'
COLLABORATOR_ALREADY = 'COLLABORATOR_ALREADY'
COLLABORATOR_INVITE_FAILED = 'COLLABORATOR_INVITE_FAILED'
INVALID_USERNAME = 'INVALID_USERNAME'
NOT_GITHUB_REMOTE = 'NOT_GITHUB_REMOTE'

# ── Work-offline mode ──────────────────────────────────────────────────────
# Returned from the user-initiated sync path (POST /v1/projects/<lang>/sync,
# i.e. the Sync button) when the daemon-wide ``sync.work_offline`` toggle
# is on. Peers route this as: toast "Work-offline mode is on" + navigate
# to the daemon settings screen anchored on the toggle (same pattern as
# AUTH_REQUIRED → credentials, NOT_A_REPO → publish flow). Auto-sync
# paths silently no-op on this code per the auto/user contract.
# Commit endpoints (commit_project) ignore the toggle — only push is
# suppressed.
WORK_OFFLINE_ENABLED = 'WORK_OFFLINE_ENABLED'

# ── Contributor identity unset ─────────────────────────────────────────────
# Returned by commit-issuing endpoints (init / sync / sync_async) when
# ``store.get_contributor()`` is empty. Pre-0.40 the daemon silently
# substituted the literal ``'Recorder'`` for missing names, producing
# meaningless "Recorder" commits in GitHub history. 0.40 forces the
# unset state to be user-visible: every commit op refuses with this
# status, peers route the user to set their name via
# ``set_contributor`` (typically through the daemon settings UI).
CONTRIBUTOR_UNSET = 'CONTRIBUTOR_UNSET'

# Transport-failure codes. Mirror of the client-side constants
# (the daemon doesn't emit these — they're produced by the
# client wrappers' transport-failure branches — but keeping the
# mirror complete avoids divergence between the two ``status``
# modules and lets any daemon-internal code that walks status
# codes recognise them as known constants.
SERVER_UNAVAILABLE = 'SERVER_UNAVAILABLE'
SERVER_ERROR = 'SERVER_ERROR'
# Returned by ``restart_server()`` after a successful
# ``POST /v1/admin/restart``. Informational — the daemon accepted
# the request and the restart is in flight. params: ``transport``
# = ``'desktop' | 'android' | 'unknown'``.
RESTARTING = 'RESTARTING'


@dataclass
class Status:
    code: str
    params: dict = field(default_factory=dict)

    def to_dict(self):
        return {'code': self.code, 'params': dict(self.params)}

    @classmethod
    def from_dict(cls, d):
        return cls(code=d.get('code', ''),
                   params=dict(d.get('params') or {}))


@dataclass
class Result:
    statuses: list = field(default_factory=list)

    def add(self, code, **params):
        self.statuses.append(Status(code=code, params=params))
        return self

    def has(self, code):
        return any(s.code == code for s in self.statuses)

    def has_any(self, *codes):
        return any(s.code in codes for s in self.statuses)

    def codes(self):
        return [s.code for s in self.statuses]

    def to_dict(self):
        return {'statuses': [s.to_dict() for s in self.statuses]}

    @classmethod
    def from_dict(cls, d):
        return cls(statuses=[Status.from_dict(s)
                             for s in d.get('statuses', [])])


class AuthError(Exception):
    """Raised from device flow helpers. Carries a Status (AUTH_EXPIRED,
    AUTH_DENIED, AUTH_TIMEOUT) so the UI can translate for display."""

    def __init__(self, status):
        super().__init__(status.code)
        self.status = status
