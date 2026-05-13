"""Tests for the per-project ``repo_slug`` field shipped in
azt_collabd 0.39.0.

What's covered:

- ``Project.repo_slug`` round-trips through
  ``register`` / ``set_repo_slug`` / ``get``.
- ``register`` with ``repo_slug=None`` preserves a previously-set
  value; with ``repo_slug=''`` explicitly clears.
- ``set_repo_slug`` on an unknown langcode is a silent no-op
  (matches ``set_cawl_image_repo``'s shape).
- Endpoint ``POST /v1/projects/<lang>/repo_slug`` persists and
  echoes the updated ``Project``.
- Endpoint surfaces 404 on unknown project, 400 on missing
  field.
- ``Project`` returned by ``_h_get_project`` / ``_h_project_status``
  carries ``repo_slug``.
- Decode-only client-side ``Project`` dataclass round-trips the
  field.

Why ``repo_slug`` exists: peer-side ``collab_langcode`` peer_pref
in recorder ≤1.41.2 was a suite-wide scalar holding what was
actually per-project data. Recorder 1.41.3 dropped it under the
no-daemon-owned-caches rule; daemon now owns the value.
"""

import pytest

from azt_collabd import projects as projects_mod
from azt_collabd import server as srv
from azt_collab_client.projects import Project as ClientProject


# ── Project dataclass round-trip ────────────────────────────────────────


def test_register_round_trips_repo_slug():
    p = projects_mod.register(
        'sw-x-kent', '/tmp/swproj', repo_slug='my-vanity-name')
    assert p.repo_slug == 'my-vanity-name'
    # Reload from disk and the value persists.
    again = projects_mod.get('sw-x-kent')
    assert again is not None
    assert again.repo_slug == 'my-vanity-name'


def test_register_none_preserves_existing_repo_slug():
    """Re-registering without passing repo_slug preserves the
    previously-set value. Empty string clears."""
    projects_mod.register('sw', '/tmp/sw', repo_slug='alpha')
    # No-slug re-register preserves.
    projects_mod.register('sw', '/tmp/sw2')
    assert projects_mod.get('sw').repo_slug == 'alpha'
    # Explicit empty string clears.
    projects_mod.register('sw', '/tmp/sw2', repo_slug='')
    assert projects_mod.get('sw').repo_slug == ''


def test_set_repo_slug_updates_existing_project():
    projects_mod.register('en', '/tmp/en')
    projects_mod.set_repo_slug('en', 'custom-name')
    assert projects_mod.get('en').repo_slug == 'custom-name'


def test_set_repo_slug_on_missing_project_is_silent():
    """Setter on an unregistered langcode is a no-op (and
    doesn't raise). The handler-side guard is what surfaces
    the 404; the storage layer just persists nothing."""
    projects_mod.set_repo_slug('mystery', 'x')
    assert projects_mod.get('mystery') is None


def test_set_repo_slug_empty_clears():
    projects_mod.register('zu', '/tmp/zu', repo_slug='something')
    projects_mod.set_repo_slug('zu', '')
    assert projects_mod.get('zu').repo_slug == ''


# ── Endpoint round-trip via dispatcher ──────────────────────────────────


def _register(langcode='sw-x-test', tmpdir=None):
    if tmpdir is None:
        tmpdir = '/tmp/' + langcode
    import os
    os.makedirs(tmpdir, exist_ok=True)
    return projects_mod.register(langcode, tmpdir)


def test_endpoint_set_repo_slug_persists():
    _register()
    status, resp = srv.dispatch(
        'POST', '/v1/projects/sw-x-test/repo_slug',
        {'repo_slug': 'shiny-name'})
    assert status == 200
    assert resp['ok'] is True
    assert resp['project']['repo_slug'] == 'shiny-name'
    assert projects_mod.get('sw-x-test').repo_slug == 'shiny-name'


def test_endpoint_set_repo_slug_empty_clears():
    _register()
    projects_mod.set_repo_slug('sw-x-test', 'something')
    status, _resp = srv.dispatch(
        'POST', '/v1/projects/sw-x-test/repo_slug',
        {'repo_slug': ''})
    assert status == 200
    assert projects_mod.get('sw-x-test').repo_slug == ''


def test_endpoint_set_repo_slug_unknown_project_returns_404():
    status, resp = srv.dispatch(
        'POST', '/v1/projects/mystery/repo_slug',
        {'repo_slug': 'x'})
    assert status == 404
    assert resp['error'] == 'project_not_found'


def test_endpoint_set_repo_slug_missing_field():
    _register()
    status, resp = srv.dispatch(
        'POST', '/v1/projects/sw-x-test/repo_slug', {})
    assert status == 400
    assert resp['error'] == 'missing_repo_slug'


def test_endpoint_set_repo_slug_invalid_body_type():
    _register()
    # Non-dict body — handler-level guard.
    status, resp = srv.dispatch(
        'POST', '/v1/projects/sw-x-test/repo_slug', 'not-a-dict')
    assert status == 400


def test_endpoint_strips_whitespace():
    """Same shape as set_cawl_image_repo: setter strips surrounding
    whitespace before persisting."""
    _register()
    status, _resp = srv.dispatch(
        'POST', '/v1/projects/sw-x-test/repo_slug',
        {'repo_slug': '  weird-name  '})
    assert status == 200
    assert projects_mod.get('sw-x-test').repo_slug == 'weird-name'


# ── repo_slug surfaces on Project getters ───────────────────────────────


def test_get_project_endpoint_returns_repo_slug():
    projects_mod.register('vi-x-test', '/tmp/vi-x-test',
                          repo_slug='my-override')
    status, resp = srv.dispatch('GET', '/v1/projects/vi-x-test', None)
    assert status == 200
    assert resp['project']['repo_slug'] == 'my-override'


def test_project_status_endpoint_returns_repo_slug():
    """``project_status`` carries per-project metadata
    (``repo_slug``, ``cawl_image_repo``) alongside the git
    summary so peers can read status + identity in one
    round-trip — the shape the recorder asked for in
    NOTES_TO_DAEMON.md when filing the per-project repo-slug
    request."""
    projects_mod.register('th-x-test', '/tmp/th-x-test',
                          repo_slug='th-shiny')
    status, resp = srv.dispatch(
        'GET', '/v1/projects/th-x-test/status', None)
    assert status == 200
    assert resp['repo_slug'] == 'th-shiny'
    # And the client-side ProjectStatus decoder picks it up:
    from azt_collab_client.projects import ProjectStatus
    ps = ProjectStatus.from_dict(resp)
    assert ps.repo_slug == 'th-shiny'


def test_list_projects_endpoint_returns_repo_slug():
    projects_mod.register('zh-x-test', '/tmp/zh-x-test',
                          repo_slug='zh-vanity')
    status, resp = srv.dispatch('GET', '/v1/projects', None)
    assert status == 200
    entries = {p['langcode']: p for p in resp['projects']}
    assert entries['zh-x-test']['repo_slug'] == 'zh-vanity'


# ── Client-side dataclass round-trip ────────────────────────────────────


def test_client_project_decodes_repo_slug():
    p = ClientProject.from_dict({
        'langcode': 'sw',
        'working_dir': '/tmp/sw',
        'repo_slug': 'my-vanity',
    })
    assert p.repo_slug == 'my-vanity'


def test_client_project_defaults_repo_slug_empty():
    """Pre-0.39 daemons don't emit the field. Forward-compat
    default is ``''`` so peers built against the new client
    don't crash on older daemons."""
    p = ClientProject.from_dict({
        'langcode': 'sw',
        'working_dir': '/tmp/sw',
    })
    assert p.repo_slug == ''


def test_client_project_tolerates_null_repo_slug():
    """Belt-and-braces: if a daemon ever emits explicit null
    (shouldn't, but the client should tolerate), coerce to ''
    so consumers can rely on the field being a string."""
    p = ClientProject.from_dict({
        'langcode': 'sw',
        'working_dir': '/tmp/sw',
        'repo_slug': None,
    })
    assert p.repo_slug == ''
