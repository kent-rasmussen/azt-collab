"""
Minimal sister-app demo for the A-Z+T suite.

What this shows: how a second app on the same device — separate from
``azt_recorder`` — wires itself into the shared collaboration backend.

Flow
----
1.  Identify ourselves (so logs / device_flow / commit identity are
    sensible). The defaults match azt-recorder; override here for
    illustration.
2.  Confirm the daemon is reachable (auto-spawn handles "not running
    yet"; this just prints a hello).
3.  List the projects already known to the daemon.
4.  Register a project working tree we already have on disk.
5.  Stage a small change to the project's working_dir from this app
    (a tiny LIFT edit), then ask the daemon to sync.
6.  Poll for the resulting job and print the structured Result.

Run it like this (with the recorder's venv on PATH or activated)::

    python examples/sister_app.py /path/to/some_lift_project

The argument is a directory containing a ``.lift`` file. If the dir
isn't a git repo yet, the script `git init`s it before registering;
that lets the example work standalone for first-time experimentation.

The example does not write credentials — those come from the recorder's
``Connect to GitHub`` / GitLab PAT flow. Without credentials configured,
the sync result will contain ``AUTH_REQUIRED`` (translated as
"Not connected to GitHub. Go to Setup > Connect to GitHub.").
"""

import os
import sys
import time

# Run cold from anywhere: prepend the project root so azt_collabd /
# azt_collab_client are importable even when this file is invoked
# directly (Python's default sys.path[0] is examples/, not the repo).
_PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..'))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import azt_collabd
from azt_collab_client import (
    Result, S,
    configure as client_configure,
    get_credentials_status,
    is_online,
    list_projects,
    open_project,
    poll_job,
    project_status,
    register_project,
    request_sync,
    translate_result,
)


def _ensure_git_repo(working_dir):
    if os.path.isdir(os.path.join(working_dir, '.git')):
        return
    from dulwich import porcelain
    porcelain.init(working_dir)


def _find_lift(working_dir):
    for name in os.listdir(working_dir):
        if name.endswith('.lift'):
            return os.path.join(working_dir, name)
    return None


def _ensure_lift(working_dir, langcode):
    lift = _find_lift(working_dir)
    if lift:
        return lift
    lift = os.path.join(working_dir, f'{langcode}.lift')
    with open(lift, 'wb') as f:
        f.write(
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<lift version="0.13"></lift>'
        )
    return lift


def _touch_lift(lift_path):
    """Append a benign annotation so there's always something to commit."""
    import datetime
    marker = (f'<!-- sister_app touched at '
              f'{datetime.datetime.now().isoformat(timespec="seconds")} -->\n')
    with open(lift_path, 'rb') as f:
        body = f.read()
    closing = b'</lift>'
    if closing in body:
        body = body.replace(closing, marker.encode() + closing)
    else:
        body += marker.encode()
    with open(lift_path, 'wb') as f:
        f.write(body)


def main(working_dir):
    working_dir = os.path.abspath(working_dir)
    if not os.path.isdir(working_dir):
        sys.exit(f'not a directory: {working_dir}')

    # 1. Identify this app to the backend. Defaults match the recorder;
    #    override for a real sister app.
    azt_collabd.configure(app_slug='azt-sister-app-demo')
    client_configure(app_id='azt-sister-app-demo')

    # 2. Reachability — auto-spawn covers the "no daemon yet" case.
    print(f'online via daemon: {is_online()}')
    creds = get_credentials_status()
    print(f'credentials: host={creds.get("host")} '
          f'github_connected={creds.get("github", {}).get("connected")}')

    # 3. What's the daemon already tracking?
    existing = list_projects()
    print(f'projects already registered: '
          f'{[p.langcode for p in existing] or "(none)"}')

    # 4. Register the working tree this app cares about.
    _ensure_git_repo(working_dir)
    langcode = os.path.basename(os.path.normpath(working_dir)) or 'demo'
    lift_path = _ensure_lift(working_dir, langcode)
    proj = register_project(langcode, working_dir, lift_path)
    print(f'registered: langcode={proj.langcode} working_dir={proj.working_dir}')

    # Sanity: open it back
    again = open_project(langcode)
    assert again is not None and again.langcode == langcode

    status = project_status(langcode)
    print(f'project status: branch={status.branch} '
          f'remote={status.remote_url or "(none)"} '
          f'changes={status.n_changes}')

    # 5. Make a tiny edit, then request a sync.
    # As of azt_collab_client 0.40.0, ``request_sync`` no longer
    # takes a ``contributor`` argument — the daemon resolves the
    # commit-author name from its store. Set it once via
    # ``set_contributor`` (typically through the daemon settings
    # UI) before running this demo, or the sync will refuse with
    # ``S.CONTRIBUTOR_UNSET`` (visible via ``poll_job(...)['result']``).
    _touch_lift(lift_path)
    job_id = request_sync(langcode)
    print(f'job_id: {job_id}')

    # 6. Poll for completion.
    deadline = time.time() + 10.0
    info = None
    while time.time() < deadline:
        info = poll_job(job_id)
        if info and info['state'] == 'DONE':
            break
        time.sleep(0.2)
    if info is None:
        sys.exit('job lookup failed')
    result: Result = info['result']
    print(f'state: {info["state"]}')
    print(f'codes: {result.codes() if result else "(none)"}')
    if result is not None:
        print('translated:')
        print(translate_result(result))
    if result is not None and result.has(S.AUTH_REQUIRED):
        print('\n(no credentials configured — connect from the recorder '
              'or `python -m azt_collabd ui`)')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit(f'usage: {sys.argv[0]} <project-dir>')
    main(sys.argv[1])
