"""Tests for the ``submit_file`` RPC (0.53.0) and its sibling
desktop-adopt hardening — the daemon half of the AZT persistence
contract (azt-collab/agenda/azt_persistence_server_sync.md, G1–G4).

What's covered:

- G1 ``submit_file`` fast path: staged sibling file is consumed via
  ``os.replace`` + committed; the Result carries ``COMMITTED_LOCAL``
  with a ``head_sha`` param and the response carries the same
  top-level ``head_sha`` (the caller's next base).
- G1 divergent path (the no-clobber crux): a peer commit lands after
  the caller's base → the daemon three-way-merges instead of
  replacing; BOTH sides' entry edits survive; ``MERGED_WITH_LOCAL``
  is returned.
- G1 auto-init: submitting into a registered-but-never-initialized
  working_dir creates the repo rather than losing the write.
- G1 durability-before-identity: contributor unset → bytes still
  land on disk, ``CONTRIBUTOR_UNSET`` returned, no commit made.
- G1 staged-path validation: wrong directory / missing / == target
  are rejected with 400 before any filesystem touch.
- G2 ``head_sha`` param on ``COMMITTED_LOCAL`` from the plain commit
  path (``commit_repo``), and the ``Result.param`` accessor on both
  status mirrors.
- G3 ``ensure_ignore_patterns``: registration appends azt desktop
  ignore patterns idempotently, preserving existing content.
- G4 duplicate-working_dir guard: a second langcode over the same
  tree is refused with 409 + ``existing_langcode``; re-registering
  the SAME langcode still works.
- Debounce burst-collapse: two rapid ``commit_project`` schedules
  return the same job id.
- Status-mirror drift check for ``MERGED_WITH_LOCAL``.

Why this exists: desktop AZT autosaves the whole LIFT on nearly
every edit and never re-reads. Without a base-aware write, its next
save after a daemon-side merge would clobber peer data at the
content level. These tests pin the by-construction guarantee the
contract stands on.
"""

import os
import time

import pytest

from azt_collabd import projects as projects_mod
from azt_collabd import repo as repo_mod
from azt_collabd import server as srv
from azt_collabd import status as S_d
from azt_collabd import store
from azt_collab_client import status as S_c


CONTRIBUTOR = 'Test Person'


def _lift(alpha='alpha', beta='beta'):
    """Minimal two-entry LIFT; entry texts parameterized so tests can
    express per-entry edits on either side of a merge."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<lift version="0.13">\n'
        f'<entry guid="aaaa-0001"><lexical-unit>'
        f'<form lang="sw"><text>{alpha}</text></form>'
        f'</lexical-unit></entry>\n'
        f'<entry guid="bbbb-0002"><lexical-unit>'
        f'<form lang="sw"><text>{beta}</text></form>'
        f'</lexical-unit></entry>\n'
        '</lift>\n'
    ).encode('utf-8')


@pytest.fixture
def project(tmp_path):
    """Register a project (its working_dir is a real temp dir) and
    yield ``(langcode, working_dir)``. The autouse ``azt_home``
    fixture has already redirected ``$AZT_HOME``."""
    langcode = 'sw-x-subtest'
    working_dir = tmp_path / 'project'
    working_dir.mkdir()
    status, resp = srv.dispatch('POST', '/v1/projects/register', {
        'langcode': langcode,
        'working_dir': str(working_dir),
        'lift_path': str(working_dir / 'test.lift'),
    })
    assert status == 200, resp
    yield langcode, str(working_dir)


def _seed_commit(working_dir, content):
    """Write the LIFT + commit it; return the resulting HEAD sha."""
    with open(os.path.join(working_dir, 'test.lift'), 'wb') as fh:
        fh.write(content)
    res = repo_mod.commit_repo(working_dir, CONTRIBUTOR)
    assert res.has(S_d.COMMITTED_LOCAL), res.codes()
    head = repo_mod.head_sha_of(working_dir)
    assert head
    return head


def _stage(working_dir, content, name='test.lift.part'):
    staged = os.path.join(working_dir, name)
    with open(staged, 'wb') as fh:
        fh.write(content)
    return staged


def _submit(langcode, staged, base_sha, rel='test.lift'):
    return srv.dispatch(
        'POST', f'/v1/projects/{langcode}/submit_file',
        {'path': rel, 'staged_path': staged, 'base_sha': base_sha})


# ── Status mirror drift ───────────────────────────────────────────────────


def test_merged_with_local_code_mirrored():
    assert S_d.MERGED_WITH_LOCAL == 'MERGED_WITH_LOCAL'
    assert S_c.MERGED_WITH_LOCAL == 'MERGED_WITH_LOCAL'


def test_result_param_accessor_both_mirrors():
    r_d = S_d.Result().add(S_d.COMMITTED_LOCAL, head_sha='abc123')
    assert r_d.param(S_d.COMMITTED_LOCAL, 'head_sha') == 'abc123'
    assert r_d.param(S_d.PUSHED, 'head_sha', 'dflt') == 'dflt'
    r_c = S_c.Result.from_dict(r_d.to_dict())
    assert r_c.param(S_c.COMMITTED_LOCAL, 'head_sha') == 'abc123'
    assert r_c.param(S_c.COMMITTED_LOCAL, 'absent', 7) == 7


# ── G1: fast path ─────────────────────────────────────────────────────────


def test_submit_file_fast_path_replaces_and_commits(project):
    langcode, working_dir = project
    store.set_contributor(CONTRIBUTOR)
    head0 = _seed_commit(working_dir, _lift())
    v2 = _lift(alpha='ALPHA-EDIT')
    staged = _stage(working_dir, v2)

    status, resp = _submit(langcode, staged, head0)
    assert status == 200, resp
    assert resp['ok'] is True

    on_disk = open(os.path.join(working_dir, 'test.lift'), 'rb').read()
    assert on_disk == v2                      # exact bytes — no merge ran
    assert not os.path.exists(staged)         # staged file consumed
    codes = [s['code'] for s in resp['result']['statuses']]
    assert 'COMMITTED_LOCAL' in codes
    assert 'MERGED_WITH_LOCAL' not in codes
    new_head = repo_mod.head_sha_of(working_dir)
    assert new_head and new_head != head0
    assert resp['head_sha'] == new_head
    committed = [s for s in resp['result']['statuses']
                 if s['code'] == 'COMMITTED_LOCAL'][0]
    assert committed['params']['head_sha'] == new_head


def test_submit_file_auto_inits_bare_working_dir(project):
    """A registered dir that was never git-initialized must not lose
    the write — same recovery contract as ``commit_repo``."""
    langcode, working_dir = project
    store.set_contributor(CONTRIBUTOR)
    v1 = _lift()
    staged = _stage(working_dir, v1)
    status, resp = _submit(langcode, staged, '')
    assert status == 200, resp
    assert open(os.path.join(working_dir, 'test.lift'), 'rb').read() == v1
    assert repo_mod.head_sha_of(working_dir)  # a commit exists now


# ── G1: divergent path (the no-clobber crux) ─────────────────────────────


def test_submit_file_divergent_merges_both_sides(project):
    """Peer commit lands after the caller's base → the daemon merges;
    the caller's entry edit AND the peer's entry edit both survive.
    This is the clobber scenario the whole contract exists for."""
    langcode, working_dir = project
    store.set_contributor(CONTRIBUTOR)
    base = _seed_commit(working_dir, _lift())          # alpha, beta
    # Peer edit on entry bbbb-0002 lands (HEAD advances past base).
    _seed_commit(working_dir, _lift(beta='BETA-PEER'))
    # Caller's save is based on the ORIGINAL base and edits aaaa-0001.
    staged = _stage(working_dir, _lift(alpha='ALPHA-EDIT'))

    status, resp = _submit(langcode, staged, base)
    assert status == 200, resp
    codes = [s['code'] for s in resp['result']['statuses']]
    assert 'MERGED_WITH_LOCAL' in codes
    assert 'COMMITTED_LOCAL' in codes

    merged = open(os.path.join(working_dir, 'test.lift'), 'rb').read()
    assert b'ALPHA-EDIT' in merged            # caller's edit survived
    assert b'BETA-PEER' in merged             # peer's edit NOT clobbered
    assert not os.path.exists(staged)
    assert resp['head_sha'] == repo_mod.head_sha_of(working_dir)


def test_submit_file_empty_base_against_existing_head_merges(project):
    """No declared base (fresh attach) + existing HEAD → merge with
    empty base (add-add), never a plain replace that could clobber."""
    langcode, working_dir = project
    store.set_contributor(CONTRIBUTOR)
    _seed_commit(working_dir, _lift(beta='BETA-ONLY-IN-HEAD'))
    staged = _stage(working_dir, _lift(alpha='ALPHA-ONLY-STAGED'))
    status, resp = _submit(langcode, staged, '')
    assert status == 200, resp
    codes = [s['code'] for s in resp['result']['statuses']]
    assert 'MERGED_WITH_LOCAL' in codes
    merged = open(os.path.join(working_dir, 'test.lift'), 'rb').read()
    assert b'BETA-ONLY-IN-HEAD' in merged
    assert b'ALPHA-ONLY-STAGED' in merged


# ── G1: durability before identity ───────────────────────────────────────


def test_submit_file_contributor_unset_lands_bytes_without_commit(project):
    langcode, working_dir = project
    store.set_contributor('')                  # explicit clear
    head0 = _seed_commit_allowed_empty(working_dir)
    v2 = _lift(alpha='SAVED-WITHOUT-NAME')
    staged = _stage(working_dir, v2)
    status, resp = _submit(langcode, staged, head0)
    assert status == 200, resp
    codes = [s['code'] for s in resp['result']['statuses']]
    assert 'CONTRIBUTOR_UNSET' in codes
    assert 'COMMITTED_LOCAL' not in codes
    # The write is durable even though history refused.
    assert open(os.path.join(working_dir, 'test.lift'),
                'rb').read() == v2
    assert repo_mod.head_sha_of(working_dir) == head0


def _seed_commit_allowed_empty(working_dir):
    """Seed an initial commit with a temporary contributor, then
    return HEAD. Used by the contributor-unset test, which needs a
    repo with history but no stored name."""
    store.set_contributor(CONTRIBUTOR)
    head = None
    try:
        head = _seed_commit(working_dir, _lift())
    finally:
        store.set_contributor('')
    return head


# ── G1: staged-path validation ───────────────────────────────────────────


def test_submit_file_rejects_staged_outside_target_dir(project, tmp_path):
    langcode, working_dir = project
    _seed_commit_contrib(working_dir)
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    staged = str(elsewhere / 'test.lift.part')
    open(staged, 'wb').write(_lift())
    status, resp = _submit(langcode, staged, '')
    assert status == 400
    assert resp['error'] == 'staged_rejected'


def test_submit_file_rejects_missing_staged(project):
    langcode, working_dir = project
    status, resp = _submit(
        langcode, os.path.join(working_dir, 'no-such.part'), '')
    assert status == 400
    assert resp['error'] == 'staged_rejected'


def test_submit_file_rejects_staged_equal_to_target(project):
    langcode, working_dir = project
    target = os.path.join(working_dir, 'test.lift')
    open(target, 'wb').write(_lift())
    status, resp = _submit(langcode, target, '')
    assert status == 400
    assert resp['error'] == 'staged_rejected'


def test_submit_file_rejects_path_traversal(project):
    langcode, working_dir = project
    staged = _stage(working_dir, _lift())
    status, resp = srv.dispatch(
        'POST', f'/v1/projects/{langcode}/submit_file',
        {'path': '../escape.lift', 'staged_path': staged,
         'base_sha': ''})
    assert status == 400
    assert resp['error'] == 'path_rejected'


def test_submit_file_rejects_unknown_project():
    status, resp = srv.dispatch(
        'POST', '/v1/projects/nonexistent-lang/submit_file',
        {'path': 'x.lift', 'staged_path': '/tmp/x', 'base_sha': ''})
    assert status == 404


def _seed_commit_contrib(working_dir):
    store.set_contributor(CONTRIBUTOR)
    return _seed_commit(working_dir, _lift())


def test_desktop_project_shapes_do_not_trip_data_loss_risk(project):
    """A desktop-azt project dir carries settings JSONs, .ldml,
    reports/, dated backups. Whole-tree staging commits the first
    two; G3's .gitignore deliberately excludes the rest — neither is
    data loss. Field repro 2026-07-07: the recorder-shaped whitelist
    walk raised a false DATA_LOSS_RISK (a never-silenced sticky
    banner on peers) on the first desktop commit."""
    langcode, working_dir = project
    store.set_contributor(CONTRIBUTOR)
    open(os.path.join(working_dir,
                      'test.kentr.host1.project.json'), 'w').write('{}')
    os.makedirs(os.path.join(working_dir, 'WritingSystems'))
    open(os.path.join(working_dir, 'WritingSystems', 'xx.ldml'),
         'w').write('<ldml/>')
    os.makedirs(os.path.join(working_dir, 'reports'))
    open(os.path.join(working_dir, 'reports', 'r.html'),
         'w').write('x')                       # gitignored via G3
    open(os.path.join(working_dir, 'test.lift_2026-07-07.txt'),
         'w').write('backup')                  # gitignored via G3
    head0 = _seed_commit(working_dir, _lift())
    staged = _stage(working_dir, _lift(alpha='EDIT'))
    status, resp = _submit(langcode, staged, head0)
    assert status == 200, resp
    codes = [s['code'] for s in resp['result']['statuses']]
    assert 'DATA_LOSS_RISK' not in codes, codes
    assert 'COMMITTED_LOCAL' in codes


# ── G2: head_sha on the plain commit path ────────────────────────────────


def test_commit_repo_reports_head_sha_param(project):
    _, working_dir = project
    with open(os.path.join(working_dir, 'test.lift'), 'wb') as fh:
        fh.write(_lift())
    res = repo_mod.commit_repo(working_dir, CONTRIBUTOR)
    assert res.has(S_d.COMMITTED_LOCAL)
    assert (res.param(S_d.COMMITTED_LOCAL, 'head_sha')
            == repo_mod.head_sha_of(working_dir))


# ── G3: adopt-time .gitignore hardening ──────────────────────────────────


def test_register_appends_azt_ignores(project):
    _, working_dir = project
    gi = open(os.path.join(working_dir, '.gitignore')).read()
    for pat in ('*.lift*txt', '*.gz', '*.7z', 'reports/**',
                'exports/**', '*.pdf'):
        assert pat in gi.splitlines(), f'{pat} missing from .gitignore'


def test_register_gitignore_idempotent_and_preserving(tmp_path):
    working_dir = tmp_path / 'proj2'
    working_dir.mkdir()
    pre_existing = '# mine\ncustom-pattern/\n'
    (working_dir / '.gitignore').write_text(pre_existing)
    for _ in range(2):                       # register twice
        status, resp = srv.dispatch('POST', '/v1/projects/register', {
            'langcode': 'sw-x-idem',
            'working_dir': str(working_dir),
            'lift_path': str(working_dir / 'a.lift'),
        })
        assert status == 200, resp
    lines = (working_dir / '.gitignore').read_text().splitlines()
    assert 'custom-pattern/' in lines        # user content preserved
    assert lines.count('*.lift*txt') == 1    # no duplicate appends
    assert lines.count('*.gz') == 1


def test_ensure_ignore_patterns_returns_added_then_empty(tmp_path):
    d = str(tmp_path / 'proj3')
    os.makedirs(d)
    added = repo_mod.ensure_ignore_patterns(d)
    assert '*.lift*txt' in added
    assert repo_mod.ensure_ignore_patterns(d) == []


# ── G4: duplicate-working_dir guard ──────────────────────────────────────


def test_register_second_langcode_same_dir_409(project):
    langcode, working_dir = project
    status, resp = srv.dispatch('POST', '/v1/projects/register', {
        'langcode': 'other-lang',
        'working_dir': working_dir,
    })
    assert status == 409
    assert resp['error'] == 'working_dir_already_registered'
    assert resp['existing_langcode'] == langcode


def test_register_same_langcode_same_dir_still_updates(project):
    langcode, working_dir = project
    status, resp = srv.dispatch('POST', '/v1/projects/register', {
        'langcode': langcode,
        'working_dir': working_dir,
        'remote_url': 'https://github.com/example/x.git',
    })
    assert status == 200, resp
    assert resp['project']['remote_url'].endswith('x.git')


def test_register_raises_typed_exception_at_module_level(tmp_path):
    d1 = tmp_path / 'd1'
    d1.mkdir()
    projects_mod.register('lang-one', str(d1))
    with pytest.raises(projects_mod.WorkingDirAlreadyRegistered) as ei:
        projects_mod.register('lang-two', str(d1))
    assert ei.value.existing_langcode == 'lang-one'


# ── Debounce burst-collapse ──────────────────────────────────────────────


def test_commit_project_burst_collapses_to_one_job(project):
    langcode, _working_dir = project
    store.set_contributor(CONTRIBUTOR)
    from azt_collabd import scheduler
    job1 = scheduler.commit_project(langcode)
    job2 = scheduler.commit_project(langcode)
    assert job1 == job2
    # Let the debounce timer fire inside the test's tmp AZT_HOME so
    # nothing runs during teardown of a later test.
    deadline = time.time() + 5
    while time.time() < deadline:
        job = scheduler.get_job(job1)
        if job is not None and getattr(job, 'result', None) is not None:
            break
        time.sleep(0.1)
