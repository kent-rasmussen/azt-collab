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
# Per-URL success/failure on each entry in ``Project.extra_remotes``.
# Emitted after the primary push so "Use both" projects can show
# partial state. Params: ``url`` (+ ``branch`` on success;
# ``error`` on failure). Auto-sync paths route silent; user-Sync
# may surface a "1 of 2 remotes received the commits" toast.
# Daemon 0.49.2+. See azt_collabd/status.py for the full rationale.
EXTRA_REMOTE_PUSHED = 'EXTRA_REMOTE_PUSHED'
EXTRA_REMOTE_PUSH_FAILED = 'EXTRA_REMOTE_PUSH_FAILED'
PULLED = 'PULLED'
CLONED = 'CLONED'
LIFT_FOUND = 'LIFT_FOUND'
LIFT_NOT_FOUND = 'LIFT_NOT_FOUND'
# Clone landed but the repo is empty (no files at all) — usually the
# first upload never completed. See azt_collabd/status.py.
REPO_EMPTY = 'REPO_EMPTY'
# clone_repo reused a real prior clone at the destination instead of
# wiping it. Params: dir. See azt_collabd/status.py.
CLONE_REUSED_EXISTING = 'CLONE_REUSED_EXISTING'
ON_BRANCH = 'ON_BRANCH'
STAGED_ALL = 'STAGED_ALL'
OPEN_PR = 'OPEN_PR'
NO_AUDIO = 'NO_AUDIO'
NO_REPO = 'NO_REPO'

# Successful response from /v1/projects/<lang>/atomic_commit (daemon
# 0.36.0+). Carries ``params['bytes_written']`` and ``params['sha256']``.
ATOMIC_COMMITTED = 'ATOMIC_COMMITTED'

# ``submit_file`` (daemon 0.53.0+) took the divergent path: a peer
# merge landed since the caller's declared ``base_sha``, so the
# daemon three-way-merged the submitted bytes with HEAD instead of
# plain-replacing. Peer routing: the save succeeded and nothing was
# lost, but in-memory state is stale — reload the file before
# further edits. Params: ``n_conflicts``, ``base_sha``. See
# ``azt_collabd/status.py`` for the full rationale.
MERGED_WITH_LOCAL = 'MERGED_WITH_LOCAL'
# merge_ref admin op (0.54.30) — mirror of azt_collabd/status.py.
# Params: ``langcode``, ``sha``, ``n_conflicts``.
MERGED_REF = 'MERGED_REF'

# Surgical LIFT edits (daemon 0.50.29+). See ``azt_collabd/status.py``
# for the full rationale. The ``NO_CHANGE`` variants let peers
# suppress redundant UI updates when the target already had the new
# value (e.g., a re-save of the same audio filename).
AUDIO_SET = 'AUDIO_SET'
AUDIO_SET_NO_CHANGE = 'AUDIO_SET_NO_CHANGE'
ILLUSTRATION_SET = 'ILLUSTRATION_SET'
ILLUSTRATION_SET_NO_CHANGE = 'ILLUSTRATION_SET_NO_CHANGE'
ENTRY_NOT_FOUND = 'ENTRY_NOT_FOUND'
LIFT_INVALID = 'LIFT_INVALID'

NOT_A_REPO = 'NOT_A_REPO'
NO_REMOTE = 'NO_REMOTE'
COMMIT_FAILED = 'COMMIT_FAILED'
# forget_project (0.54.25) — mirror of azt_collabd/status.py.
NOT_A_PROJECT = 'NOT_A_PROJECT'
PROJECT_FORGOTTEN = 'PROJECT_FORGOTTEN'
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
# Wall-clock cap on the daemon's push loop (``sync.push_budget_s``,
# default 300 s) was hit before the loop could drain the queue. The
# pending commits stay queued; the next sync run picks them up.
# Params: ``budget_s`` (the cap that fired), ``commits_pending``
# (commits still ahead of remote). Route silent on auto-sync (next
# scheduled run picks up where this left off); on user-initiated
# sync, surface a toast naming the retry-on-next-run behaviour —
# distinct from PUSH_FAILED + dulwich-error-blob which is what
# pre-0.43.22 surfaced after a 30-minute hang.
SYNC_GIVING_UP_TRANSIENT = 'SYNC_GIVING_UP_TRANSIENT'
PULL_FAILED = 'PULL_FAILED'
CLONE_FAILED = 'CLONE_FAILED'
CLONE_AUTH_REQUIRED = 'CLONE_AUTH_REQUIRED'
BRANCH_ERROR = 'BRANCH_ERROR'
REMOTE_CREATE_FAILED = 'REMOTE_CREATE_FAILED'
# Informational: publish skipped repo auto-creation because the URL's
# owner is not the authenticated user (typical when the URL was
# adopted from a LAN peer). Push proceeds against the URL as-is;
# success depends on the authenticated user being a collaborator on
# the target. Params: ``owner``, ``username``, ``url``. Since 0.50.27.
# See azt_collabd/status.py for the full rationale.
REMOTE_OWNER_MISMATCH_SKIP_CREATE = 'REMOTE_OWNER_MISMATCH_SKIP_CREATE'
BUSY = 'BUSY'
CONFLICTS = 'CONFLICTS'
SERVICE_RESTARTED = 'SERVICE_RESTARTED'
JOB_INTERRUPTED = 'JOB_INTERRUPTED'
# Daemon refused a three-way merge because the device's free
# memory (``MemAvailable`` from /proc/meminfo) was below
# ``sync.min_free_mem_mb_for_merge`` (default 200 MB) — preserving
# the chance the merge would have OOM-killed the ``:provider``
# service. Params: ``mem_available_mb`` (int), ``min_required_mb``
# (int). Treat as transient + retryable; next drain cycle re-reads
# memory. Routing contract: silent in auto-sync (nothing the user
# can fix mid-recording), translated toast in user-initiated sync.
# 0.44.4+.
INSUFFICIENT_MEMORY_FOR_MERGE = 'INSUFFICIENT_MEMORY_FOR_MERGE'
# Topic-branch (used for chunked upload of diverged history) already
# exists on the server with foreign content (a SHA we don't recognize).
# Two devices probably share a device_name. Surfaced by sync_project /
# the auto-sync drain when the topic-branch push refuses to force-push.
# Params: ``topic_branch``, ``server_tip`` (hex prefix). User fix:
# change device_name to something unique in the daemon settings UI.
# Since 0.44.8.
TOPIC_BRANCH_CONFLICT = 'TOPIC_BRANCH_CONFLICT'
# Phase A chunk-halving has bottomed out at chunk_n=1 and one of two
# gates tripped: pack estimate > budget (default 3 MB) OR second
# chunk_n=1 failure regardless of size. Either way the bytes that
# need to cross the wire for one commit don't fit inside the server's
# per-request timeout on this connection. Params: ``commit_sha``
# (hex prefix), ``raw_bytes`` (estimate, pre-compression upper
# bound), ``budget_bytes``, ``object_count``. Genuine remedies are
# a faster connection or moving audio out of git history (LFS /
# external store); the daemon can't work around it. Since 0.44.11.
COMMIT_PACK_EXCEEDS_NETWORK_BUDGET = 'COMMIT_PACK_EXCEEDS_NETWORK_BUDGET'
# Sub-commit pre-seeding can't help: a single blob's size
# (uncompressed) exceeds the per-attempt budget. The
# topic-push tried to upload the commit's blobs into side refs
# so the eventual commit-pack would fit — but at least one
# blob is bigger than the budget alone. Params: ``blob_sha``
# (hex prefix), ``blob_bytes``, ``budget_bytes``. Remedies:
# bigger budget, faster connection, or move that file out of
# git history. Surfaces in place of
# ``COMMIT_PACK_EXCEEDS_NETWORK_BUDGET`` when the daemon has
# identified the specific oversized blob. Since 0.52.4.
BLOB_EXCEEDS_BUDGET = 'BLOB_EXCEEDS_BUDGET'
# Data-quality flag: a just-made commit contains a file whose size
# exceeds ``data_quality.large_audio_byte_threshold`` (default 500 KB).
# The recorder is for word-list elicitation; multi-MB files almost
# always mean a phrase / text was recorded by mistake. Params:
# ``path``, ``bytes``, ``threshold``, ``commit_sha``. Informational.
# Since 0.44.11.
LARGE_AUDIO_FILE_DETECTED = 'LARGE_AUDIO_FILE_DETECTED'

AUTH_REQUIRED = 'AUTH_REQUIRED'
APP_NOT_INSTALLED = 'APP_NOT_INSTALLED'
APP_SUSPENDED = 'APP_SUSPENDED'
REPO_NOT_AUTHORIZED = 'REPO_NOT_AUTHORIZED'
ACCESS_DENIED = 'ACCESS_DENIED'
# Mirror of azt_collabd.status.REPO_NO_ACCESS (0.52.24). 404 /
# NotGitRepository from a git op with a valid token — the repo can't
# be seen with this account (private-not-shared / not-a-collaborator /
# app-not-granted / wrong name; GitHub can't disambiguate). Distinct
# from ACCESS_DENIED (403 branch) and APP_NOT_INSTALLED (unverifiable
# from a 404). Never emitted when there are no credentials at all.
REPO_NO_ACCESS = 'REPO_NO_ACCESS'
# Mirror of azt_collabd.status.INVITE_ACCEPTED (0.52.24). Daemon auto-
# accepted a pending GitHub repo invitation for this repo; transient/
# retryable — access should now work on the next attempt.
INVITE_ACCEPTED = 'INVITE_ACCEPTED'
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

# ── LAN sync transport (parked spec, phases 1-8) ───────────────────────────
# Mirror of the daemon-side codes in ``azt_collabd/status.py``. See
# that file for the full per-code rationale.
LAN_PAIRED = 'LAN_PAIRED'
LAN_UNPAIRED = 'LAN_UNPAIRED'
LAN_PEER_UNREACHABLE = 'LAN_PEER_UNREACHABLE'
LAN_FP_MISMATCH = 'LAN_FP_MISMATCH'
LAN_TOGGLE_OFF = 'LAN_TOGGLE_OFF'
# Socket timeout during the LAN clone's packfile transfer.
# Params: peer_id, langcode, timeout_s. See azt_collabd/status.py.
LAN_CLONE_TIMEOUT = 'LAN_CLONE_TIMEOUT'
# THIS side's TLS layer failed on a missing/unreadable local file
# (LAN-identity peer_id/peer.crt, typically) — NOT a network/peer
# problem. Params: peer_id, detail. See azt_collabd/status.py.
LAN_LOCAL_TLS_ERROR = 'LAN_LOCAL_TLS_ERROR'
# The peer answered but its listener refused to serve the repo (not
# shared with this device / not registered there). Params: peer_id,
# langcode, detail. See azt_collabd/status.py.
LAN_PROJECT_NOT_SHARED = 'LAN_PROJECT_NOT_SHARED'

# Combined-pair-share-clone flow codes — see ``azt_collabd/status.py``
# for the full per-code rationale.
LAN_PROJECT_CLONED = 'LAN_PROJECT_CLONED'
LAN_PROJECT_REOPENED = 'LAN_PROJECT_REOPENED'
LAN_PROJECT_ADOPTED_REMOTE = 'LAN_PROJECT_ADOPTED_REMOTE'
LAN_PROJECT_COLLISION_UNRELATED = 'LAN_PROJECT_COLLISION_UNRELATED'
# Merge-time counterpart (0.54.19): tips share no git ancestor —
# two different projects under one langcode; merge refused,
# nothing changed. Params: ``error``.
MERGE_UNRELATED_HISTORIES = 'MERGE_UNRELATED_HISTORIES'
LAN_ADOPT_ORIGIN_NEEDED = 'LAN_ADOPT_ORIGIN_NEEDED'
LAN_REMOTE_CONFLICT = 'LAN_REMOTE_CONFLICT'
LAN_SHARE_OFFER = 'LAN_SHARE_OFFER'
LAN_SHARE_DECLINED = 'LAN_SHARE_DECLINED'
LAN_OFFER_ACCEPTED = 'LAN_OFFER_ACCEPTED'
# Sender-side outcomes for the user-tap share gesture — see
# azt_collabd/status.py for the per-code body.
LAN_OFFER_DELIVERED = 'LAN_OFFER_DELIVERED'
LAN_OFFER_NOT_DELIVERED = 'LAN_OFFER_NOT_DELIVERED'
PROJECT_NOT_INITIALISED = 'PROJECT_NOT_INITIALISED'
PROJECT_UNBORN = 'PROJECT_UNBORN'
PEER_UNKNOWN = 'PEER_UNKNOWN'

# Nearby-pair flow — see azt_collabd/status.py for the per-code
# rationale. Sender's outbound request lives 5 min then times out;
# receiver Accept/Decline routes through the shared decisions
# watcher (kind=pair_request).
LAN_PAIR_REQUEST_PENDING = 'LAN_PAIR_REQUEST_PENDING'
LAN_PAIR_REQUEST_ACCEPTED = 'LAN_PAIR_REQUEST_ACCEPTED'
LAN_PAIR_REQUEST_DECLINED = 'LAN_PAIR_REQUEST_DECLINED'
LAN_PAIR_REQUEST_TIMEOUT = 'LAN_PAIR_REQUEST_TIMEOUT'

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
# Returned by ``restart_server()`` after a successful
# ``POST /v1/admin/restart``. Informational — the daemon accepted
# the request and the restart is in flight. params: ``transport``
# = ``'desktop' | 'android' | 'unknown'``.
RESTARTING = 'RESTARTING'


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

    def param(self, code, key, default=None):
        """Value of ``params[key]`` on the first status matching
        *code*, or *default*. Convenience for single-value reads
        like ``result.param(S.COMMITTED_LOCAL, 'head_sha', '')``.
        Mirror of ``azt_collabd/status.py``."""
        for s in self.statuses:
            if s.code == code:
                return s.params.get(key, default)
        return default

    @classmethod
    def from_dict(cls, d):
        return cls(statuses=[Status.from_dict(s)
                             for s in (d or {}).get('statuses', [])])
