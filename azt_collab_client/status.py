"""Client-side mirror of azt_collabd.status (Status/Result dataclasses
and code constants). Duplicated intentionally so azt_collab_client stays
independent of the server package.
"""

from dataclasses import dataclass, field


# Keep these in sync with azt_collabd/status.py.
INITIALIZED = 'INITIALIZED'
ALREADY_INITIALIZED = 'ALREADY_INITIALIZED'
GITIGNORE_CREATED = 'GITIGNORE_CREATED'
COMMITTED = 'COMMITTED'
COMMITTED_LOCAL = 'COMMITTED_LOCAL'
COMMITTED_OFFLINE = 'COMMITTED_OFFLINE'
COMMITTED_NO_REMOTE = 'COMMITTED_NO_REMOTE'
COMMITTED_AND_PUSHED = 'COMMITTED_AND_PUSHED'
NOTHING_TO_COMMIT = 'NOTHING_TO_COMMIT'
# Files written to the daemon's project dir that don't fall under
# any staging filter (audio/, images/, .lift) — peer wrote to an
# unexpected location and the file will never reach git. Surfaced
# loudly by the daemon as a data-loss-class signal. params carry
# ``count`` and ``sample`` (up to 5 paths) so peers can render a
# user-actionable toast / banner urging "Please send your daemon
# log" without parsing the log file.
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

# Successful response from /v1/projects/<lang>/atomic_commit (daemon
# 0.36.0+). Carries ``params['bytes_written']`` and ``params['sha256']``.
ATOMIC_COMMITTED = 'ATOMIC_COMMITTED'

NOT_A_REPO = 'NOT_A_REPO'
NO_REMOTE = 'NO_REMOTE'
COMMIT_FAILED = 'COMMIT_FAILED'
# Two-or-more successive COMMIT_FAILED for this project. Routed
# never-silenced peer-side — same bucket as DATA_LOSS_RISK because
# the user's data is accumulating on the daemon's filesDir without
# entering git history. Params: ``count`` (running streak),
# ``error`` (last dulwich message). Counter cleared on the next
# successful commit. See azt_collabd/status.py for the full
# rationale.
COMMIT_REPEATEDLY_FAILED = 'COMMIT_REPEATEDLY_FAILED'
PUSH_FAILED = 'PUSH_FAILED'
# Both system DNS and the daemon's DoH fallback failed to resolve
# the sync host. See azt_collabd/status.py for the full rationale.
# Peers should route this **silent on auto-sync** (same bucket as
# PUSH_FAILED on network-class failures) — the daemon will retry
# automatically when the underlying issue clears. On user-initiated
# Sync, route to an informational toast telling the user to check
# device DNS / VPN / restricted-data settings; do not navigate.
DNS_RESOLUTION_FAILED = 'DNS_RESOLUTION_FAILED'
PULL_FAILED = 'PULL_FAILED'
CLONE_FAILED = 'CLONE_FAILED'
CLONE_AUTH_REQUIRED = 'CLONE_AUTH_REQUIRED'
BRANCH_ERROR = 'BRANCH_ERROR'
REMOTE_CREATE_FAILED = 'REMOTE_CREATE_FAILED'
BUSY = 'BUSY'
CONFLICTS = 'CONFLICTS'
SERVICE_RESTARTED = 'SERVICE_RESTARTED'
JOB_INTERRUPTED = 'JOB_INTERRUPTED'

AUTH_REQUIRED = 'AUTH_REQUIRED'
APP_NOT_INSTALLED = 'APP_NOT_INSTALLED'
APP_SUSPENDED = 'APP_SUSPENDED'
REPO_NOT_AUTHORIZED = 'REPO_NOT_AUTHORIZED'
ACCESS_DENIED = 'ACCESS_DENIED'
# Carries ``params['expires_at']`` — unix timestamp at which the
# current GitHub access token expires (token_time + 8h). See the
# daemon's status.py for the full rationale and the peer-side
# contract section of CLAUDE.md.
AUTH_REFRESH_STALE = 'AUTH_REFRESH_STALE'

AUTH_EXPIRED = 'AUTH_EXPIRED'
AUTH_DENIED = 'AUTH_DENIED'
AUTH_TIMEOUT = 'AUTH_TIMEOUT'

COLLABORATOR_INVITED = 'COLLABORATOR_INVITED'
COLLABORATOR_ALREADY = 'COLLABORATOR_ALREADY'
COLLABORATOR_INVITE_FAILED = 'COLLABORATOR_INVITE_FAILED'
INVALID_USERNAME = 'INVALID_USERNAME'
NOT_GITHUB_REMOTE = 'NOT_GITHUB_REMOTE'

# Returned from the user-initiated sync path (Sync button) when the
# daemon-wide ``sync.work_offline`` toggle is on. Peers route this as:
# toast "Work-offline mode is on" + navigate to the daemon settings
# screen (open_server_ui()) so the user can toggle it off. Auto-sync
# paths silently no-op on this code per the auto/user contract. The
# commit endpoint (``commit_project``) ignores the toggle entirely —
# only push is suppressed.
WORK_OFFLINE_ENABLED = 'WORK_OFFLINE_ENABLED'

# Returned by commit-issuing endpoints (init / sync / sync_async)
# when the daemon's stored contributor name is empty. Peers must
# route the user to ``set_contributor`` (typically through the
# daemon settings UI) before any further sync/init can land. Pre-
# 0.40 the daemon silently substituted ``'Recorder'``; that's gone
# now and unset state surfaces explicitly.
CONTRIBUTOR_UNSET = 'CONTRIBUTOR_UNSET'

# Transport-failure codes. The client wrappers' ``except
# ServerUnavailable`` / non-``ok`` response branches (see
# ``sync_project``, ``project_status``, etc. in ``__init__.py``)
# already emit ``Status('SERVER_UNAVAILABLE', …)`` /
# ``Status('SERVER_ERROR', …)`` as string literals, so the
# values land on results correctly. Exporting the constants
# here so peer code can route via
# ``result.has_any(S.SERVER_UNAVAILABLE, S.SERVER_ERROR)`` per
# CLAUDE.md's "Peer contract: routing on sync results" table
# without inlining the string literal and losing the typing
# aid.
SERVER_UNAVAILABLE = 'SERVER_UNAVAILABLE'
SERVER_ERROR = 'SERVER_ERROR'


@dataclass
class Status:
    code: str
    params: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d):
        return cls(code=d.get('code', ''),
                   params=dict(d.get('params') or {}))


@dataclass
class Result:
    statuses: list = field(default_factory=list)

    def has(self, code):
        return any(s.code == code for s in self.statuses)

    def has_any(self, *codes):
        return any(s.code in codes for s in self.statuses)

    def codes(self):
        return [s.code for s in self.statuses]

    @classmethod
    def from_dict(cls, d):
        return cls(statuses=[Status.from_dict(s)
                             for s in (d or {}).get('statuses', [])])
