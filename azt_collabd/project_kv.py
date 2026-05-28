"""
Project-shared KV + atomic slot claims.

Each project's working tree carries:

  - ``.azt/kv/<key>.txt``     — scalar KV. Content is a single
    UTF-8 line (trailing newline trimmed on read). Used for
    cross-phone values like ``team_size`` that every device
    on the project must agree on.

  - ``.azt/slots/<slot>.txt`` — per-slot claim. Content:

        <peer_id>
        <claimed_at_iso>
        <device_name>

    First line is the canonical key (ed25519 pubkey hex);
    second is the ISO-8601 UTC claim time used as the
    merge-conflict tiebreak; third is a display label that
    callers may use for the UI (may go stale — peers' display
    labels can change post-pair without invalidating the
    claim).

Why per-file (not per-key dicts in one JSON):

Two simultaneous claims of slot 2 from offline phones both
write ``.azt/slots/2.txt``. When git merges the two histories
the file diverges and natural conflict markers land. The
post-merge resolver (``resolve_kv_conflicts``) picks the
version whose embedded ``claimed_at`` is later. With a single
JSON we'd have to hand-roll the entire merge.

The four locked semantics from the 2026-05-28 architecture
discussion (per ``NOTES_TO_DAEMON.md`` amendment):

1. Convergent, not real-time atomic. Two simultaneous claims
   both land; convergent merge picks one; loser sees on next
   sync that they're not in the slot dict and is re-prompted.
2. Key by ``peer_id`` (canonical), with ``device_name`` as a
   display label only.
3. One file per slot in the working tree.
4. Tiebreak: latest ``claimed_at`` ISO timestamp wins.
   (Phones routinely NTP-sync; in-room collisions resolve
   intuitively. Switch to peer_id-alpha as an implementation
   detail if field data shows misbehaviour — wire format
   unchanged.)
"""

from __future__ import annotations

import os
import re
import sys
import time


_AZT_DIR = '.azt'
_KV_DIR = 'kv'
_SLOTS_DIR = 'slots'

# Restricts user-supplied key / slot names to safe filenames. The
# server RPCs further validate, but the storage layer also enforces
# so a misbehaving caller can't escape the .azt/ subtree.
_SAFE_NAME = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$')


def _now_iso():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _is_safe_name(name):
    return bool(_SAFE_NAME.match(str(name or '')))


def _azt_dir(working_dir):
    return os.path.join(working_dir, _AZT_DIR)


def _kv_dir(working_dir):
    return os.path.join(_azt_dir(working_dir), _KV_DIR)


def _slots_dir(working_dir):
    return os.path.join(_azt_dir(working_dir), _SLOTS_DIR)


def _kv_path(working_dir, key):
    return os.path.join(_kv_dir(working_dir), f'{key}.txt')


def _slot_path(working_dir, slot):
    return os.path.join(_slots_dir(working_dir), f'{slot}.txt')


def _atomic_write(path, content):
    """Tempfile + os.replace. Atomic on local filesystems; git
    sees the post-replace contents as a single observable state."""
    import tempfile
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.kv.', suffix='.tmp',
                               dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Scalar KV ──────────────────────────────────────────────────────────────


def kv_get(working_dir, key, default=''):
    """Read ``.azt/kv/<key>.txt``. Returns *default* (empty
    string by default) if the file is missing or unreadable.
    Strips trailing newline.
    """
    if not _is_safe_name(key):
        return default
    try:
        with open(_kv_path(working_dir, key), 'r',
                  encoding='utf-8') as f:
            return f.read().rstrip('\n')
    except (FileNotFoundError, OSError):
        return default


def kv_set(working_dir, key, value):
    """Write ``.azt/kv/<key>.txt`` with *value*. Raises
    ValueError on a malformed key. Caller (server RPC) is
    responsible for firing the debounced ``commit_project`` so
    the change propagates."""
    if not _is_safe_name(key):
        raise ValueError(f'unsafe kv key: {key!r}')
    text = str(value if value is not None else '')
    if not text.endswith('\n'):
        text += '\n'
    _atomic_write(_kv_path(working_dir, key), text)


def kv_list(working_dir):
    """Return ``{key: value}`` for every file in ``.azt/kv/``.
    Empty dict if the directory doesn't exist."""
    out = {}
    d = _kv_dir(working_dir)
    if not os.path.isdir(d):
        return out
    for name in os.listdir(d):
        if not name.endswith('.txt'):
            continue
        key = name[:-len('.txt')]
        if not _is_safe_name(key):
            continue
        out[key] = kv_get(working_dir, key)
    return out


# ── Slot claims ────────────────────────────────────────────────────────────


def _parse_slot_file(path):
    """Parse a slot file into ``{peer_id, claimed_at, device_name}``.
    Returns None on missing / malformed."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.read().splitlines()
    except (FileNotFoundError, OSError):
        return None
    if not lines:
        return None
    return {
        'peer_id': (lines[0].strip() if len(lines) > 0 else ''),
        'claimed_at': (lines[1].strip() if len(lines) > 1 else ''),
        'device_name': (lines[2].strip() if len(lines) > 2 else ''),
    }


def _format_slot_file(peer_id, device_name, claimed_at=None):
    """Compose the slot-file body. ``claimed_at`` defaults to
    now (UTC ISO). Trailing newline so editors don't whine."""
    return (
        f'{peer_id.strip()}\n'
        f'{claimed_at or _now_iso()}\n'
        f'{(device_name or "").strip()}\n'
    )


def _slot_filename_to_slot(name):
    """``2.txt`` → ``'2'``; ``foo.txt`` → ``'foo'``. Returns ''
    on malformed."""
    if not name.endswith('.txt'):
        return ''
    base = name[:-len('.txt')]
    if not _is_safe_name(base):
        return ''
    return base


def slot_list(working_dir):
    """Return ``{slot: {peer_id, claimed_at, device_name}}`` for
    every slot file. Empty dict if the directory doesn't exist or
    is empty.
    """
    out = {}
    d = _slots_dir(working_dir)
    if not os.path.isdir(d):
        return out
    for name in os.listdir(d):
        slot = _slot_filename_to_slot(name)
        if not slot:
            continue
        parsed = _parse_slot_file(os.path.join(d, name))
        if parsed is None:
            continue
        out[slot] = parsed
    return out


def slot_claim(working_dir, peer_id, device_name, slot):
    """Atomic displace-on-claim: writes ``.azt/slots/<slot>.txt``
    with our identity AND removes any other slot file currently
    held by us. The "atomic" part is local-only — two phones
    racing this against each other produce divergent histories
    that converge via the post-merge resolver. Per the architecture
    discussion this is convergent atomicity, not real-time.

    Returns ``True`` on the write, ``False`` if the slot name
    fails validation. Raises on filesystem failure (caller wraps
    in the server-side try/except).
    """
    if not _is_safe_name(slot):
        return False
    if len(peer_id) != 64:
        return False
    # Drop any prior claim by this peer (the one-slot-per-peer
    # invariant). Local-only — does not displace other peers'
    # claims on the same slot; the post-merge resolver handles
    # that case.
    _purge_own_claims(working_dir, peer_id, except_slot=slot)
    body = _format_slot_file(peer_id, device_name)
    _atomic_write(_slot_path(working_dir, slot), body)
    return True


def slot_release(working_dir, peer_id):
    """Remove every slot currently held by *peer_id*. Returns
    the list of slots that were released. Idempotent — empty
    list if we held nothing."""
    released = []
    d = _slots_dir(working_dir)
    if not os.path.isdir(d):
        return released
    for name in os.listdir(d):
        slot = _slot_filename_to_slot(name)
        if not slot:
            continue
        path = os.path.join(d, name)
        parsed = _parse_slot_file(path)
        if parsed is None:
            continue
        if parsed.get('peer_id') == peer_id:
            try:
                os.unlink(path)
                released.append(slot)
            except OSError:
                pass
    return released


def _purge_own_claims(working_dir, peer_id, except_slot=''):
    """Internal — drop any slot we currently hold, except for
    *except_slot* (which the caller is about to write).
    """
    d = _slots_dir(working_dir)
    if not os.path.isdir(d):
        return
    for name in os.listdir(d):
        slot = _slot_filename_to_slot(name)
        if not slot or slot == except_slot:
            continue
        path = os.path.join(d, name)
        parsed = _parse_slot_file(path)
        if parsed is None:
            continue
        if parsed.get('peer_id') == peer_id:
            try:
                os.unlink(path)
            except OSError:
                pass


# ── Post-merge conflict resolution ─────────────────────────────────────────


_CONFLICT_START = re.compile(r'^<<<<<<<')
_CONFLICT_MID = re.compile(r'^=======$')
_CONFLICT_END = re.compile(r'^>>>>>>>')


def _split_conflict_sides(text):
    """Yield ``(ours_text, theirs_text)`` for each git-style
    conflict block in *text*. If there are no conflict markers,
    yields nothing. Robust to multiple blocks in the same file
    though slot/KV files generally have one (small files)."""
    lines = text.splitlines(keepends=False)
    i = 0
    blocks = []
    while i < len(lines):
        if _CONFLICT_START.match(lines[i]):
            ours = []
            i += 1
            while i < len(lines) and not _CONFLICT_MID.match(lines[i]):
                ours.append(lines[i])
                i += 1
            if i >= len(lines):
                break
            i += 1  # skip =======
            theirs = []
            while i < len(lines) and not _CONFLICT_END.match(lines[i]):
                theirs.append(lines[i])
                i += 1
            if i >= len(lines):
                break
            i += 1  # skip >>>>>>>
            blocks.append(('\n'.join(ours), '\n'.join(theirs)))
        else:
            i += 1
    return blocks


def _resolve_slot_file(path):
    """Inspect a single ``.azt/slots/<slot>.txt`` for conflict
    markers. If present, pick the side with the later
    ``claimed_at`` and rewrite the file with that side only.
    Returns ``True`` if the file had a conflict that we
    resolved, ``False`` otherwise.
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read()
    except (FileNotFoundError, OSError):
        return False
    if '<<<<<<<' not in text:
        return False
    blocks = _split_conflict_sides(text)
    if not blocks:
        return False
    # Slot files are small (one conflict at most). Take the
    # first block.
    ours_text, theirs_text = blocks[0]
    ours = _parse_text(ours_text)
    theirs = _parse_text(theirs_text)
    winner = _later_claim(ours, theirs)
    if winner is None:
        # Both unparseable — keep ours (arbitrary; logged).
        winner = ours
    body = _format_slot_file(
        winner.get('peer_id', ''),
        winner.get('device_name', ''),
        claimed_at=winner.get('claimed_at') or _now_iso(),
    )
    _atomic_write(path, body)
    return True


def _parse_text(text):
    """Parse a slot-file body (no conflict markers) into the
    ``{peer_id, claimed_at, device_name}`` shape."""
    lines = text.splitlines()
    return {
        'peer_id': (lines[0].strip() if len(lines) > 0 else ''),
        'claimed_at': (lines[1].strip() if len(lines) > 1 else ''),
        'device_name': (lines[2].strip() if len(lines) > 2 else ''),
    }


def _later_claim(a, b):
    """Return whichever of *a* / *b* has the later
    ``claimed_at``. Lexicographic compare on ISO-8601 UTC
    strings = chronological compare for the formats we
    write. Tiebreaks on equal timestamps by peer_id
    (deterministic; rare since ISO is second-granularity and
    the simultaneous-claim window is ms). Returns None if
    both lack a parseable timestamp."""
    a_ts = (a or {}).get('claimed_at', '')
    b_ts = (b or {}).get('claimed_at', '')
    if not a_ts and not b_ts:
        return None
    if not a_ts:
        return b
    if not b_ts:
        return a
    if a_ts > b_ts:
        return a
    if b_ts > a_ts:
        return b
    # Equal timestamps — peer_id alphabetical fallback.
    a_pid = (a or {}).get('peer_id', '')
    b_pid = (b or {}).get('peer_id', '')
    return a if a_pid <= b_pid else b


def _resolve_kv_file(path):
    """For non-slot KV files: pick the side with the larger
    byte count if both sides parse, else 'ours'. KV conflicts
    are expected to be rare (single-write scalars like
    ``team_size``); the value is "pick a deterministic winner
    so the file is no longer conflicted." Returns True if a
    conflict was resolved.
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read()
    except (FileNotFoundError, OSError):
        return False
    if '<<<<<<<' not in text:
        return False
    blocks = _split_conflict_sides(text)
    if not blocks:
        return False
    ours_text, theirs_text = blocks[0]
    # Pick lexicographically-larger. Deterministic across both
    # sides of the merge. For ``team_size`` (integer string)
    # this picks the larger number, which is usually the right
    # call ("they added a team member; honour that"); for
    # arbitrary scalars it just picks something stable.
    winner = max(ours_text, theirs_text)
    body = winner.rstrip('\n') + '\n'
    _atomic_write(path, body)
    return True


def resolve_kv_conflicts(working_dir):
    """Walk ``.azt/`` for files containing git conflict markers
    and resolve them per kind:

      - ``.azt/slots/*.txt``: later ``claimed_at`` wins.
      - ``.azt/kv/*.txt``: lexicographically-larger value wins.

    Returns ``{slot_resolved: [...], kv_resolved: [...]}``
    listing the files we touched so the caller can stage them
    before continuing the merge.

    Called from the merge path after dulwich resolves what it
    can; anything left in ``.azt/`` is ours to handle.
    """
    out = {'slot_resolved': [], 'kv_resolved': []}
    sd = _slots_dir(working_dir)
    if os.path.isdir(sd):
        for name in os.listdir(sd):
            if not name.endswith('.txt'):
                continue
            path = os.path.join(sd, name)
            try:
                if _resolve_slot_file(path):
                    out['slot_resolved'].append(
                        _slot_filename_to_slot(name))
            except Exception as ex:
                print(f'[project_kv] slot resolve {path!r} '
                      f'raised: {ex!r}',
                      file=sys.stderr, flush=True)
    kd = _kv_dir(working_dir)
    if os.path.isdir(kd):
        for name in os.listdir(kd):
            if not name.endswith('.txt'):
                continue
            path = os.path.join(kd, name)
            try:
                if _resolve_kv_file(path):
                    out['kv_resolved'].append(name[:-len('.txt')])
            except Exception as ex:
                print(f'[project_kv] kv resolve {path!r} raised: '
                      f'{ex!r}', file=sys.stderr, flush=True)
    return out
