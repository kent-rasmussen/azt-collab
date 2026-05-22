"""
User-facing pending decisions for the LAN sync transport.

Three flows stash decisions here:

  - **share_offer** — a paired peer pushed an offer to share a
    project with us; we haven't accepted or declined yet.
    Params: ``peer_id``, ``device_name``, ``langcode``, ``repo_url``.
  - **adopt_origin** — the owner of a project we're about to clone
    (or just cloned) has a ``remote_url`` we don't; user must
    confirm before we register it as ``origin``.
    Params: ``peer_id``, ``device_name``, ``langcode``, ``url``.
  - **remote_conflict** — two paired peers have *different*
    ``remote_url``\\ s for the same shared project (fork case).
    User picks: use theirs / keep mine / dual-publish.
    Params: ``peer_id``, ``device_name``, ``langcode``,
    ``existing_url``, ``incoming_url``.

State at ``$AZT_HOME/pending_decisions.json``. Atomic read/write
per the standard daemon obligation. Decisions are addressed by a
stable ``id`` so the UI can resolve them without races.

These aren't ``$AZT_HOME/peers.json`` fields because they're
per-(peer, langcode, intent) tuples that come and go on a
different cadence from the underlying peer relationship — a peer
can be paired indefinitely while their share-offer for a single
project is pending for an hour and then accepted. Keeping them
separate keeps peers.json focused on the long-lived bits.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import uuid

from . import paths as _paths


_LOCK = threading.Lock()


# Decision kinds. Exported for switch/dispatch on the receiving
# side without stringly-typed land mines.
KIND_SHARE_OFFER = 'share_offer'
KIND_ADOPT_ORIGIN = 'adopt_origin'
KIND_REMOTE_CONFLICT = 'remote_conflict'


def _path():
    return os.path.join(_paths.azt_home(), 'pending_decisions.json')


def _atomic_write(target_path, data):
    target_dir = os.path.dirname(target_path) or '.'
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.pending.', suffix='.tmp',
                               dir=target_dir)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.replace(tmp, target_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_raw():
    try:
        with open(_path(), 'rb') as f:
            data = json.loads(f.read().decode('utf-8'))
    except FileNotFoundError:
        return {'decisions': []}
    except (OSError, ValueError) as ex:
        print(f'[pending] load failed: {ex!r}',
              file=sys.stderr, flush=True)
        return {'decisions': []}
    if not isinstance(data, dict) \
            or not isinstance(data.get('decisions'), list):
        return {'decisions': []}
    return data


def _save_raw(data):
    payload = json.dumps(data, indent=2, sort_keys=True).encode('utf-8')
    _atomic_write(_path(), payload)


def _now_iso():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _stable_id(kind, params):
    """Identity that collapses duplicates so a peer re-sending the
    same offer doesn't pile up entries. (kind, peer_id, langcode)
    is the dedup key; the value of ``url`` etc. can change between
    instances without making them distinct decisions."""
    peer_id = str(params.get('peer_id', '') or '')
    langcode = str(params.get('langcode', '') or '')
    return f'{kind}:{peer_id}:{langcode}'


def add(kind, params):
    """Insert or update a decision. Returns the canonical entry
    (with ``id``, ``created_at``, ``kind``, ``params``)."""
    decision_id = _stable_id(kind, params)
    with _LOCK:
        data = _load_raw()
        decisions = [d for d in data.get('decisions', [])
                     if isinstance(d, dict)
                     and d.get('id') != decision_id]
        entry = {
            'id': decision_id,
            'kind': str(kind),
            'params': dict(params or {}),
            'created_at': _now_iso(),
        }
        decisions.append(entry)
        data['decisions'] = decisions
        _save_raw(data)
    return entry


def remove(decision_id):
    """Delete a decision by id. Returns True if it existed."""
    with _LOCK:
        data = _load_raw()
        before = data.get('decisions', [])
        after = [d for d in before if isinstance(d, dict)
                 and d.get('id') != decision_id]
        if len(after) == len(before):
            return False
        data['decisions'] = after
        _save_raw(data)
    return True


def list_all():
    """Return every pending decision as a list of canonical
    entries. Empty list if none."""
    with _LOCK:
        data = _load_raw()
    out = []
    for d in (data.get('decisions') or []):
        if not isinstance(d, dict):
            continue
        out.append({
            'id': str(d.get('id', '') or ''),
            'kind': str(d.get('kind', '') or ''),
            'params': dict(d.get('params') or {}),
            'created_at': str(d.get('created_at', '') or ''),
        })
    return out


def get(decision_id):
    """Return one decision or None."""
    for d in list_all():
        if d['id'] == decision_id:
            return d
    return None


def count_by_kind(kind=None):
    """Diagnostic / badge helper. With ``kind=None`` returns the
    total count; otherwise returns count of that kind."""
    decisions = list_all()
    if kind is None:
        return len(decisions)
    return sum(1 for d in decisions if d.get('kind') == kind)


def list_by_kind(kind):
    """Return only decisions of the given kind."""
    return [d for d in list_all() if d.get('kind') == kind]


def has_for(kind, peer_id, langcode):
    """Convenience for "is there already a pending X for this peer
    + langcode"? Used by routers to avoid double-stash on retry."""
    return any(d['id'] == _stable_id(kind, {
        'peer_id': peer_id, 'langcode': langcode,
    }) for d in list_all())
