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


# ── refresh-broken lifecycle (0.34.3) ─────────────────────────────────────
#
# ``get_valid_github_token`` records ``refresh_broken=True`` on a
# failed OAuth refresh (``incorrect_client_credentials`` etc.) and
# exposes it via ``get_status``. Fresh tokens (re-auth via device
# flow → ``set_github_tokens``) must clear it atomically — otherwise
# the user re-authenticates and the toast keeps firing forever.
# These tests pin that contract.

def test_set_github_tokens_clears_refresh_broken():
    store.set_github_tokens(access_token='tok', username='alice')
    store._set_github_refresh_broken('incorrect_client_credentials')
    assert store.get_status()['github']['refresh_broken'] is True

    # Re-auth = fresh tokens. Must clear the flag.
    store.set_github_tokens(access_token='tok2', username='alice')
    assert store.get_status()['github']['refresh_broken'] is False
    state = store.github_refresh_state()
    assert state['broken'] is False
    assert state['error'] == ''


def test_status_exposes_access_token_expires_at():
    store.set_github_tokens(access_token='tok', username='alice')
    expires_at = store.get_status()['github']['access_token_expires_at']
    # 8h-from-issuance window, in seconds. token_time stamped to
    # ~now, so the deadline is ~now+28800. Allow generous slack
    # for slow CI.
    import time
    delta = expires_at - time.time()
    assert 8 * 3600 - 60 <= delta <= 8 * 3600 + 60


def test_github_refresh_state_with_no_token():
    """No token ever stored → broken=False, expires_at=0. Peers
    should treat 0 as 'no deadline to render' and skip the toast."""
    state = store.github_refresh_state()
    assert state['broken'] is False
    assert state['expires_at'] == 0
    assert state['error'] == ''


def test_clear_github_clears_refresh_state():
    store.set_github_tokens(access_token='tok', username='alice')
    store._set_github_refresh_broken('test')
    store.clear_github()
    state = store.github_refresh_state()
    assert state['broken'] is False
    assert state['expires_at'] == 0
