"""Phase-1 smoke test: WAN exponential backoff with persistence in
``azt_collabd.wan_backoff``.

The curve is load-bearing for the 0.50 design: a 24-hour cap is
meaningless without persistence, and a wrong curve either hammers
the radio (curve too shallow) or makes recovery feel broken
(curve too steep). These tests pin the math and the persistence
contract so a future refactor can't silently regress either.
"""

import json
import os
import time

import pytest

from azt_collabd import wan_backoff


def test_no_record_returns_due_immediately():
    """A project with no failure history is always due now."""
    assert wan_backoff.next_due_at('fr') == 0.0
    assert wan_backoff.is_due('fr')
    assert wan_backoff.consecutive_failures('fr') == 0


def test_record_failure_advances_curve():
    wan_backoff.record_failure('fr')
    assert wan_backoff.consecutive_failures('fr') == 1
    # First step is _BASE_S (30 s)
    delta = wan_backoff.next_due_at('fr') - time.time()
    assert 25 < delta < 35


def test_curve_doubles():
    wan_backoff.record_failure('fr')  # 30 s
    wan_backoff.record_failure('fr')  # 60 s
    wan_backoff.record_failure('fr')  # 120 s
    delta = wan_backoff.next_due_at('fr') - time.time()
    assert 115 < delta < 125


def test_curve_caps_at_24_hours():
    """After enough failures, the curve clamps at the 24 h ceiling.
    Field-relevant: phone offline for 14 days probes once a day,
    not 32× per hour."""
    for _ in range(30):
        wan_backoff.record_failure('fr')
    delta = wan_backoff.next_due_at('fr') - time.time()
    # 24 h with some slop for the time.time() between record and read
    assert 86_390 < delta < 86_410


def test_record_success_resets():
    """A successful push clears the curve entirely. Next failure
    starts from step 1 again."""
    for _ in range(5):
        wan_backoff.record_failure('fr')
    wan_backoff.record_success('fr')
    assert wan_backoff.consecutive_failures('fr') == 0
    assert wan_backoff.next_due_at('fr') == 0.0


def test_nudge_clears_due_time_only(monkeypatch):
    """User-nudge ``nudge()`` makes the next attempt fire
    immediately but preserves ``consecutive_failures`` — a fresh
    failure re-enters the curve at the same step rather than
    starting from 30 s."""
    for _ in range(5):
        wan_backoff.record_failure('fr')
    failures_before = wan_backoff.consecutive_failures('fr')
    wan_backoff.nudge('fr')
    assert wan_backoff.next_due_at('fr') == 0.0
    assert wan_backoff.consecutive_failures('fr') == failures_before
    # Next failure advances from the SAME count (not from 1).
    wan_backoff.record_failure('fr')
    assert wan_backoff.consecutive_failures('fr') == failures_before + 1


def test_state_persists_across_module_reload(azt_home):
    """The whole point of the persistence contract: a 24 h backoff
    must survive daemon respawn (Android OOM, APK reinstall).
    Verify by reading the file directly after a failure."""
    wan_backoff.record_failure('fr')
    wan_backoff.record_failure('fr')
    path = os.path.join(str(azt_home), 'wan_state.json')
    assert os.path.exists(path)
    with open(path) as f:
        on_disk = json.load(f)
    assert on_disk['fr']['consecutive_failures'] == 2
    assert on_disk['fr']['next_attempt_at'] > time.time() + 30


def test_reset_due_times_on_startup_is_a_noop():
    """Deprecated no-op since 0.50.45: daemon lifecycle is not user
    intent. Pre-0.50.45 this cleared every ``next_attempt_at`` on
    respawn, which — given how often Android respawns the daemon
    (OOM, APK self-update, sticky-service restart) — made the 24 h
    cap effectively unreachable. Pin the no-op: due times AND
    failure counts survive; only ``nudge`` (user tap) or
    ``record_success`` reset the curve."""
    for _ in range(4):
        wan_backoff.record_failure('fr')
    due_before = wan_backoff.next_due_at('fr')
    assert due_before > 0.0
    wan_backoff.reset_due_times_on_startup()
    assert wan_backoff.next_due_at('fr') == due_before
    assert wan_backoff.consecutive_failures('fr') == 4
    wan_backoff.record_failure('fr')
    assert wan_backoff.consecutive_failures('fr') == 5


def test_per_langcode_isolation():
    """Failures in one project don't affect another."""
    wan_backoff.record_failure('fr')
    wan_backoff.record_failure('fr')
    wan_backoff.record_failure('en')
    assert wan_backoff.consecutive_failures('fr') == 2
    assert wan_backoff.consecutive_failures('en') == 1
    wan_backoff.record_success('fr')
    assert wan_backoff.consecutive_failures('fr') == 0
    assert wan_backoff.consecutive_failures('en') == 1


def test_corrupt_state_file_handled_gracefully(azt_home):
    """A truncated wan_state.json (mid-write crash) reads as empty
    state. We don't clobber the file; behaviour degrades to "treat
    everything as due now," which is the worst case we want — one
    extra attempt rather than silent backoff-forever."""
    path = os.path.join(str(azt_home), 'wan_state.json')
    with open(path, 'w') as f:
        f.write('{"fr": {"consecutive_failures": 3, "next_atte')  # truncated
    # Read shouldn't raise; should return defaults
    assert wan_backoff.next_due_at('fr') == 0.0
    assert wan_backoff.consecutive_failures('fr') == 0
