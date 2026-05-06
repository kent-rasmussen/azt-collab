"""Tests for ``azt_collab_client.ui.update``.

Mocked-Auto: we patch ``urllib.request.urlopen`` to drive every
GitHub-API path, and we don't touch real Android jnius. Coverage
maps to test_plan.md sections 1 (network), 3 (peer version), 4
(prerelease filtering).

Tests that need the install-intent dispatch (jnius / MediaStore /
ACTION_VIEW) live in the manual matrix — see test_plan.md §8.
"""

import io
import json
from unittest.mock import patch

import pytest

from azt_collab_client.ui import update as upd


# ── helpers ───────────────────────────────────────────────────────────────

class _FakeResponse:
    """Tiny stand-in for ``urllib.request.urlopen`` context manager."""

    def __init__(self, payload):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode()
        self._buf = io.BytesIO(payload)

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _release(tag, *, prerelease=False, draft=False, asset='peer.apk',
             size=1234, browser_url='https://example/peer.apk'):
    return {
        'tag_name': tag,
        'prerelease': prerelease,
        'draft': draft,
        'assets': [{
            'name': asset,
            'size': size,
            'browser_download_url': browser_url,
        }],
    }


# ── _fetch_latest: prerelease filtering ───────────────────────────────────

def test_fetch_latest_skips_prereleases():
    """test_plan.md §3.8: a release marked prerelease=true must be
    skipped in favor of the most recent stable. Regression for a
    bug shipped in v0.28.0."""
    listing = [
        _release('1.0.0-rc.1', prerelease=True),
        _release('0.9.5'),
        _release('0.9.4'),
    ]
    with patch('urllib.request.urlopen',
               return_value=_FakeResponse(listing)):
        rel = upd._fetch_latest('owner/repo')
    assert rel['tag_name'] == '0.9.5'


def test_fetch_latest_skips_drafts():
    listing = [
        _release('1.0.0', draft=True),
        _release('0.9.5'),
    ]
    with patch('urllib.request.urlopen',
               return_value=_FakeResponse(listing)):
        rel = upd._fetch_latest('owner/repo')
    assert rel['tag_name'] == '0.9.5'


def test_fetch_latest_falls_back_when_all_prereleases():
    """Every release in the first page is a prerelease — fall back
    to /releases/latest singleton (which may itself be a
    prerelease, but at least there's *something* to return)."""
    listing = [
        _release('1.0.0-rc.2', prerelease=True),
        _release('1.0.0-rc.1', prerelease=True),
    ]
    singleton = _release('1.0.0-rc.2', prerelease=True)

    call_count = {'n': 0}

    def _urlopen(req, timeout=None):
        call_count['n'] += 1
        if call_count['n'] == 1:
            return _FakeResponse(listing)
        return _FakeResponse(singleton)

    with patch('urllib.request.urlopen', side_effect=_urlopen):
        rel = upd._fetch_latest('owner/repo')
    assert rel['tag_name'] == '1.0.0-rc.2'


def test_fetch_latest_falls_back_on_listing_error():
    """If /releases?per_page=20 returns junk or 5xx, fall back to
    /releases/latest."""
    singleton = _release('0.9.5')

    call_count = {'n': 0}

    def _urlopen(req, timeout=None):
        call_count['n'] += 1
        if call_count['n'] == 1:
            raise Exception('listing failed')
        return _FakeResponse(singleton)

    with patch('urllib.request.urlopen', side_effect=_urlopen):
        rel = upd._fetch_latest('owner/repo')
    assert rel['tag_name'] == '0.9.5'


# ── _pick_asset ───────────────────────────────────────────────────────────

def test_pick_asset_finds_match():
    rel = _release('1.0', asset='azt_recorder.apk')
    a = upd._pick_asset(rel, 'azt_recorder.apk')
    assert a is not None
    assert a['name'] == 'azt_recorder.apk'


def test_pick_asset_returns_none_when_missing():
    """test_plan.md §3.6: asset filename mismatch returns None,
    upstream surfaces "no {file} in release {tag}"."""
    rel = _release('1.0', asset='something_else.apk')
    assert upd._pick_asset(rel, 'azt_recorder.apk') is None


# ── check_for_update on desktop is a translated error ─────────────────────

def test_check_for_update_desktop_routes_to_error(desktop):
    """Non-android hosts must call on_error with the translated
    "Android only" message and not attempt any network or jnius
    work."""
    errors = []

    upd.check_for_update(
        repo='owner/repo',
        current_version='0.0.0',
        asset_filename='peer.apk',
        on_status=lambda _msg: None,
        on_error=errors.append,
    )

    assert len(errors) == 1
    assert 'Android' in errors[0]


# ── version comparisons through the full helper (mocked Android) ──────────

def _drive_check(android, *, current, latest, asset='peer.apk',
                listing=None):
    """Helper: run check_for_update with a fake urlopen returning a
    canned release listing. Returns the captured callback args."""
    captured = {'status': [], 'no_update': 0, 'error': []}
    if listing is None:
        listing = [_release(latest, asset=asset)]

    def _urlopen(req, timeout=None):
        return _FakeResponse(listing)

    # Don't actually hit jnius — patch the install path so the
    # helper's UI marshaling is the only thing we exercise.
    with patch('urllib.request.urlopen', side_effect=_urlopen), \
            patch.object(upd, '_can_install_packages',
                         return_value=False), \
            patch.object(upd, '_open_unknown_sources_settings'), \
            patch.object(upd, '_media_store_uri',
                         return_value='content://x'), \
            patch.object(upd, '_trigger_install'):
        # Bypass Clock.schedule_once → run the lambda inline so the
        # test thread sees callbacks deterministically.
        with patch('kivy.clock.Clock.schedule_once',
                   side_effect=lambda fn, _t: fn(0)):
            upd.check_for_update(
                repo='owner/repo',
                current_version=current,
                asset_filename=asset,
                on_status=captured['status'].append,
                on_no_update=lambda: captured.__setitem__(
                    'no_update', captured['no_update'] + 1),
                on_error=captured['error'].append,
            )
            # Worker thread is daemon — join it briefly to let the
            # callbacks land.
            import threading
            for t in threading.enumerate():
                if t.daemon and t is not threading.main_thread():
                    t.join(timeout=2.0)
    return captured


def test_no_update_when_current_matches_latest(android):
    """test_plan.md §3.1: peer at latest → on_no_update fires; no
    download attempted."""
    captured = _drive_check(android, current='1.0.0', latest='1.0.0')
    assert captured['no_update'] == 1
    assert not captured['error']


def test_no_update_when_peer_is_newer(android):
    """test_plan.md §3.3: dev build newer than published latest. We
    must not propose a 'downgrade' install."""
    captured = _drive_check(android, current='2.0.0', latest='1.0.0')
    assert captured['no_update'] == 1


def test_install_path_when_newer_available(android):
    """When a newer release exists, on_no_update must NOT fire and
    on_status should report progress / install transitions."""
    captured = _drive_check(android, current='1.0.0', latest='2.0.0')
    assert captured['no_update'] == 0
    # Status messages should include at least the check + install
    # transitions. We don't pin exact strings — that's i18n's job —
    # just assert *something* came through.
    assert captured['status']


def test_missing_asset_routes_to_error(android):
    """test_plan.md §3.6: latest release exists but our asset name
    isn't in it. Surfaces a translated error."""
    listing = [_release('2.0.0', asset='wrong_name.apk')]
    captured = _drive_check(
        android, current='1.0.0', latest='2.0.0',
        asset='peer.apk', listing=listing)
    assert captured['error']


def test_network_error_routes_to_error(android):
    """test_plan.md §1.1 / 1.2: urlopen raises → translated error."""
    captured = {'error': []}

    def _urlopen(req, timeout=None):
        raise OSError('network down')

    with patch('urllib.request.urlopen', side_effect=_urlopen), \
            patch('kivy.clock.Clock.schedule_once',
                  side_effect=lambda fn, _t: fn(0)):
        upd.check_for_update(
            repo='owner/repo',
            current_version='1.0.0',
            asset_filename='peer.apk',
            on_status=lambda _msg: None,
            on_error=captured['error'].append,
        )
        import threading
        for t in threading.enumerate():
            if t.daemon and t is not threading.main_thread():
                t.join(timeout=2.0)
    assert captured['error']
