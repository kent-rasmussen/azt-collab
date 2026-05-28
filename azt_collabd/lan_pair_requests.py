"""
In-flight outbound pair-requests (sender side).

When the user taps "Pair…" in the Nearby-unpaired list, the
daemon POSTs to the discovered peer's listener
``/v1/lan/pair_request`` and stashes an in-memory record of
the outbound request keyed by ``peer_id``. The receiver's
accept / decline reaches us via a listener callback
(``/v1/lan/pair_response``) which flips the in-memory state.
The peer UI polls ``status_for(peer_id)`` to drive its spinner
/ toast.

Why in-memory and not ``$AZT_HOME/lan_outbound.json``: an
outbound pair request is a transient user gesture. Surviving
a daemon restart isn't required — if the daemon restarts
mid-request, the receiver may still accept and the hello-back
will land normally (recording the pair); the sender just
loses the spinner. Acceptable since the persisted pair is
the durable outcome.

5-minute timeout. The reaper sweep runs lazily on every
``status_for`` / ``list_outbound`` / ``status_for_each`` call —
no background thread needed since this state is touched only
from the request paths.

The TIMEOUT / ACCEPTED / DECLINED statuses surfaced here are
**one-shot**: ``status_for`` reads-and-clears them so the peer
UI sees each terminal state exactly once and can dismiss its
spinner. ``pending`` does not clear (peer keeps polling).
"""

from __future__ import annotations

import threading
import time


_LOCK = threading.Lock()
# peer_id → {'state': str, 'sent_at': float, 'langcode': str,
#            'device_name': str}
# state ∈ {'pending', 'accepted', 'declined', 'timeout'}
_OUTBOUND = {}

# 5-minute cap (from CLIENT_INTEGRATION.md § 20a).
TIMEOUT_S = 300.0


def _reap_locked(now):
    """Mark stale 'pending' entries as 'timeout'. Called under
    _LOCK by every public function before reading the dict."""
    for peer_id, entry in _OUTBOUND.items():
        if entry.get('state') != 'pending':
            continue
        if now - float(entry.get('sent_at', 0)) >= TIMEOUT_S:
            entry['state'] = 'timeout'


def record_sent(peer_id, device_name='', langcode=''):
    """Record that we just POSTed a pair-request to *peer_id*.
    Replaces any prior outbound state for this peer."""
    with _LOCK:
        _OUTBOUND[str(peer_id)] = {
            'state': 'pending',
            'sent_at': time.time(),
            'device_name': str(device_name or ''),
            'langcode': str(langcode or ''),
        }


def record_response(peer_id, accept):
    """Update an outbound request after the receiver responded.
    Idempotent — if the request already timed out, the late
    response is recorded over the timeout (the pair did happen
    on their side; we'd just stopped watching)."""
    with _LOCK:
        entry = _OUTBOUND.get(str(peer_id))
        if entry is None:
            # No prior record — receiver responded to a request
            # we don't remember (daemon restart). Record so a
            # subsequent status_for call surfaces ACCEPTED to
            # the UI if it's still watching.
            entry = {'sent_at': time.time(),
                     'device_name': '', 'langcode': ''}
            _OUTBOUND[str(peer_id)] = entry
        entry['state'] = 'accepted' if accept else 'declined'


def status_for(peer_id):
    """One-shot read: returns ``{state, device_name, langcode}``.
    Terminal states (accepted/declined/timeout) are cleared on
    read so the UI sees each transition exactly once. Pending
    state does not clear (peer keeps polling). Returns None if
    no outbound request exists for this peer."""
    with _LOCK:
        _reap_locked(time.time())
        entry = _OUTBOUND.get(str(peer_id))
        if entry is None:
            return None
        out = {
            'state': entry.get('state', 'pending'),
            'device_name': entry.get('device_name', ''),
            'langcode': entry.get('langcode', ''),
        }
        if out['state'] != 'pending':
            _OUTBOUND.pop(str(peer_id), None)
        return out


def list_outbound():
    """Snapshot for diagnostics / UI roster refresh. Reaps
    timeouts in-place before returning. Does NOT clear terminal
    states (those still surface via status_for)."""
    with _LOCK:
        _reap_locked(time.time())
        return {
            peer_id: {
                'state': entry.get('state', 'pending'),
                'sent_at': entry.get('sent_at', 0),
                'device_name': entry.get('device_name', ''),
                'langcode': entry.get('langcode', ''),
            }
            for peer_id, entry in _OUTBOUND.items()
        }


def forget(peer_id):
    """Drop any outbound record for *peer_id*. Used by the UI
    when the user gives up on a spinner without waiting for a
    terminal state."""
    with _LOCK:
        _OUTBOUND.pop(str(peer_id), None)
