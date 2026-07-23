"""Merge guard: refuse to merge histories with no common ancestor
(0.54.19, LAN audit F16). Pre-guard, ``_merge_diverged`` silently
ran with an EMPTY base when ``_find_merge_base`` returned nothing —
unioning two unrelated projects that share a langcode label and
pushing the result to both sides.

Known limit (documented on the exception): projects FORKED from one
another share an ancestor, so this guard does not fire for them —
that case needs project identity beyond the langcode
(agenda/project_identity_beyond_langcode.md).
"""

import pytest

pytest.importorskip('dulwich')

from azt_collabd.repo import _merge_diverged, UnrelatedHistoriesError


def _orphan_commit(repo, msg):
    from dulwich.objects import Commit, Tree
    tree = Tree()
    repo.object_store.add_object(tree)
    c = Commit()
    c.tree = tree.id
    c.parents = []
    c.author = c.committer = b'test <t@t>'
    c.author_time = c.commit_time = 0
    c.author_timezone = c.commit_timezone = 0
    c.encoding = b'UTF-8'
    c.message = msg
    repo.object_store.add_object(c)
    return c.id


def test_no_common_ancestor_refused(tmp_path):
    from dulwich.repo import Repo
    r = Repo.init(str(tmp_path))
    try:
        a = _orphan_commit(r, b'lineage A root')
        b = _orphan_commit(r, b'lineage B root')
        with pytest.raises(UnrelatedHistoriesError):
            _merge_diverged(r, str(tmp_path), 'main', a, b)
    finally:
        r.close()
