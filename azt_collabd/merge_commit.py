"""
Format a merge commit message.

Author + committer for merge commits is the bot identity:
    <slug>[bot] <<slug>[bot]@users.noreply.github.com>

Message body:

    Merge origin/<branch> into <branch>

    Local commits:
      <sha> <subject>  (<contributor>)
      ...
    Remote commits:
      <sha> <subject>  (<author>)
      ...
    Conflicts (azt-lift-conflict markers added):
      <lift-path>: <guid> <kind>
      ...

    Co-authored-by: <Local Contributor> <local@device>
    Co-authored-by: <Remote Author> <remote@email>

Co-author trailers are deduplicated. Conflicts section is omitted if
the merge was clean.
"""

from . import config as _config


def bot_identity():
    slug = _config.get()['app_slug']
    return f'{slug}[bot] <{slug}[bot]@users.noreply.github.com>'


def _short(sha):
    if isinstance(sha, bytes):
        sha = sha.decode('ascii', errors='replace')
    return (sha or '')[:8]


def _subject(message):
    if isinstance(message, bytes):
        message = message.decode('utf-8', errors='replace')
    return (message.splitlines() or [''])[0].strip()


def _author_str(author):
    if isinstance(author, bytes):
        return author.decode('utf-8', errors='replace')
    return str(author or '')


def _format_commit_line(commit_dict):
    """*commit_dict* has keys 'sha', 'message', 'author'."""
    sha = _short(commit_dict.get('sha', ''))
    subj = _subject(commit_dict.get('message', ''))
    author = _author_str(commit_dict.get('author', ''))
    # Trim email out of "Name <email>" for the parenthetical
    name = author.split('<', 1)[0].strip() if '<' in author else author
    return f'  {sha} {subj}  ({name})' if name else f'  {sha} {subj}'


def build_merge_message(branch, local_commits, remote_commits, conflicts):
    """Build a merge commit message.

    *local_commits* and *remote_commits* are iterables of dicts with
    keys 'sha', 'message', 'author'. *conflicts* is a list of
    lift_merge.Conflict.
    """
    lines = [f'Merge origin/{branch} into {branch}', '']

    if local_commits:
        lines.append('Local commits:')
        for c in local_commits:
            lines.append(_format_commit_line(c))
        lines.append('')

    if remote_commits:
        lines.append('Remote commits:')
        for c in remote_commits:
            lines.append(_format_commit_line(c))
        lines.append('')

    if conflicts:
        lines.append('Conflicts (azt-lift-conflict markers added):')
        for cf in conflicts:
            path = getattr(cf, 'path', '') or '?'
            guid = getattr(cf, 'guid', '') or '?'
            kind = getattr(cf, 'kind', '') or '?'
            lines.append(f'  {path}: {guid} ({kind})')
        lines.append('')

    seen = set()
    trailers = []
    for c in list(local_commits) + list(remote_commits):
        a = _author_str(c.get('author', '')).strip()
        if not a or a in seen:
            continue
        seen.add(a)
        # Trailers want "Name <email>" form. If no "<", coerce.
        if '<' not in a:
            slug = a.lower().replace(' ', '_') or 'contributor'
            a = f'{a} <{slug}@device>'
        trailers.append(f'Co-authored-by: {a}')
    lines.extend(trailers)

    return '\n'.join(lines).rstrip() + '\n'
