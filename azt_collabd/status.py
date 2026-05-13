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
PUSH_FAILED = 'PUSH_FAILED'
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
