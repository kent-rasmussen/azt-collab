"""
Minimal sister-app demo for the A-Z+T suite — read-only daemon
survey.

What this shows: everything an app on the same device sees from
``azt_collabd`` through ``azt_collab_client``. No writes. No
``register_project``. The daemon's picker and settings UI are
the canonical places for those — bound to the ``p`` and ``s``
keys here so you can launch them from the same session.

Run::

    python examples/sister_app.py

Then:
    p ⏎   — open the project picker (daemon UI)
    s ⏎   — open the daemon settings UI
    r ⏎   — refresh the survey
    q ⏎   — quit

If no project has been registered yet, the survey shows the
daemon's empty state; pressing ``p`` walks you through the
picker / publish flow exactly the way a real peer would.
"""

import os
import sys

# Run cold from anywhere: prepend the project root so
# azt_collab_client is importable when invoked directly.
_PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..'))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import azt_collabd
from azt_collab_client import (
    configure as client_configure,
    check_server_compat,
    get_contributor,
    get_credentials_status,
    get_device_name,
    get_work_offline,
    is_online,
    last_project,
    list_projects,
    open_project,
    open_server_ui,
    pick_project,
    project_status,
    MIN_SERVER_VERSION,
    __version__ as _CLIENT_VERSION,
)


def _h1(text):
    print()
    print(text)
    print('─' * max(40, len(text)))


def _kv(label, value, indent=2):
    print(f'{" " * indent}{label:<22} {value}')


def _yes_no(b):
    return 'yes' if b else 'no'


def _print_reachability():
    _h1('daemon reachability')
    compat = check_server_compat()
    _kv('online (cached)', _yes_no(is_online()))
    _kv('client version', _CLIENT_VERSION)
    _kv('min server version', MIN_SERVER_VERSION)
    _kv('server reachable', _yes_no(compat.get('ok')))
    if compat.get('ok'):
        _kv('server version', compat.get('server_version', '(unknown)'))
    else:
        _kv('compat error', compat.get('error', '(none)'))


def _print_identity_and_creds():
    _h1('identity + credentials')
    contributor = get_contributor() or '(unset)'
    device = get_device_name() or '(unset)'
    creds = get_credentials_status() or {}
    github = creds.get('github') or {}
    gitlab = creds.get('gitlab') or {}
    _kv('contributor', contributor)
    _kv('device_name', device)
    _kv('credentials host', creds.get('host') or '(none)')
    _kv('github connected', _yes_no(github.get('connected')))
    _kv('github confirmed', _yes_no(github.get('confirmed')))
    _kv('github refresh_broken', _yes_no(github.get('refresh_broken')))
    _kv('gitlab confirmed', _yes_no(gitlab.get('confirmed')))


def _print_sync_policy():
    _h1('sync policy (daemon-wide)')
    _kv('work_offline', _yes_no(get_work_offline()))


def _print_projects_summary():
    _h1('registered projects')
    projects = list_projects() or []
    if not projects:
        _kv('count', 0)
        print('  (none registered — press p to pick / publish one)')
        return
    _kv('count', len(projects))
    for p in projects:
        _kv(p.langcode,
            f'{p.working_dir!r}  remote={p.remote_url or "(none)"}',
            indent=4)


def _print_current_project():
    _h1('current project (last_project)')
    langcode = last_project()
    if not langcode:
        _kv('langcode', '(none)')
        print('  (no project loaded yet — press p to pick one)')
        return
    _kv('langcode', langcode)
    proj = open_project(langcode)
    if proj is None:
        print('  (daemon does not recognise this langcode — '
              'projects.json drift)')
        return
    _kv('working_dir', proj.working_dir)
    _kv('lift_path', proj.lift_path)
    _kv('lift_exists', _yes_no(proj.lift_exists))
    _kv('remote_url', proj.remote_url or '(none)')
    _kv('repo_slug', proj.repo_slug or '(default = langcode)')
    _kv('cawl_image_repo', proj.cawl_image_repo or '(daemon default)')
    _kv('last_commit', proj.last_commit or '(never)')
    _kv('last_sync', proj.last_sync or '(never)')
    status = project_status(langcode)
    if status is None:
        print('  (project_status returned None — daemon offline?)')
        return
    print()
    _kv('— project_status —', '')
    _kv('branch', status.branch or '(none)')
    _kv('n_changes', status.n_changes)
    _kv('commits_ahead', status.commits_ahead)
    _kv('work_offline', _yes_no(status.work_offline))
    _kv('commit_failure_count', status.commit_failure_count)
    if status.commit_failure_count:
        _kv('last_commit_failure_at', status.last_commit_failure_at)
        _kv('last_commit_error', status.last_commit_error)
    if status.n_recovered_today:
        _kv('n_recovered_today', status.n_recovered_today)


def _print_survey():
    _print_reachability()
    _print_identity_and_creds()
    _print_sync_policy()
    _print_projects_summary()
    _print_current_project()
    print()


def _run_picker():
    print('opening picker… (close the window when done)')
    result = pick_project()
    if not result.get('ok'):
        print(f'picker: {result.get("error", "(no detail)")}')
        return
    print(f'picker selected: {result.get("path")}')
    print('(the daemon updates last_project on its own; refreshing…)')


def _run_settings():
    print('opening daemon settings UI… (close the window when done)')
    result = open_server_ui(on_status=lambda s: print(f'  {s}'))
    if not result.get('ok'):
        print(f'settings: {result.get("error", "(no detail)")}')
        if result.get('detail'):
            print(f'  detail: {result["detail"]}')


def main():
    # 1. Identify ourselves to the backend. Defaults match the
    #    recorder; override here for illustration.
    azt_collabd.configure(app_slug='azt-sister-app-demo')
    client_configure(app_id='azt-sister-app-demo')

    print('A-Z+T sister-app survey — what the daemon reports.')
    _print_survey()

    while True:
        try:
            choice = input('[p]icker  [s]ettings  [r]efresh  [q]uit > ')
        except (EOFError, KeyboardInterrupt):
            print()
            return
        choice = choice.strip().lower()
        if choice in ('q', 'quit', 'exit'):
            return
        if choice in ('p', 'pick', 'picker'):
            _run_picker()
            _print_survey()
        elif choice in ('s', 'settings'):
            _run_settings()
            _print_survey()
        elif choice in ('', 'r', 'refresh'):
            _print_survey()
        else:
            print(f'unknown: {choice!r}')


if __name__ == '__main__':
    main()
