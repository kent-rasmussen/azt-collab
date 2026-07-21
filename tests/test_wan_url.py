"""wan_url — live https conversion of ssh-shaped remote URLs (0.54.11).

The daemon authenticates with a token over HTTPS; dulwich routes
ssh-shaped URLs to SubprocessSSHVendor, which cannot take a password
(field repro 2026-07-21: baf, ``git@github.com:audioword-ui/baf.git``
— every WAN fetch/pull/push died with NotImplementedError). Stored
URLs keep the user's spelling; conversion happens at use time only.
"""

import pytest

from azt_collabd.repo import wan_url, _import_origin_heads


@pytest.mark.parametrize('given,expected', [
    # The field repro.
    ('git@github.com:audioword-ui/baf.git',
     'https://github.com/audioword-ui/baf.git'),
    # scp-style, other hosts / no .git suffix.
    ('git@gitlab.com:owner/repo.git', 'https://gitlab.com/owner/repo.git'),
    ('git@github.com:owner/repo', 'https://github.com/owner/repo'),
    # Explicit ssh scheme, with and without port.
    ('ssh://git@github.com/owner/repo.git',
     'https://github.com/owner/repo.git'),
    ('ssh://git@github.com:22/owner/repo.git',
     'https://github.com/owner/repo.git'),
    ('git+ssh://git@github.com/owner/repo.git',
     'https://github.com/owner/repo.git'),
    # Leading slash in scp path is tolerated.
    ('git@github.com:/owner/repo.git', 'https://github.com/owner/repo.git'),
    # Surrounding whitespace is stripped before conversion.
    (' git@github.com:owner/repo.git ',
     'https://github.com/owner/repo.git'),
])
def test_ssh_shapes_convert(given, expected):
    assert wan_url(given) == expected


@pytest.mark.parametrize('given', [
    '',
    'https://github.com/owner/repo.git',
    'http://github.com/owner/repo.git',
    # https URL with userinfo (installation-token form) — has a
    # scheme, must NOT be touched.
    'https://x-access-token@github.com/owner/repo.git',
    # LAN listener URL (private IP + port) — https already.
    'https://192.168.1.7:34501/baf.git',
    # Local paths, including a windows drive (colon but no '@').
    '/home/user/projects/baf',
    'C:\\Users\\user\\repo',
    # user@host without a colon isn't a git URL — leave it alone.
    'git@github.com',
])
def test_non_ssh_untouched(given):
    assert wan_url(given) == given


def test_none_passes_through():
    assert wan_url(None) is None


def test_import_origin_heads(tmp_path):
    """Only refs/heads/* are mirrored into refs/remotes/origin/*
    (HEAD and tags excluded), matching dulwich's own named-remote
    import."""
    from dulwich.repo import Repo
    r = Repo.init(str(tmp_path))
    try:
        sha_main = b'1' * 40
        sha_tag = b'2' * 40
        n = _import_origin_heads(r, {
            b'refs/heads/main': sha_main,
            b'HEAD': sha_main,
            b'refs/tags/v1': sha_tag,
        })
        assert n == 1
        assert r.refs[b'refs/remotes/origin/main'] == sha_main
        assert b'refs/remotes/origin/v1' not in r.refs.allkeys()
    finally:
        r.close()


def test_import_origin_heads_empty_and_none():
    assert _import_origin_heads(None, {}) == 0
    assert _import_origin_heads(None, None) == 0
