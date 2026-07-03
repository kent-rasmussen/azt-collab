"""Client-side Project dataclass (decode only). Mirrors
azt_collabd.projects.Project."""

from dataclasses import dataclass


@dataclass
class Project:
    langcode: str
    working_dir: str
    lift_path: str = ''
    remote_url: str = ''
    # ``last_commit`` is the timestamp of the most recent local commit
    # the daemon wrote (any of COMMITTED_LOCAL / COMMITTED_NO_REMOTE /
    # COMMITTED_AND_PUSHED). ``last_sync`` is the most recent
    # successful remote contact (PUSHED / PULLED /
    # COMMITTED_AND_PUSHED). Peers render the more recent of the two
    # so the user can distinguish "committed but not yet pushed" from
    # "fully backed up". Defaults to 0.0 for forward-compat with pre-
    # 0.19 daemons that don't emit ``last_commit``.
    last_commit: float = 0.0
    last_sync: float = 0.0
    created_at: float = 0.0
    # True iff the daemon could stat the project's LIFT file at the
    # time of the API response. Peers should check this before
    # handing lift_path to LiftHandle — a stale projects.json entry
    # whose underlying file was deleted out-of-band would otherwise
    # surface as a not-found at open time. Defaults to True for
    # forward-compat with pre-0.16 daemons that don't emit the flag.
    lift_exists: bool = True
    # Per-project CAWL image source (``owner/repo``). Empty → the
    # project falls back to the daemon-global default; consumers
    # generally shouldn't need to read this directly (the
    # ``cawl_index(langcode)`` / ``CAWLHandle`` wrappers resolve it
    # internally). Defaults to '' for forward-compat with pre-0.38
    # daemons that don't emit it.
    cawl_image_repo: str = ''
    # Per-project GitHub repo-name override for the publish path.
    # Empty → callers (recorder CollabScreen, future peers) treat
    # as equal to ``langcode``. Non-empty → user explicitly chose a
    # different repo name. Defaults to '' for forward-compat with
    # pre-0.39 daemons that don't emit it.
    repo_slug: str = ''

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(
            langcode=d.get('langcode', ''),
            working_dir=d.get('working_dir', ''),
            lift_path=d.get('lift_path', ''),
            remote_url=d.get('remote_url', ''),
            last_commit=float(d.get('last_commit', 0.0)),
            last_sync=float(d.get('last_sync', 0.0)),
            created_at=float(d.get('created_at', 0.0)),
            lift_exists=bool(d.get('lift_exists', True)),
            cawl_image_repo=d.get('cawl_image_repo', '') or '',
            repo_slug=d.get('repo_slug', '') or '',
        )


@dataclass
class ProjectStatus:
    """Snapshot of one project's git state."""
    langcode: str
    branch: str
    remote_url: str
    n_changes: int
    last_commit: float
    last_sync: float
    working_dir: str
    lift_path: str
    # Sync-status counts (v0.47.0). Three independent walks over the
    # local commit graph; render via the 5-state recipe in
    # CLIENT_INTEGRATION.md § 17b. Pre-0.47 daemons emitted
    # ``commits_ahead`` + ``unshared_commits``; those fields are
    # gone (MIN_SERVER_VERSION enforces). Defaults of 0 here are
    # for *missing fields on stub responses*, not for old-daemon
    # tolerance.
    #
    #   wan_unshared — commits not on github (was commits_ahead).
    #     Special-cased for LAN-only projects: walks from HEAD
    #     when no origin URL is configured, surfacing the whole
    #     history as a friction signal for "no github backup."
    #   lan_unshared — commits not on any paired peer's
    #     ``last_seen_main``. Returns 0 when no peers are paired
    #     (the "nothing to be behind on" convention).
    #   at_risk      — commits on neither channel (set intersection
    #     of wan_unshared and lan_unshared as commit sets). Zero
    #     except in state E ("both behind on the same commits"),
    #     which is the routine transient state right after a
    #     fresh commit.
    wan_unshared: int = 0
    lan_unshared: int = 0
    at_risk: int = 0
    # Per-project metadata mirrored from the project record so
    # peers can read status + identity in one round-trip. Empty
    # for forward-compat with pre-0.39 daemons.
    repo_slug: str = ''
    cawl_image_repo: str = ''
    # Stuck-commit telemetry (since daemon 0.41.27). Running
    # streak of successive COMMIT_FAILED, last failure timestamp
    # (unix), last dulwich error message. Peers polling
    # ``project_status`` SHOULD surface ``COMMIT_REPEATEDLY_FAILED``
    # when ``commit_failure_count >= 2`` — matches the daemon's
    # threshold and catches the case where the daemon's scheduler
    # retried in the background (no fresh sync result delivered to
    # the peer). Counter clears on the next successful commit.
    commit_failure_count: int = 0
    last_commit_failure_at: float = 0.0
    last_commit_error: str = ''
    # Access-class reason the last WAN *sync* failed (daemon 0.52.24+):
    # a typed status CODE — ``REPO_NO_ACCESS`` / ``AUTH_REQUIRED`` /
    # ``REPO_NOT_AUTHORIZED`` / ``APP_SUSPENDED`` / ``ACCESS_DENIED`` /
    # ``AUTH_REFRESH_STALE`` — or ``''`` when the last sync had no access
    # problem. Persistent (survives restart), cleared on the next
    # successful sync or an auto-accepted invite. Lets a peer show a
    # standing "sync blocked: <reason>" banner instead of silently
    # backing off; route ``REPO_NO_ACCESS`` to ``repo_access_popup``.
    last_sync_error: str = ''
    last_sync_error_at: float = 0.0
    # Atomic-recovery diagnostic counter (daemon 0.41.27+). The
    # daemon auto-merges orphaned ``.azt_atomic_pending/<token>``
    # LIFT scratches into the current LIFT in the background;
    # this is the count of recoveries that landed today. Zero
    # on a healthy project. Purely informational — peers needing
    # a "we rescued some unsaved work today" diagnostic banner
    # can render this number; recovered entries are already on
    # disk (and the conflicts, if any, are flagged with
    # ``<annotation name="azt-lift-conflict">`` the same way as
    # cross-peer merge conflicts).
    n_recovered_today: int = 0
    # Daemon-wide work-offline toggle. True means automatic push
    # is suppressed (the watcher's drain is a no-op,
    # ``sync_project`` returns ``S.WORK_OFFLINE_ENABLED``);
    # ``commit_project`` is unaffected. Carried on every
    # ``project_status`` even though it's daemon-wide so peers
    # don't need a second RPC to render the badge. Since 0.43.0.
    # Rendering recipe: CLIENT_INTEGRATION.md § 17b.
    work_offline: bool = False
    # Daemon-wide LAN-sync toggle (independent of ``work_offline``).
    # Combines with ``work_offline`` into the four-cell matrix the
    # sync indicator renders as suffix ``''`` / ``offline`` /
    # ``LAN-only``. Since 0.45.0. Rendering recipe:
    # CLIENT_INTEGRATION.md § 17b.
    lan_allow_sync: bool = False
    # SHA hex of the most recent commit successfully LAN-delivered
    # to at least one paired peer. Empty when nothing has been
    # LAN-delivered yet. Diagnostic only — ``lan_unshared`` and
    # ``at_risk`` are what drive the indicator (was the conflated
    # ``unshared_commits`` pre-0.47.0). Since 0.45.0.
    lan_pushed_sha: str = ''
    # SHA hex of the project's current HEAD. Changes on every HEAD
    # advance — local commit, incoming LAN receive-pack,
    # post-receive merge — so peers polling ``project_status`` can
    # use a change here as a uniform "the daemon's view of this
    # project moved; re-read content" signal, independent of which
    # event caused the move. Empty string when no commits exist
    # yet (pre-init, or pre-first-commit project). Since 0.45.45.
    # See CLIENT_INTEGRATION.md § 17b Background refresh obligation.
    head_sha: str = ''
    # Foreign-device topic-branch orphan count. Number of
    # ``refs/remotes/origin/azt-pending-*`` refs whose
    # device-name suffix isn't this daemon's — i.e. cross-device
    # orphans the local janitor can't safely sweep. Diagnostic
    # only; non-zero means "another device's incomplete push is
    # still parked on this remote." Peers may surface as a
    # "remote has leftover branches" indicator in a sync-detail
    # screen; it's not a sync-blocking condition. Since 0.50.15.
    foreign_topic_orphan_count: int = 0

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(
            langcode=d.get('langcode', ''),
            branch=d.get('branch', ''),
            remote_url=d.get('remote_url', ''),
            n_changes=int(d.get('n_changes', 0)),
            last_commit=float(d.get('last_commit', 0.0)),
            last_sync=float(d.get('last_sync', 0.0)),
            working_dir=d.get('working_dir', ''),
            lift_path=d.get('lift_path', ''),
            wan_unshared=int(d.get('wan_unshared', 0) or 0),
            lan_unshared=int(d.get('lan_unshared', 0) or 0),
            at_risk=int(d.get('at_risk', 0) or 0),
            repo_slug=d.get('repo_slug', '') or '',
            cawl_image_repo=d.get('cawl_image_repo', '') or '',
            commit_failure_count=int(
                d.get('commit_failure_count', 0) or 0),
            last_commit_failure_at=float(
                d.get('last_commit_failure_at', 0.0) or 0.0),
            last_commit_error=d.get('last_commit_error', '') or '',
            last_sync_error=d.get('last_sync_error', '') or '',
            last_sync_error_at=float(
                d.get('last_sync_error_at', 0.0) or 0.0),
            n_recovered_today=int(
                d.get('n_recovered_today', 0) or 0),
            work_offline=bool(d.get('work_offline', False)),
            lan_allow_sync=bool(d.get('lan_allow_sync', False)),
            lan_pushed_sha=str(d.get('lan_pushed_sha', '') or ''),
            head_sha=str(d.get('head_sha', '') or ''),
            foreign_topic_orphan_count=int(
                d.get('foreign_topic_orphan_count', 0) or 0),
        )
