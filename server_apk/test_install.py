#!/usr/bin/env python3
"""
test_install.py — desktop integration smoke-test for the kill-recovery
flow added in azt_collabd 0.16.0 / azt_collab_client 0.20.0.

Sibling of test_install.sh (which is the adb-driven on-device test for
the installed APK). This script exercises the *primitive* the Android
sticky-bound service relies on: scheduler.jobs.json persistence +
scheduler.reconcile_on_startup + the JOB_INTERRUPTED status code, plus
the loopback transport's auto-spawn after the daemon is SIGKILL'd.

A passing desktop test is a strong indicator the same primitives work
under the Android service host. Comprehensive on-device coverage of
the sticky-bound service / ContentProvider / URI-grant survival path
still goes through test_install.sh against a freshly-installed APK.

Sections, in order of "would have caught a real regression":
  1. JOB_INTERRUPTED defined on both server and client status modules
  2. JOB_INTERRUPTED has a translation handler in translate.py
  3. azt_collabd MIN_CLIENT_VERSION + azt_collab_client MIN_SERVER_VERSION
     are the documented floors (catches a missed bump)
  4. Auto-spawn fires the daemon on first client call
  5. request_sync persists the job to $AZT_HOME/jobs.json with state
     PENDING (the persistence layer's contract — without this, recovery
     is impossible because there's nothing for reconcile to find)
  6. SIGKILL'd daemon is detected by the client transport, which
     respawns a fresh daemon
  7. reconcile_on_startup marks the stale PENDING job as DONE +
     JOB_INTERRUPTED (the recovery pass)
  8. poll_job returns Result.has(JOB_INTERRUPTED) to the peer

Run from the project root:

    python server_apk/test_install.py
    # or with a custom workdir:
    python server_apk/test_install.py /tmp/aztest_install_demo

Exit 0 if all pass, 1 if any fail. The script sets its own AZT_HOME
under /tmp by default so the user's real $AZT_HOME isn't touched.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Force a long debounce BEFORE importing azt_collabd so newly-spawned
# daemons inherit it via the env-copying spawn helper. With a 60-second
# debounce, request_sync sits in PENDING long enough for us to kill the
# daemon between the call and the timer firing.
os.environ['AZT_SYNC_DEBOUNCE_MS'] = '60000'


# ── Pass/fail accounting ────────────────────────────────────────────────────

_passes = 0
_fails = 0


def section(title):
    print()
    print(f'── {title} ──')


def _passed(msg):
    global _passes
    _passes += 1
    print(f'  PASS: {msg}')


def _failed(msg):
    global _fails
    _fails += 1
    print(f'  FAIL: {msg}')


def expect(cond, ok_msg, fail_msg):
    if cond:
        _passed(ok_msg)
    else:
        _failed(fail_msg)
    return cond


# ── Test sections ───────────────────────────────────────────────────────────

def section_1_status_codes_defined():
    section('1. JOB_INTERRUPTED defined on both server and client status modules')
    from azt_collabd import status as srv_status
    from azt_collab_client import status as cli_status
    expect(getattr(srv_status, 'JOB_INTERRUPTED', None) == 'JOB_INTERRUPTED',
           'azt_collabd.status.JOB_INTERRUPTED == "JOB_INTERRUPTED"',
           'azt_collabd.status missing JOB_INTERRUPTED')
    expect(getattr(cli_status, 'JOB_INTERRUPTED', None) == 'JOB_INTERRUPTED',
           'azt_collab_client.status.JOB_INTERRUPTED == "JOB_INTERRUPTED"',
           'azt_collab_client.status missing JOB_INTERRUPTED — '
           'mirror got out of sync')


def section_2_translation_handler():
    section('2. JOB_INTERRUPTED has a translation handler')
    from azt_collab_client import status as S, translate
    handler = translate._HANDLERS.get(S.JOB_INTERRUPTED)
    if not expect(handler is not None,
                  'translate._HANDLERS has JOB_INTERRUPTED entry',
                  'no translate._HANDLERS entry for JOB_INTERRUPTED — '
                  'peers will see "[JOB_INTERRUPTED] {}" in their UI'):
        return
    rendered = handler({})
    expect(isinstance(rendered, str) and 'interrupted' in rendered.lower(),
           f'handler renders English: {rendered!r}',
           f'handler returned unexpected value: {rendered!r}')


def _vt(s):
    """Best-effort 'X.Y.Z' → (X, Y, Z) tuple. Mirrors the client's
    _version_tuple so the test compares the same way the client's
    handshake does."""
    out = []
    for chunk in str(s or '').split('.'):
        digits = ''
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def section_3_version_floors():
    section('3. Versions and floors meet the JOB_INTERRUPTED release line')
    from azt_collabd import __version__ as srv_v, MIN_CLIENT_VERSION
    from azt_collab_client import __version__ as cli_v, MIN_SERVER_VERSION
    # Use >= comparisons rather than equality so a patch bump
    # (0.20.0 → 0.20.1 etc.) doesn't fail the test — the contract is
    # "at least the version that introduced JOB_INTERRUPTED."
    expect(_vt(srv_v) >= _vt('0.16.0'),
           f'azt_collabd.__version__ ({srv_v}) >= 0.16.0',
           f'azt_collabd.__version__ ({srv_v}) is below 0.16.0')
    expect(_vt(cli_v) >= _vt('0.20.0'),
           f'azt_collab_client.__version__ ({cli_v}) >= 0.20.0',
           f'azt_collab_client.__version__ ({cli_v}) is below 0.20.0')
    expect(_vt(MIN_CLIENT_VERSION) >= _vt('0.20.0'),
           f'azt_collabd.MIN_CLIENT_VERSION ({MIN_CLIENT_VERSION}) >= 0.20.0',
           f'MIN_CLIENT_VERSION ({MIN_CLIENT_VERSION}) is below 0.20.0')
    expect(_vt(MIN_SERVER_VERSION) >= _vt('0.16.0'),
           f'azt_collab_client.MIN_SERVER_VERSION '
           f'({MIN_SERVER_VERSION}) >= 0.16.0',
           f'MIN_SERVER_VERSION ({MIN_SERVER_VERSION}) is below 0.16.0')


def _ensure_git_repo(working_dir):
    if os.path.isdir(os.path.join(working_dir, '.git')):
        return
    from dulwich import porcelain
    porcelain.init(working_dir)


def _ensure_lift(working_dir, langcode):
    for name in os.listdir(working_dir):
        if name.endswith('.lift'):
            return os.path.join(working_dir, name)
    lift = os.path.join(working_dir, f'{langcode}.lift')
    with open(lift, 'wb') as f:
        f.write(b'<?xml version="1.0" encoding="utf-8"?>'
                b'<lift version="0.13"></lift>')
    return lift


def _read_daemon_pid():
    from azt_collabd.paths import server_info_path
    with open(server_info_path()) as f:
        info = json.load(f)
    return int(info.get('pid', 0))


def _wait_for_dead(pid, timeout=5.0):
    """A process is 'dead' for our purposes if it's gone OR a zombie.

    The loopback auto-spawn launches the daemon via
    ``subprocess.Popen(start_new_session=True)``. ``start_new_session``
    calls ``setsid`` (new session/process group) but does NOT change
    parentage, so the daemon is still a child of this test process.
    When we SIGKILL it, the kernel transitions it to zombie state and
    waits for the parent (us) to ``waitpid`` it. ``os.kill(pid, 0)``
    returns success for zombies — they still have a PID — so checking
    PID existence alone reports the dead daemon as still alive.

    Inspect ``/proc/<pid>/status`` to distinguish: ``Z`` (zombie) or
    ``X`` (dying) are dead-for-our-purposes; anything else (R/S/D/T)
    means it really is still running. Also call ``waitpid`` to reap
    cleanly so the test process doesn't leak zombies."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(f'/proc/{pid}/status') as f:
                state_char = ''
                for line in f:
                    if line.startswith('State:'):
                        state_char = line.split(':', 1)[1].strip()[:1]
                        break
            if state_char in ('Z', 'X'):
                try:
                    os.waitpid(pid, os.WNOHANG)
                except OSError:
                    pass  # not our child, or already reaped
                return True
        except FileNotFoundError:
            # /proc entry gone — process is fully dead and reaped.
            return True
        time.sleep(0.05)
    return False


def section_4_to_8_kill_recovery(working_dir):
    """Sections 4-8 share state (a single auto-spawned daemon, a
    registered project, a job_id) so they're driven by one function."""
    from azt_collab_client import (
        S, configure as client_configure,
        is_online, poll_job, register_project, request_sync,
    )
    import azt_collabd
    from azt_collabd.paths import azt_home

    azt_collabd.configure(app_slug='azt-test-install-demo')
    client_configure(app_id='azt-test-install-demo')

    section('4. Auto-spawn fires the daemon on first client call')
    online = is_online()
    expect(isinstance(online, bool),
           f'is_online() returned a bool ({online!r}); daemon auto-spawned',
           f'is_online() did not auto-spawn the daemon: {online!r}')
    pid = _read_daemon_pid()
    expect(pid > 0,
           f'daemon pid={pid} read from server.json',
           'no pid in server.json — auto-spawn did not write it')

    section('5. request_sync persists job to jobs.json with state PENDING')
    _ensure_git_repo(working_dir)
    langcode = os.path.basename(working_dir) or 'test_install_demo'
    lift_path = _ensure_lift(working_dir, langcode)
    proj = register_project(langcode, working_dir, lift_path)
    if not expect(proj is not None,
                  f'project registered: langcode={langcode}',
                  'register_project returned None'):
        return
    job_id = request_sync(langcode, contributor='Test Install Demo')
    if not expect(bool(job_id),
                  f'request_sync returned job_id={job_id}',
                  'request_sync returned no job_id'):
        return
    jobs_path = os.path.join(azt_home(), 'jobs.json')
    if not expect(os.path.exists(jobs_path),
                  f'{jobs_path} exists post-request_sync',
                  f'{jobs_path} not created — _persist_locked never fired'):
        return
    with open(jobs_path) as f:
        persisted = json.load(f)
    matching = [j for j in persisted.get('jobs', [])
                if j.get('job_id') == job_id]
    if not expect(bool(matching),
                  f'jobs.json contains job_id={job_id}',
                  f'jobs.json does NOT contain job_id={job_id}: '
                  f'{persisted}'):
        return
    state = matching[0].get('state')
    expect(state in ('PENDING', 'RUNNING'),
           f'job state is {state} (PENDING or RUNNING expected)',
           f'job state is {state} — debounce too short, the worker '
           f'finished before we could kill it')

    section('6. SIGKILL daemon, client transport detects + respawns')
    print(f'  killing daemon pid={pid}')
    os.kill(pid, signal.SIGKILL)
    if not expect(_wait_for_dead(pid),
                  f'daemon pid={pid} died within timeout',
                  f'daemon pid={pid} still alive after SIGKILL — wedged'):
        return
    # The act of polling triggers the loopback transport to detect the
    # dead PID, remove server.json, and respawn. Sections 7 and 8 read
    # the result of this respawn.
    print('  polling job_id post-kill (auto-spawn should fire)...')
    info = poll_job(job_id)
    if not expect(info is not None,
                  'poll_job returned a result (auto-spawn recovered)',
                  'poll_job returned None — auto-spawn did NOT recover'):
        return
    new_pid = _read_daemon_pid()
    expect(new_pid != pid and new_pid > 0,
           f'fresh daemon pid={new_pid} (was {pid})',
           f'pid did not change post-kill (still {new_pid}) — '
           f'auto-spawn did not run')

    section('7. reconcile_on_startup flipped PENDING → DONE+JOB_INTERRUPTED')
    expect(info['state'] == 'DONE',
           f'state == DONE (was {state} pre-kill)',
           f'state is {info["state"]}, expected DONE — '
           f'reconcile_on_startup did not run on respawn')
    result = info['result']
    expect(result is not None,
           'poll_job returned a non-null Result',
           'poll_job returned None Result — reconcile produced nothing')

    section('8. Result carries JOB_INTERRUPTED status code')
    expect(result is not None and result.has(S.JOB_INTERRUPTED),
           'Result.has(JOB_INTERRUPTED) is True',
           f'Result.codes={result.codes() if result else "(none)"} — '
           f'expected JOB_INTERRUPTED')
    # Translation sanity: the user-visible string should not be the
    # raw code (catches a missing translate handler that section 2
    # validates more directly).
    if result and result.has(S.JOB_INTERRUPTED):
        from azt_collab_client import translate_result
        rendered = translate_result(result)
        expect(rendered and 'JOB_INTERRUPTED' not in rendered,
               f'rendered string is human-readable: {rendered!r}',
               f'rendered string still has raw code: {rendered!r}')


# ── Cleanup ─────────────────────────────────────────────────────────────────

def _kill_lingering_daemons():
    """Best-effort SIGTERM to any azt_collabd processes we (or a prior
    aborted run) left behind. We use pgrep+kill rather than pkill so a
    foreign process whose argv happens to contain 'azt_collabd' isn't
    shot."""
    try:
        out = subprocess.check_output(
            ['pgrep', '-f', 'python.*-m azt_collabd'],
            stderr=subprocess.DEVNULL).decode().strip()
    except subprocess.CalledProcessError:
        return  # none found
    for raw in out.split():
        try:
            pid = int(raw)
        except ValueError:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _reap_children():
    """Reap any zombie children left behind by the auto-spawn path.
    See _wait_for_dead for why these accumulate. Best-effort — ignore
    ECHILD (no children) and any other races."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        except OSError:
            return
        if pid == 0:
            return  # no more zombies waiting


def main(argv):
    workdir = argv[1] if len(argv) > 1 else None

    # Default: throwaway $AZT_HOME under /tmp so we don't disturb the
    # user's real one. Let the user override via $AZT_HOME if they want
    # to inspect state after the run.
    if not os.environ.get('AZT_HOME'):
        os.environ['AZT_HOME'] = tempfile.mkdtemp(prefix='aztest_install_')
    home_dir = os.environ['AZT_HOME']
    if os.path.isdir(home_dir):
        # Wipe any leftovers from a previous aborted run so reconcile
        # doesn't see ghost jobs from before.
        shutil.rmtree(home_dir)
    os.makedirs(home_dir, exist_ok=True)

    if workdir is None:
        workdir = tempfile.mkdtemp(prefix='aztest_install_demo_')
    else:
        workdir = os.path.abspath(workdir)
        if os.path.isdir(workdir):
            shutil.rmtree(workdir)
        os.makedirs(workdir, exist_ok=True)

    print(f'AZT_HOME={home_dir}')
    print(f'workdir={workdir}')
    print(f'AZT_SYNC_DEBOUNCE_MS={os.environ.get("AZT_SYNC_DEBOUNCE_MS")}')

    _kill_lingering_daemons()
    time.sleep(0.3)

    try:
        section_1_status_codes_defined()
        section_2_translation_handler()
        section_3_version_floors()
        section_4_to_8_kill_recovery(workdir)
    finally:
        _kill_lingering_daemons()
        time.sleep(0.2)
        _reap_children()
        shutil.rmtree(home_dir, ignore_errors=True)
        shutil.rmtree(workdir, ignore_errors=True)

    print()
    print('═' * 40)
    print(f'  {_passes} passed, {_fails} failed')
    print('═' * 40)
    return 0 if _fails == 0 else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
