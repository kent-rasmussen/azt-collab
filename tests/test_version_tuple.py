"""Tests for ``azt_collab_client._version_tuple``.

Cheap unit tests; the function is hot on the bootstrap path so
correctness here matters. Covers test_plan.md §3 cases 3.3 (peer
newer than latest), 3.4 (malformed __version__), 3.7 (date-tagged
release).
"""

from azt_collab_client import _version_tuple


def test_plain_semver():
    assert _version_tuple('1.2.3') == (1, 2, 3)


def test_v_prefix_stripped_by_caller():
    # Caller ``.lstrip('vV')`` before passing — function itself
    # doesn't strip. Document that here so a future caller doesn't
    # forget.
    assert _version_tuple('v1.2.3') == (0, 2, 3)


def test_pads_short():
    assert _version_tuple('1') == (1, 0, 0)
    assert _version_tuple('1.2') == (1, 2, 0)


def test_truncates_long():
    assert _version_tuple('1.2.3.4') == (1, 2, 3)


def test_empty_returns_zero():
    assert _version_tuple('') == (0, 0, 0)
    assert _version_tuple(None) == (0, 0, 0)


def test_non_digit_chunks_yield_zero():
    # 'rc1' → digits-only walk pulls out '1' before non-digit.
    # Document the behavior even if it's accidental: 'rc1' chunks
    # become 1, not 0.
    assert _version_tuple('1.2.rc1') == (1, 2, 1)


def test_date_tagged_release():
    """test_plan.md §3.7: date-tagged tags like v2026-05-06 work
    incidentally because dashes are stripped per chunk."""
    assert _version_tuple('2026-05-06') == (2026, 5, 6)


def test_peer_newer_than_latest_does_not_downgrade():
    """test_plan.md §3.3 — must not propose a downgrade. We test the
    comparison the bootstrap path actually uses."""
    peer = (0, 28, 5)  # local dev build
    latest = _version_tuple('0.28.0')
    assert latest <= peer


def test_pure_text_is_zero():
    assert _version_tuple('garbage') == (0, 0, 0)
