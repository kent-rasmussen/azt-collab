"""Desktop self-update: fast-forward the app's own source checkout.

On Android the app updates by sideloading a new APK
(``azt_collab_client/ui/update.py``). On desktop the suite ships as a
**git checkout**, so "update this app" means fast-forwarding that
checkout from its origin.

Git lives here in the daemon package, never in the platform-agnostic
client (client hard-rule #1: no git ops in ``azt_collab_client``). This
updates the daemon's OWN source clone (the ``azt-collab`` checkout this
module lives in); the desktop AZT application is a separate checkout
with its own update path.

FF-only on purpose: a clean field clone fast-forwards silently; a clone
with local edits (a developer's) or a diverged history is reported, not
force-merged — we never mangle someone's working tree behind an
"update" button.

Returns a ``(code, detail)`` tuple so the caller owns translation:
  UPDATED         — fast-forwarded; restart to load the new code.
  UP_TO_DATE      — already at origin's tip.
  NOT_A_CHECKOUT  — target dir has no .git (detail = the dir).
  NO_GIT          — git binary not found on PATH.
  TIMEOUT         — the pull exceeded the timeout.
  FAILED          — git returned non-zero (detail = its output, most
                    commonly "not possible to fast-forward" = local
                    edits / diverged; the user resolves it by hand).
"""
import os
import subprocess

_PULL_TIMEOUT_S = 120


def repo_root():
    """The source checkout root — ``<root>/azt_collabd/self_update.py``
    → ``<root>``."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(here)


def git_pull_self(repo_dir=None):
    """Fast-forward the app's source checkout from its origin. See the
    module docstring for the ``(code, detail)`` contract."""
    root = repo_dir or repo_root()
    if not os.path.isdir(os.path.join(root, '.git')):
        return ('NOT_A_CHECKOUT', root)
    try:
        proc = subprocess.run(
            ['git', '-C', root, 'pull', '--ff-only'],
            capture_output=True, text=True, timeout=_PULL_TIMEOUT_S)
    except FileNotFoundError:
        return ('NO_GIT', '')
    except subprocess.TimeoutExpired:
        return ('TIMEOUT', '')
    out = ((proc.stdout or '') + '\n' + (proc.stderr or '')).strip()
    if proc.returncode == 0:
        low = out.lower()
        if 'up to date' in low or 'up-to-date' in low:
            return ('UP_TO_DATE', '')
        return ('UPDATED', out)
    return ('FAILED', out or 'git pull failed')
