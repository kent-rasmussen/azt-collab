"""Tests for the GitHub credentials confirmed-flag lifecycle.

v0.27.0 promoted ``github.confirmed`` from a derived field to a
stored flag with reset-on-settings-change semantics. These tests
lock that contract in:

- A bare ``set_github_tokens`` resets confirmed to False.
- A bare ``set_github_app_installed`` resets confirmed to False.
- ``set_github_confirmed(True)`` after a settings save sticks until
  the next settings change.
- ``get_status()`` exposes the stored value, not the legacy derived
  one.

Mirror of GitLab's lifecycle, which has worked since 0.25 — we copy
the asserts to ensure GitHub now matches.
"""

from azt_collabd import store


def test_set_github_tokens_resets_confirmed():
    store.set_github_tokens(access_token='tok', username='alice')
    store.set_github_confirmed(True)
    assert store.get_status()['github']['confirmed'] is True

    # Save a different token — confirmed must drop.
    store.set_github_tokens(access_token='tok2', username='alice')
    assert store.get_status()['github']['confirmed'] is False


def test_set_github_app_installed_resets_confirmed():
    store.set_github_tokens(access_token='tok', username='alice')
    store.set_github_confirmed(True)
    store.set_github_app_installed(True)
    # Flipping app_installed counts as a settings change.
    assert store.get_status()['github']['confirmed'] is False


def test_set_github_confirmed_persists():
    store.set_github_tokens(access_token='tok', username='alice')
    store.set_github_confirmed(True)
    s1 = store.get_status()
    s2 = store.get_status()
    assert s1['github']['confirmed'] is True
    assert s2['github']['confirmed'] is True


def test_status_exposes_stored_not_derived():
    """Pre-0.27.0 confirmed was derived as
    ``connected and app_installed``. With a token + app_installed
    set but confirmed never explicitly set, the new stored field is
    False — we must NOT regress to the derived shape."""
    store.set_github_tokens(access_token='tok', username='alice')
    store.set_github_app_installed(True)
    # Note: set_github_app_installed itself resets confirmed=False,
    # so we cannot assert "True && True → confirmed True" anymore.
    # That's the whole point of the contract change.
    assert store.get_status()['github']['confirmed'] is False


def test_disconnect_clears_confirmed():
    store.set_github_tokens(access_token='tok', username='alice')
    store.set_github_confirmed(True)
    store.clear_github()
    s = store.get_status()
    assert s['github']['connected'] is False
    assert s['github']['confirmed'] is False


def test_gitlab_confirmed_lifecycle_unchanged():
    """Sanity check: the GitLab side hasn't regressed."""
    store.set_gitlab('alice', 'glpat-token')
    store.set_gitlab_confirmed(True)
    assert store.get_status()['gitlab']['confirmed'] is True
    store.set_gitlab('alice', 'different-token')
    assert store.get_status()['gitlab']['confirmed'] is False
