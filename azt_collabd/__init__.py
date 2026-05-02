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

__version__ = "0.10.3"

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
