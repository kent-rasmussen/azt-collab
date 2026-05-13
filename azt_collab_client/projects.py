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
    # Number of local commits on the current branch not yet pushed
    # to the remote — the count peers display as "(+n)" alongside
    # last_sync. Defaults to 0 for forward-compat with daemons that
    # don't yet emit it (see NOTES_TO_DAEMON.md).
    commits_ahead: int = 0
    # Per-project metadata mirrored from the project record so
    # peers can read status + identity in one round-trip. Empty
    # for forward-compat with pre-0.39 daemons.
    repo_slug: str = ''
    cawl_image_repo: str = ''

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
            commits_ahead=int(d.get('commits_ahead', 0)),
            repo_slug=d.get('repo_slug', '') or '',
            cawl_image_repo=d.get('cawl_image_repo', '') or '',
        )
