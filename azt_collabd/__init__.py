"""
azt_collabd — the A-Z+T suite collaboration server (library form).

Step 1: functions have been moved from collab.py into submodules here.
collab.py remains as a shim re-exporting everything so existing callers in
main.py keep working unchanged.

Submodules:
    net     — SSL patching, connectivity check
    auth    — GitHub device flow, token refresh, app install checks, GitLab
    store   — token persistence (reads/writes a prefs-like json file)
    repo    — dulwich operations: init, clone, pull, push, commit, sync
    paths   — $AZT_HOME resolution, server.json path
    server  — loopback HTTP/JSON front-end (run via `python -m azt_collabd`)

The backend has no Kivy dependency. UI-thread marshaling is the caller's
responsibility.
"""

__version__ = "0.20.5"

# Floor on the azt_collab_client version this daemon is willing to talk
# to. Published on /v1/health so the client compares locally and a peer
# bundling an older client surfaces ``client_too_old`` from
# ``check_server_compat()``. Bump in lockstep with wire-format / data-flow
# changes older clients can't survive — for example the 0.16.0 cut moved
# the picker to emit ``content://`` URIs, which a pre-LiftHandle client
# would try to ``open()`` as a filesystem path (the recorder's
# ``[Errno 2] No such file or directory`` symptom).
#
# 0.20.0 floor: poll_job() now returns Result(JOB_INTERRUPTED, ...)
# when the daemon was killed mid-job and respawned (sticky-bound
# service, scheduler.reconcile_on_startup). Pre-0.20 clients don't
# have that status code or its translation; they'd surface the raw
# uppercase code in their UI.
#
# 0.23.0 floor: azt_collab_client.recent moved from file-based
# ($AZT_HOME/config.json::recent.last_langcode) to RPC against
# /v1/recent/last_project. Pre-0.23 clients keep reading their *own*
# package's filesDir, which on Android lives in a different sandbox
# from the daemon's; the daemon's last_project stamping is invisible
# to them and the recorder's auto-resume falls through to the picker
# every restart. Bumping the floor surfaces ``client_too_old`` from
# ``check_server_compat()`` so the user is prompted to update the
# peer APK, instead of debugging the silent loop. Saved-memory note
# in feedback_min_client_version.md applies.
MIN_CLIENT_VERSION = "0.23.0"

from . import config
from . import net
from . import auth
from . import store
from . import repo
from . import projects
from . import status
from .config import configure
from .status import Status, Result, AuthError

# Convenience re-exports (match the surface of the old collab.py module)
from .net import _find_ca_bundle, _patch_dulwich_ssl, _ensure_gitconfig, \
    _ensure_ssl, _has_internet
from .auth import (
    device_flow_start, device_flow_poll, refresh_access_token,
    get_github_username, check_app_installed, check_repo_in_installation,
    app_install_url, add_collaborator, diagnose_403, _diagnose_403,
)
from .store import save_tokens, get_valid_token
from .repo import (
    repo_status_summary, init_repo, clone_repo, pull_repo,
    commit_and_push_branch, sync_repo, commit_audio_and_sync,
)


# GitHub App identity values are exposed dynamically so they reflect
# configure() calls that may happen after this package is imported.
def __getattr__(name):
    if name in ('GITHUB_APP_CLIENT_ID', 'GITHUB_APP_NAME',
                'GITHUB_COLLABORATOR', 'GITHUB_APP_INSTALL_URL'):
        return getattr(auth, name)
    raise AttributeError(
        f'module {__name__!r} has no attribute {name!r}')
