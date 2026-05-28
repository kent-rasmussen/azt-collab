"""
Shared decisions watcher — single Kivy popup surface for every
peer.

Polls ``lan_pending()`` on a configurable cadence; for each
unresolved decision, renders a modal popup matching the kind.
Accept / Decline / (per-kind extras like the 3-way Internet-URL
conflict resolution) dispatch to the existing per-kind RPCs and
fire ``on_resolved(kind, action, decision)`` for the host peer
to refresh its own state.

This module is the **only** place pending-decisions popups
should be rendered. Peers don't poll ``lan_pending`` themselves
and don't reimplement these popups — see
``CLIENT_INTEGRATION.md`` § 20a.

Public surface (re-exported via ``azt_collab_client.ui``):

  install_decision_watcher(poll_interval_s=1.0, on_resolved=None)

Idempotent — calling twice replaces the previous installation's
interval / callback without spawning a second poll loop.
"""

from __future__ import annotations

import sys

from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView

from . import theme
from ..translate import tr as _tr


# Decision kind constants — mirror of ``azt_collabd.pending_decisions``
# string values. Kept here so peer code doesn't have to import the
# server package (hard rule #3).
KIND_SHARE_OFFER = 'share_offer'
KIND_PAIR_REQUEST = 'pair_request'
KIND_ADOPT_ORIGIN = 'adopt_origin'
KIND_REMOTE_CONFLICT = 'remote_conflict'


# Module-level singleton state. The Clock-scheduled callable closes
# over these.
_STATE = {
    'installed': False,
    'poll_interval_s': 1.0,
    'on_resolved': None,
    'event': None,        # the Clock event handle
    # ID of decision currently showing a popup. Prevents stacking
    # a second popup over the first while the user is mid-tap.
    'showing_id': '',
}


def install_decision_watcher(poll_interval_s=1.0, on_resolved=None):
    """Install the background decision watcher.

    *poll_interval_s* is how often the watcher fetches
    ``lan_pending`` from the daemon. 1.0 s is the recommended
    default — near-instant popup on same-LAN gestures without
    flogging the daemon. Don't go below 0.5 s; don't go above
    5 s.

    *on_resolved* is an optional callable
    ``(kind: str, action: str, decision: dict) -> None`` invoked
    after a decision is resolved (either via popup or remotely).
    Peers use it to refresh project lists, peer rosters, etc. The
    callback is invoked on the Kivy main thread; do NOT block.

    Idempotent — a second call replaces the previous interval /
    callback without spawning a second poll loop. Safe to call
    every ``on_start``.
    """
    from kivy.clock import Clock
    _STATE['poll_interval_s'] = float(
        max(0.5, min(5.0, poll_interval_s)))
    _STATE['on_resolved'] = on_resolved
    if _STATE['installed']:
        # Re-schedule with the new interval.
        if _STATE['event'] is not None:
            try:
                _STATE['event'].cancel()
            except Exception:
                pass
        _STATE['event'] = Clock.schedule_interval(
            _poll_once, _STATE['poll_interval_s'])
        return
    _STATE['installed'] = True
    _STATE['event'] = Clock.schedule_interval(
        _poll_once, _STATE['poll_interval_s'])
    # Fire once immediately so a decision that landed before
    # install_decision_watcher was called doesn't wait a full
    # interval to surface.
    Clock.schedule_once(lambda _dt: _poll_once(0), 0)


def _poll_once(_dt):
    """Single poll tick. Fetches pending decisions; if any are
    unresolved and no popup is currently showing, renders the
    first one. Subsequent decisions surface on subsequent ticks
    once the popup closes (``showing_id`` clears on dismiss)."""
    if _STATE['showing_id']:
        return  # popup already up; wait for it to resolve
    from .. import lan_pending
    try:
        decisions = lan_pending()
    except Exception as ex:
        print(f'[decisions] lan_pending raised: {ex!r}',
              file=sys.stderr, flush=True)
        return
    if not decisions:
        return
    # Render the oldest first — preserves arrival order so a
    # share-offer doesn't get queue-jumped by a later
    # adopt-origin from a different peer.
    decisions = sorted(
        decisions, key=lambda d: d.get('created_at', '') or '')
    for d in decisions:
        if _render_decision(d):
            break  # one popup at a time


def _render_decision(decision):
    """Dispatch on ``kind``. Returns True if a popup was opened
    (caller stops iterating); False if the decision was skipped
    (unknown kind logged, nothing rendered)."""
    kind = decision.get('kind', '')
    if kind == KIND_SHARE_OFFER:
        _open_share_offer_popup(decision)
        return True
    if kind == KIND_PAIR_REQUEST:
        _open_pair_request_popup(decision)
        return True
    if kind == KIND_ADOPT_ORIGIN:
        _open_adopt_origin_popup(decision)
        return True
    if kind == KIND_REMOTE_CONFLICT:
        _open_remote_conflict_popup(decision)
        return True
    print(f'[decisions] unknown kind {kind!r}; skipping',
          file=sys.stderr, flush=True)
    return False


# ── Shared popup chrome ────────────────────────────────────────────────────


def _wrap_label(text, font_size=12, bold=False, color=None,
                halign='left'):
    """Build a wrap-friendly Label. Long device_names / langcodes /
    URLs render across multiple lines instead of clipping. Height
    auto-sizes to content via the ``size`` → ``text_size``
    binding."""
    label = Label(
        text=text, font_size=sp(font_size), bold=bold,
        color=color or theme.TEXT,
        halign=halign, valign='top',
        size_hint_y=None,
    )

    def _resize(_w, size):
        # text_size width = label width; height = None lets the
        # text wrap naturally. Then we read texture_size.y to set
        # the label's actual height.
        label.text_size = (size[0], None)

    def _set_height(_w, texture_size):
        label.height = max(dp(20), texture_size[1] + dp(4))

    label.bind(size=_resize, texture_size=_set_height)
    return label


def _make_popup(title, body_widgets, button_widgets,
                size_hint=(0.92, None), min_height_dp=240):
    """Build a popup with a wrap-friendly body and a row of
    buttons at the bottom. Body widgets scroll if they exceed
    available space; button row stays fixed."""
    container = BoxLayout(orientation='vertical', spacing=dp(10),
                          padding=dp(12))

    body_box = BoxLayout(orientation='vertical', spacing=dp(8),
                         size_hint_y=None)
    body_box.bind(minimum_height=body_box.setter('height'))
    for w in body_widgets:
        body_box.add_widget(w)

    scroll = ScrollView(size_hint_y=1, do_scroll_x=False)
    scroll.add_widget(body_box)
    container.add_widget(scroll)

    btn_row = BoxLayout(orientation='horizontal',
                        size_hint_y=None, height=dp(52),
                        spacing=dp(8))
    for b in button_widgets:
        btn_row.add_widget(b)
    container.add_widget(btn_row)

    popup = Popup(
        title=title, content=container,
        size_hint=size_hint,
        height=dp(min_height_dp) if size_hint[1] is None else 0,
        auto_dismiss=False)
    return popup


def _fire_on_resolved(kind, action, decision):
    cb = _STATE.get('on_resolved')
    if cb is None:
        return
    try:
        cb(kind, action, decision)
    except Exception as ex:
        print(f'[decisions] on_resolved raised: {ex!r}',
              file=sys.stderr, flush=True)


def _on_popup_dismiss():
    """Common dismiss cleanup. Frees the ``showing_id`` slot so
    the next poll tick can surface the next decision."""
    _STATE['showing_id'] = ''


# ── KIND_SHARE_OFFER ───────────────────────────────────────────────────────


def _open_share_offer_popup(decision):
    """{device_name} wants to share project {langcode}. Accept /
    Decline. Accept fires a passive clone (no last_project
    hijack)."""
    from .. import lan_accept_offer, lan_decline_offer, S
    params = decision.get('params') or {}
    device_name = str(params.get('device_name') or
                      _tr('Unnamed device'))
    langcode = str(params.get('langcode', '') or '')

    body = [
        _wrap_label(
            _tr('Project share offer'),
            font_size=15, bold=True,
            color=theme.ACCENT, halign='center'),
        _wrap_label(
            _tr('{device_name} wants to share project '
                "'{langcode}' with you.").format(
                    device_name=device_name, langcode=langcode),
            font_size=13, bold=True),
        _wrap_label(
            _tr('Accept to download the project from this device. '
                "It will appear in your project list, but won't "
                'replace your current project.'),
            font_size=11, color=theme.TEXT_DIM),
    ]

    decline_btn = Button(
        text=_tr('Decline'), font_size=sp(13))
    accept_btn = Button(
        text=_tr('Accept'), font_size=sp(13),
        background_color=theme.ACCENT)

    popup = _make_popup(
        title=_tr('Receive a project'),
        body_widgets=body,
        button_widgets=[decline_btn, accept_btn],
        min_height_dp=280)

    def _accept(*_):
        # Passive accept — the daemon-side accept_offer must pass
        # user_initiated=False through to lan_clone so the new
        # project doesn't hijack last_project. Until the daemon
        # change lands, peer hosts honour the rule via the
        # on_resolved callback (don't auto-load the new project).
        result = lan_accept_offer(decision['id'])
        # In-flow adopt-origin confirm: if the clone stashed a
        # KIND_ADOPT_ORIGIN, the next poll tick will surface it
        # via _open_adopt_origin_popup. We don't chain here —
        # keeping each popup independent.
        popup.dismiss()
        _on_popup_dismiss()
        _fire_on_resolved(KIND_SHARE_OFFER, 'accept', decision)

    def _decline(*_):
        lan_decline_offer(decision['id'])
        popup.dismiss()
        _on_popup_dismiss()
        _fire_on_resolved(KIND_SHARE_OFFER, 'decline', decision)

    accept_btn.bind(on_release=_accept)
    decline_btn.bind(on_release=_decline)

    _STATE['showing_id'] = decision.get('id', '')
    popup.open()


# ── KIND_PAIR_REQUEST ──────────────────────────────────────────────────────


def _open_pair_request_popup(decision):
    """{device_name} wants to pair with this device. Accept /
    Decline. Accept records the pair + sends hello-back (auto-
    share the sender's pair-context project IFF langcodes match
    and histories are related)."""
    from .. import lan_pair_request_resolve
    params = decision.get('params') or {}
    device_name = str(params.get('device_name') or
                      _tr('Unnamed device'))
    langcode = str(params.get('langcode', '') or '')

    body = [
        _wrap_label(
            _tr('Pair request'),
            font_size=15, bold=True,
            color=theme.ACCENT, halign='center'),
        _wrap_label(
            _tr('{device_name} wants to pair with this device.')
                .format(device_name=device_name),
            font_size=13, bold=True),
    ]
    if langcode:
        body.append(_wrap_label(
            _tr("They're working on project '{langcode}'. If you "
                'accept and you also have this project, it will be '
                'shared automatically.').format(langcode=langcode),
            font_size=11, color=theme.TEXT_DIM))
    else:
        body.append(_wrap_label(
            _tr('Accept to add this device to your paired list. '
                'You can share projects with them after pairing.'),
            font_size=11, color=theme.TEXT_DIM))

    decline_btn = Button(
        text=_tr('Decline'), font_size=sp(13))
    accept_btn = Button(
        text=_tr('Accept'), font_size=sp(13),
        background_color=theme.ACCENT)

    popup = _make_popup(
        title=_tr('Pair request'),
        body_widgets=body,
        button_widgets=[decline_btn, accept_btn],
        min_height_dp=280)

    def _resolve(accept):
        lan_pair_request_resolve(decision['id'], accept)
        popup.dismiss()
        _on_popup_dismiss()
        _fire_on_resolved(KIND_PAIR_REQUEST,
                          'accept' if accept else 'decline',
                          decision)

    accept_btn.bind(on_release=lambda *_: _resolve(True))
    decline_btn.bind(on_release=lambda *_: _resolve(False))

    _STATE['showing_id'] = decision.get('id', '')
    popup.open()


# ── KIND_ADOPT_ORIGIN ──────────────────────────────────────────────────────


def _open_adopt_origin_popup(decision):
    """{device_name} pushes project to {url} on the Internet. Use
    it from this device too?"""
    from .. import lan_adopt_origin
    params = decision.get('params') or {}
    device_name = str(params.get('device_name') or
                      _tr('A paired device'))
    langcode = str(params.get('langcode', '') or '')
    url = str(params.get('url', '') or '')

    body = [
        _wrap_label(
            _tr('Back up to the Internet?'),
            font_size=15, bold=True,
            color=theme.ACCENT, halign='center'),
        _wrap_label(
            _tr("{device_name} pushes project '{langcode}' to:")
                .format(device_name=device_name, langcode=langcode),
            font_size=13),
        _wrap_label(url, font_size=11, color=theme.TEXT_DIM),
        _wrap_label(
            _tr('Using the same Internet location from this device '
                'means future commits go to the Internet too, not '
                'just over the local network.'),
            font_size=11, color=theme.TEXT_DIM),
    ]

    decline_btn = Button(
        text=_tr('No, local only'), font_size=sp(13))
    accept_btn = Button(
        text=_tr('Yes, use it'), font_size=sp(13),
        background_color=theme.ACCENT)

    popup = _make_popup(
        title=_tr('Back up to the Internet?'),
        body_widgets=body,
        button_widgets=[decline_btn, accept_btn],
        min_height_dp=320)

    def _resolve(accept):
        lan_adopt_origin(decision['id'], accept)
        popup.dismiss()
        _on_popup_dismiss()
        _fire_on_resolved(KIND_ADOPT_ORIGIN,
                          'accept' if accept else 'decline',
                          decision)

    accept_btn.bind(on_release=lambda *_: _resolve(True))
    decline_btn.bind(on_release=lambda *_: _resolve(False))

    _STATE['showing_id'] = decision.get('id', '')
    popup.open()


# ── KIND_REMOTE_CONFLICT (three-way) ───────────────────────────────────────


def _open_remote_conflict_popup(decision):
    """Two different Internet locations for the same project.
    User picks: keep existing, switch to incoming, or use both.
    Daemon's resolve_conflict supports all three (use_theirs /
    keep_mine / dual_publish)."""
    from .. import lan_resolve_conflict
    params = decision.get('params') or {}
    device_name = str(params.get('device_name') or
                      _tr('A paired device'))
    langcode = str(params.get('langcode', '') or '')
    existing_url = str(params.get('existing_url', '') or '')
    incoming_url = str(params.get('incoming_url', '') or '')

    body = [
        _wrap_label(
            _tr('Two Internet locations for the same project'),
            font_size=15, bold=True,
            color=theme.ACCENT, halign='center'),
        _wrap_label(
            _tr("Project '{langcode}' is set up to push to two "
                'different places. Pick which one(s) this device '
                'should use.').format(langcode=langcode),
            font_size=12),
        _wrap_label(
            _tr('Your current setting:'),
            font_size=11, color=theme.TEXT_DIM),
        _wrap_label(existing_url, font_size=11, bold=True),
        _wrap_label(
            _tr('{device_name} uses:').format(
                device_name=device_name),
            font_size=11, color=theme.TEXT_DIM),
        _wrap_label(incoming_url, font_size=11, bold=True),
    ]

    keep_btn = Button(
        text=_tr('Keep mine'), font_size=sp(12))
    both_btn = Button(
        text=_tr('Use both'), font_size=sp(12),
        background_color=theme.ACCENT)
    switch_btn = Button(
        text=_tr('Switch to theirs'), font_size=sp(12))

    popup = _make_popup(
        title=_tr('Resolve Internet location'),
        body_widgets=body,
        button_widgets=[keep_btn, both_btn, switch_btn],
        min_height_dp=380)

    def _resolve(mode, action):
        lan_resolve_conflict(decision['id'], mode)
        popup.dismiss()
        _on_popup_dismiss()
        _fire_on_resolved(KIND_REMOTE_CONFLICT, action, decision)

    keep_btn.bind(on_release=lambda *_: _resolve(
        'keep_mine', 'keep_mine'))
    both_btn.bind(on_release=lambda *_: _resolve(
        'dual_publish', 'both'))
    switch_btn.bind(on_release=lambda *_: _resolve(
        'use_theirs', 'use_theirs'))

    _STATE['showing_id'] = decision.get('id', '')
    popup.open()
