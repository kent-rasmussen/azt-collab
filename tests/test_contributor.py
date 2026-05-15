"""Tests for the 0.40.0 contributor / device-name contract.

Pins:

- ``store.set_contributor`` / ``get_contributor`` round-trip.
- ``store.get_contributor`` returns ``''`` when unset (no more
  ``'Recorder'`` fallback).
- ``store.get_device_name`` auto-populates on first read; persists
  the autodetect so a second read is stable.
- ``store.set_device_name`` round-trip; empty string clears and
  triggers re-detect on next read.
- ``repo._default_author`` composes ``Name <safe@safe_device>``
  with both contributor and device name.
- ``repo._default_author`` lazy-looks-up device_name from store
  when called with ``device_name=None``.
- ``_h_init_project`` / ``_h_project_sync`` refuse with
  ``S.CONTRIBUTOR_UNSET`` when no contributor is stored.
- ``_h_project_sync_async`` enqueues unconditionally; scheduler's
  ``_run_commit`` refuses with ``S.CONTRIBUTOR_UNSET`` at exec time
  (defence-in-depth so a long debounce + name-clear race can't
  produce a meaningless commit).
- ``body['contributor']`` is **ignored** by the daemon endpoints —
  pre-migration clients that pass it have no effect on the
  stored value or the commit author.
- ``set_contributor`` empty string clears.
- Endpoints ``GET/POST /v1/config/device_name`` round-trip.
"""

import json
import os
import socket
import threading

import pytest

from azt_collabd import server as srv
from azt_collabd import status as S_d
from azt_collabd import store
from azt_collabd import scheduler as _scheduler
from azt_collabd.repo import _default_author, _safe_email_segment
from azt_collab_client import status as S_c


# ── Status mirror drift ─────────────────────────────────────────────────


def test_contributor_unset_code_mirrored():
    assert S_d.CONTRIBUTOR_UNSET == 'CONTRIBUTOR_UNSET'
    assert S_c.CONTRIBUTOR_UNSET == 'CONTRIBUTOR_UNSET'


# ── store.get_contributor / set_contributor ─────────────────────────────


def test_get_contributor_empty_when_unset():
    """No more 'Recorder' fallback. Unset means empty string,
    period — peers branch on truthiness, daemon refuses commits."""
    assert store.get_contributor() == ''


def test_set_get_contributor_round_trip():
    store.set_contributor('Alice Smith')
    assert store.get_contributor() == 'Alice Smith'


def test_set_contributor_strips_whitespace():
    store.set_contributor('  Alice  ')
    assert store.get_contributor() == 'Alice'


def test_set_contributor_empty_clears():
    store.set_contributor('Alice')
    store.set_contributor('')
    assert store.get_contributor() == ''


# ── store.get_device_name auto-populate ──────────────────────────────────


def test_get_device_name_auto_populates_on_first_read():
    """First read on an unset store probes the OS (no jnius on the
    test host → falls through to socket.gethostname()) and persists
    so a second read sees the same value without re-probing."""
    # Sanity: nothing stored to start.
    raw = (store._load_config_file().get('collab') or {}).get(
        'device_name', '')
    assert raw == ''
    name = store.get_device_name()
    assert name  # non-empty
    # Persisted so the second read returns the same string.
    raw_after = (store._load_config_file().get('collab') or {}).get(
        'device_name', '')
    assert raw_after == name
    assert store.get_device_name() == name


def test_get_device_name_falls_back_to_hostname_on_desktop():
    """In the test host (no Android), the autodetect chain ends at
    socket.gethostname(). The persisted value should match it."""
    name = store.get_device_name()
    assert name == socket.gethostname() or name == 'unknown-device'


def test_set_device_name_round_trip():
    store.set_device_name("Marie's Tablet")
    assert store.get_device_name() == "Marie's Tablet"


def test_set_device_name_empty_clears_and_redetects():
    """Setting empty triggers re-autodetect on next read so the
    user clearing the override goes back to the OS default."""
    store.set_device_name('custom-name')
    assert store.get_device_name() == 'custom-name'
    store.set_device_name('')
    # Cleared → next read autodetects, persists, returns non-empty.
    redetected = store.get_device_name()
    assert redetected
    assert redetected != 'custom-name'


# ── _safe_email_segment ─────────────────────────────────────────────────


def test_safe_email_segment_basic_cases():
    assert _safe_email_segment('Alice Smith') == 'alice_smith'
    assert _safe_email_segment('Marie Dubois') == 'marie_dubois'
    assert _safe_email_segment('SM-T580') == 'sm-t580'
    assert _safe_email_segment('Pixel 6') == 'pixel_6'


def test_safe_email_segment_strips_invalid_chars():
    assert _safe_email_segment('Alice!@#$%') == 'alice'
    assert _safe_email_segment("Marie's tablet") == 'maries_tablet'


def test_safe_email_segment_empty_input_is_unknown():
    assert _safe_email_segment('') == 'unknown'
    assert _safe_email_segment(None) == 'unknown'
    assert _safe_email_segment('   ') == 'unknown'


# ── _default_author composition ─────────────────────────────────────────


def test_default_author_composes_name_and_device():
    """Author = display name verbatim; email = safe-name@safe-device.
    The literal ``@device`` placeholder is gone."""
    out = _default_author('Alice Smith', 'Marie Tablet')
    assert out == b'Alice Smith <alice_smith@marie_tablet>'


def test_default_author_explicit_empty_device_yields_unknown():
    """device_name='' (explicit, not None) skips the store lookup —
    used by tests that want deterministic output without touching
    the store."""
    out = _default_author('Alice Smith', '')
    assert out == b'Alice Smith <alice_smith@unknown>'


def test_default_author_lazy_lookup_when_device_none():
    """device_name=None triggers store.get_device_name() — which
    autodetects + persists. Result email uses the autodetected
    device (host name on this test host)."""
    out = _default_author('Alice Smith', None)
    # The store lookup returns a non-empty value; the resulting
    # author bytes include it after sanitisation.
    assert out.startswith(b'Alice Smith <alice_smith@')
    assert not out.endswith(b'@unknown>')


# ── Endpoints refuse with CONTRIBUTOR_UNSET ─────────────────────────────


def test_init_project_refuses_when_contributor_unset(tmp_path):
    """No contributor → 200 OK with Result(CONTRIBUTOR_UNSET).
    Doesn't fall through to git, doesn't make a meaningless
    'Recorder' commit, doesn't error 500."""
    working_dir = str(tmp_path / 'project')
    os.makedirs(working_dir)
    status, resp = srv.dispatch('POST', '/v1/projects/init', {
        'working_dir': working_dir,
        'remote_url': 'https://github.com/alice/repo',
    })
    assert status == 200
    assert resp['ok'] is True
    statuses = resp['result']['statuses']
    assert any(s['code'] == 'CONTRIBUTOR_UNSET' for s in statuses)


def test_project_sync_refuses_when_contributor_unset(tmp_path):
    from azt_collabd import projects as projects_mod
    working_dir = str(tmp_path / 'sw')
    os.makedirs(working_dir)
    projects_mod.register('sw-x-test', working_dir,
                          remote_url='https://github.com/alice/repo')
    status, resp = srv.dispatch('POST', '/v1/projects/sw-x-test/sync', {})
    assert status == 200
    assert resp['ok'] is True
    statuses = resp['result']['statuses']
    assert any(s['code'] == 'CONTRIBUTOR_UNSET' for s in statuses)


def test_init_project_proceeds_when_contributor_set(tmp_path, monkeypatch):
    """Counterpart: with contributor stored, the endpoint passes
    the unset-check and proceeds to the credentials check.
    Stub _init_repo so we don't touch git; assert the call reaches
    a state past the CONTRIBUTOR_UNSET branch (in this case,
    AUTH_REQUIRED, since no credentials are configured)."""
    store.set_contributor('Alice')
    working_dir = str(tmp_path / 'project')
    os.makedirs(working_dir)
    status, resp = srv.dispatch('POST', '/v1/projects/init', {
        'working_dir': working_dir,
        'remote_url': 'https://github.com/alice/repo',
    })
    assert status == 200
    statuses = resp['result']['statuses']
    codes = [s['code'] for s in statuses]
    assert 'CONTRIBUTOR_UNSET' not in codes
    # Reached the credentials check, which fails for an unconfigured
    # daemon: AUTH_REQUIRED.
    assert 'AUTH_REQUIRED' in codes


# ── body['contributor'] is ignored ──────────────────────────────────────


def test_body_contributor_is_ignored_when_store_set(tmp_path, monkeypatch):
    """A pre-0.40 peer that still passes ``contributor='Recorder'``
    in the body must NOT override the daemon's stored value. We
    can't directly inspect what _init_repo would commit (we don't
    run git), but we can verify the endpoint passes the unset-check
    (so it used the store, not the body) AND that the body field
    doesn't leak into store state."""
    store.set_contributor('Alice')
    assert store.get_contributor() == 'Alice'
    working_dir = str(tmp_path / 'project')
    os.makedirs(working_dir)
    # Pre-0.40 shape: peer sends contributor in body.
    status, resp = srv.dispatch('POST', '/v1/projects/init', {
        'working_dir': working_dir,
        'remote_url': 'https://github.com/alice/repo',
        'contributor': 'Recorder',   # ignored by daemon
    })
    assert status == 200
    statuses = resp['result']['statuses']
    codes = [s['code'] for s in statuses]
    # Used the store ('Alice'), not the body ('Recorder'), so the
    # endpoint did NOT refuse.
    assert 'CONTRIBUTOR_UNSET' not in codes
    # Body didn't bleed into the store.
    assert store.get_contributor() == 'Alice'


def test_body_contributor_does_not_bypass_unset_refusal(tmp_path):
    """Inverse: even if a peer passes a non-empty contributor in
    the body, the daemon refuses if its store is unset. The
    peer-passed value has no power over the contract."""
    assert store.get_contributor() == ''
    working_dir = str(tmp_path / 'project')
    os.makedirs(working_dir)
    status, resp = srv.dispatch('POST', '/v1/projects/init', {
        'working_dir': working_dir,
        'remote_url': 'https://github.com/alice/repo',
        'contributor': 'Bob',
    })
    statuses = resp['result']['statuses']
    assert any(s['code'] == 'CONTRIBUTOR_UNSET' for s in statuses)
    # No store contamination.
    assert store.get_contributor() == ''


# ── Scheduler exec-time defence-in-depth ────────────────────────────────


def test_scheduler_run_commit_refuses_when_contributor_unset():
    """If a job manages to slip past the upfront check (it
    doesn't, today, since the endpoint enqueues unconditionally —
    but a long debounce + a clear-name race could land us here),
    the scheduler refuses at exec time. Since 0.43.0 the debounced
    worker is ``_run_commit`` (commit-only); ``_run_sync`` is gone."""
    # No contributor stored.
    assert store.get_contributor() == ''
    result = _scheduler._run_commit('sw-x-mystery')
    assert result.has(S_d.CONTRIBUTOR_UNSET)


# ── Device-name endpoints ───────────────────────────────────────────────


def test_endpoint_get_device_name_autopopulates():
    status, resp = srv.dispatch('GET', '/v1/config/device_name', None)
    assert status == 200
    assert resp['ok'] is True
    assert resp['device_name']  # non-empty


def test_endpoint_set_device_name_round_trip():
    status, resp = srv.dispatch('POST', '/v1/config/device_name',
                                {'device_name': "Alice's phone"})
    assert status == 200
    assert resp['ok'] is True
    assert resp['device_name'] == "Alice's phone"
    # Subsequent GET returns the persisted value.
    status, resp = srv.dispatch('GET', '/v1/config/device_name', None)
    assert resp['device_name'] == "Alice's phone"


def test_endpoint_set_device_name_strips_whitespace():
    srv.dispatch('POST', '/v1/config/device_name',
                 {'device_name': '  hostX  '})
    assert store.get_device_name() == 'hostX'


def test_endpoint_set_device_name_empty_redetects():
    srv.dispatch('POST', '/v1/config/device_name',
                 {'device_name': 'override'})
    assert store.get_device_name() == 'override'
    srv.dispatch('POST', '/v1/config/device_name',
                 {'device_name': ''})
    # Cleared → re-detected on next read.
    redetected = store.get_device_name()
    assert redetected
    assert redetected != 'override'


# ── resolve_contributor is gone ─────────────────────────────────────────


def test_resolve_contributor_removed():
    """Pre-0.40 had ``store.resolve_contributor`` that fell back to
    the 'Recorder' literal. It's gone in 0.40 — any in-tree caller
    that still imports it will fail at import time, which is the
    fail-loud behaviour we want."""
    assert not hasattr(store, 'resolve_contributor')
