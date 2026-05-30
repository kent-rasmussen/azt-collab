"""
Kivy popups for the LAN sync transport (combined pair-share-clone
flow, 0.45.0).

Public entry points:

  - ``share_project_popup(langcode)`` — settings-side "Share
    {langcode} project" popup. One panel with three sections:
    (a) paired phones list with one-tap Share buttons, (b) "Show
    QR code" sub-popup for in-person pairing-with-clone of a new
    phone, (c) "Add permission by github username" section that
    folds in the existing grant-collaborator flow.
  - ``scan_to_pair(on_done)`` — picker-side QR scanner driver.
    Decodes the payload, dispatches on payload shape (plain URL,
    pair-only, pair+clone, share-only URL), runs the matching
    sequence end-to-end (pair → clone → propose-origin).
  - ``pending_offers_popup(on_done)`` — picker-side "Receive a
    project from another phone" entry. Shows pending share offers
    from already-paired peers with Accept / Decline buttons, plus
    a "Scan QR code" fallthrough for new-phone first-pair.
  - ``adopt_origin_popup(decision, on_done)`` — always-confirm
    prompt before setting ``origin`` on a project. Reused for
    both QR-scan-time and settings-side resolution surfaces.
  - ``paired_phones_popup`` — settings-side list of paired peers
    with per-peer manage actions (share / unshare projects, set
    static endpoint, unpair).

All entry points live in the shared client package because both
the daemon settings UI (``azt_collabd.ui.app``) and the picker
(peer-side picker hosts) consume them. Translations route through
``azt_collab_client.translate.tr``.
"""

from __future__ import annotations

import io
import json
import sys
import threading

from kivy.clock import Clock
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from .themed_popup import ThemedButton as Button
from kivy.uix.image import Image
from kivy.uix.label import Label
from .themed_popup import ThemedPopup as Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput

from . import theme
from ..translate import tr as _tr


def _show_lan_failure_popup(title, message, font_name='Roboto'):
    """One-button info popup used to surface clone failures (timeout,
    peer unreachable) that previously vanished into a silent
    fall-through in ``_finish_on_main``. Single OK button dismisses;
    the user lands back on the picker so they can retry or pick a
    different path."""
    body = BoxLayout(orientation='vertical', spacing=dp(12),
                     padding=dp(16))
    body.add_widget(Label(
        text=message, font_size=sp(13), font_name=font_name,
        halign='center', valign='top',
        text_size=(dp(280), None)))
    ok_btn = Button(text=_tr('OK'), size_hint_y=None,
                    height=dp(44), font_size=sp(13),
                    font_name=font_name)
    body.add_widget(ok_btn)
    popup = Popup(title=title, content=body,
                  size_hint=(0.9, 0.5), auto_dismiss=False)
    ok_btn.bind(on_release=lambda *_: popup.dismiss())
    popup.open()
    return popup


def _auto_enable_lan(font_name='Roboto'):
    """Turn the daemon-wide LAN toggle on AND make sure the
    listener is actually bound before returning, so the QR
    payload picks up the real endpoint and the post-pair hello
    has somewhere to call back to. Returns ``True`` if we
    flipped the *persisted* toggle from off to on (caller should
    fire ``_offer_disable_when_done`` later); ``False`` if the
    persisted toggle was already on.

    Checking ``lan_toggle().get('on')`` alone isn't enough: the
    persisted bit can be ``True`` while the in-memory listener
    state is empty (daemon restarted since the toggle was last
    set — config.json carried ``lan.allow_sync=true`` forward,
    but the listener thread didn't survive the process kill).
    We force a ``lan_set_toggle(True)`` round-trip whenever the
    endpoint is empty so ``apply_toggle`` re-starts the listener
    and binds a real port; without that, ``lan_pair_qr`` builds
    a QR with ``endpoint=''`` and the receiving phone has no
    place to push back to."""
    from .. import lan_toggle, lan_set_toggle
    state = lan_toggle()
    was_off = not state.get('on')
    if not was_off and state.get('endpoint'):
        return False  # already on AND bound
    # Either off, or on-but-not-bound. Re-apply.
    lan_set_toggle(True)
    return was_off


def _offer_disable_when_done(was_off_before, font_name='Roboto'):
    """Post-exchange follow-up: if we auto-enabled the LAN toggle
    for this exchange, offer the user the chance to turn it back
    off now that the handshake / clone is done. Default = keep on
    (the underlying flow paired with someone, so leaving it on
    lets future commits LAN-fan-out). Non-modal: dismissing the
    popup leaves the toggle on."""
    if not was_off_before:
        return
    from .. import lan_set_toggle

    content = BoxLayout(orientation='vertical', spacing=dp(10),
                        padding=dp(12))
    content.add_widget(Label(
        text=_tr('Local-network sharing is on.'),
        size_hint_y=None, height=dp(28),
        font_size=sp(14), bold=True, halign='center',
        valign='middle', font_name=font_name,
        text_size=(dp(320), dp(28))))
    content.add_widget(Label(
        text=_tr('Leave it on so future commits can sync over the '
                 'local network too, or turn it off if you only '
                 'needed it for this exchange.'),
        font_size=sp(11), color=theme.TEXT_DIM,
        halign='center', valign='top', font_name=font_name,
        text_size=(dp(340), None)))

    btn_row = BoxLayout(orientation='horizontal',
                        size_hint_y=None, height=dp(48),
                        spacing=dp(8))
    off_btn = Button(
        text=_tr('Turn off'),
        font_size=sp(13), font_name=font_name)
    keep_btn = Button(
        text=_tr('Keep on'),
        font_size=sp(13), font_name=font_name,
        background_color=theme.ACCENT)
    btn_row.add_widget(off_btn)
    btn_row.add_widget(keep_btn)
    content.add_widget(btn_row)

    popup = Popup(
        title=_tr('Keep local-network sharing on?'),
        content=content,
        size_hint=(0.9, None), height=dp(240),
        auto_dismiss=True)

    def _off(*_):
        lan_set_toggle(False)
        popup.dismiss()

    off_btn.bind(on_release=_off)
    keep_btn.bind(on_release=lambda *_: popup.dismiss())
    popup.open()


def _resolve_adopt_origin_then_done(result, on_done, font_name):
    """Helper: open the adopt-origin popup for the first matching
    pending decision, then call ``on_done(result)`` once the user
    has responded (regardless of yes/no). Used by both
    ``scan_to_pair`` and ``pending_offers_popup.accept`` so the
    inline confirm fires in both entry paths."""
    from .. import lan_pending

    decisions = [d for d in lan_pending()
                 if d.get('kind') == 'adopt_origin']
    if not decisions:
        if on_done is not None:
            on_done(result)
        return
    decision = decisions[0]

    def _after_adopt(_adopt_result):
        if on_done is not None:
            on_done(result)

    adopt_origin_popup(decision, on_done=_after_adopt,
                       font_name=font_name)


def _render_qr_widget(payload, scale=8, border=2):
    """Render ``payload`` (a dict — typically the daemon's pairing
    QR payload) as a Kivy ``Image`` widget. ``segno`` produces PNG
    bytes; ``CoreImage`` wraps them into a texture the Image widget
    can show. Returns ``(widget, error_message)`` — ``widget`` is
    ``None`` on failure and the message is what to display
    instead."""
    try:
        import segno
    except ImportError as ex:
        return None, _tr('QR generator not available: ') + str(ex)
    try:
        text = json.dumps(payload, separators=(',', ':'))
        qr = segno.make(text, error='M')
        buf = io.BytesIO()
        qr.save(buf, kind='png', scale=scale, border=border)
        buf.seek(0)
        from kivy.core.image import Image as CoreImage
        core = CoreImage(buf, ext='png')
        img = Image(texture=core.texture, allow_stretch=True,
                    keep_ratio=True)
        return img, ''
    except Exception as ex:
        print(f'[lan-popups] QR render failed: {ex!r}',
              file=sys.stderr, flush=True)
        return None, _tr('Could not render QR.')


def share_pairing_qr_popup(font_name='Roboto', langcode=''):
    """Open a popup that shows this daemon's pairing QR. The other
    phone scans it with ``scan_to_pair``. Top of the popup shows the
    device name + peer-id prefix so the user can verbally confirm
    across the table before accepting the pair on the other side.

    When *langcode* is non-empty the QR carries the langcode +
    that project's ``remote_url`` so the same scan does pair +
    share + clone (combined flow, 0.45.0). Empty langcode produces
    a pair-only QR (legacy behavior, kept for the "trust but no
    project yet" case)."""
    from .. import lan_peer_id, lan_toggle

    content = BoxLayout(orientation='vertical', spacing=dp(10),
                        padding=dp(12))

    info = lan_peer_id()
    toggle = lan_toggle()
    if not info.get('peer_id'):
        # Identity path failed. Surface the daemon's diagnostic so
        # the user can see *why* (e.g. cryptography unavailable —
        # build issue) instead of just "not available." Compact
        # popup since there's nothing else to render.
        err = info.get('error', '') or _tr('unknown')
        detail = info.get('detail', '') or ''
        content.add_widget(Label(
            text=_tr('LAN identity is not available on this device.'),
            size_hint_y=None, height=dp(28),
            font_size=sp(13), bold=True, font_name=font_name))
        content.add_widget(Label(
            text=err, size_hint_y=None, height=dp(22),
            font_size=sp(11), color=theme.TEXT_DIM,
            font_name=font_name))
        if detail:
            detail_label = Label(
                text=detail, font_size=sp(10),
                color=theme.TEXT_DIM, halign='left', valign='top',
                font_name=font_name)
            detail_label.bind(
                size=lambda w, s: setattr(w, 'text_size', s))
            content.add_widget(detail_label)
        close_btn = Button(
            text=_tr('Close'), size_hint_y=None, height=dp(44),
            font_size=sp(14), font_name=font_name)
        content.add_widget(close_btn)
        popup = Popup(
            title=_tr('Pair a phone'),
            content=content,
            size_hint=(0.9, None), height=dp(280),
            auto_dismiss=False)
        close_btn.bind(on_release=lambda *_: popup.dismiss())
        popup.open()
        return popup
    device_name = info.get('device_name', '') or _tr('this device')
    content.add_widget(Label(
        text=device_name,
        size_hint_y=None, height=dp(28), font_size=sp(15),
        bold=True, color=theme.ACCENT, font_name=font_name))
    content.add_widget(Label(
        text=_tr('Peer ID: ') + info['peer_id'][:12] + '…',
        size_hint_y=None, height=dp(22), font_size=sp(12),
        color=theme.TEXT_DIM, font_name=font_name))

    if not toggle.get('on'):
        content.add_widget(Label(
            text=_tr('Turn on local-network sharing in sync '
                     'settings so the other phone can connect '
                     'after scanning.'),
            size_hint_y=None, height=dp(48),
            font_size=sp(11), color=theme.TEXT_DIM,
            halign='center', valign='middle',
            font_name=font_name))

    # Build the QR payload by calling lan_pair_qr — the daemon
    # auto-populates the endpoint from the running listener when
    # present. Passing *langcode* makes the payload combined
    # (pair + share + clone) rather than pair-only.
    from .. import lan_pair_qr
    payload = lan_pair_qr(langcode=langcode)
    if payload:
        widget, err = _render_qr_widget(payload, scale=8, border=2)
        if widget is not None:
            content.add_widget(widget)
        else:
            content.add_widget(Label(
                text=err, font_size=sp(12),
                color=theme.TEXT_DIM, font_name=font_name))
        endpoint = payload.get('endpoint', '')
        if endpoint:
            content.add_widget(Label(
                text=_tr('Endpoint: ') + endpoint,
                size_hint_y=None, height=dp(22),
                font_size=sp(11), color=theme.TEXT_DIM,
                font_name=font_name))
    else:
        content.add_widget(Label(
            text=_tr('Could not generate pairing QR.'),
            font_size=sp(12), color=theme.TEXT_DIM,
            font_name=font_name))

    close_btn = Button(
        text=_tr('Close'), size_hint_y=None, height=dp(44),
        font_size=sp(14), font_name=font_name)
    content.add_widget(close_btn)

    popup = Popup(
        title=_tr('Pair a phone'),
        content=content, size_hint=(0.9, 0.9),
        auto_dismiss=False)
    close_btn.bind(on_release=lambda *_: popup.dismiss())
    popup.open()
    return popup


def scan_to_pair(on_done=None, on_status=None, font_name='Roboto'):
    """Picker entry: launch the QR scanner, decode the payload,
    and run the appropriate flow (combined pair-share-clone for a
    pair payload with a langcode, pair-only for a legacy pair-only
    payload).

    On success calls ``on_done(result)`` with a typed ``Result``;
    ``Result.has(S.LAN_PROJECT_CLONED)`` / ``has(S.LAN_PROJECT_REOPENED)``
    means we have a project; ``has(S.LAN_PAIRED)`` only means we
    paired but no project arrived (legacy pair-only QR).

    On unrecognized payloads, falls through to a typed ``SERVER_ERROR``
    so the picker can still show something useful. No-op +
    ``on_status`` toast if QR scanning isn't available."""
    from . import qr_scan
    from .. import lan_pair_accept, lan_clone, S
    from ..status import Result, Status

    def _emit_status(text):
        if on_status is not None:
            try:
                on_status(text)
            except Exception:
                pass

    if not qr_scan.available():
        _emit_status(_tr('QR scanning is not available on this '
                         'device.'))
        if on_done is not None:
            on_done(Result(statuses=[Status(
                'SERVER_ERROR', {'error': 'qr_scan unavailable'})]))
        return

    # Auto-enable LAN sharing so the post-pair LAN-clone has a
    # listener to talk back to. The cert handshake for the clone
    # is bidirectional; both sides need their listener up. Remember
    # whether we flipped so the post-exchange prompt offers to
    # revert.
    auto_enabled = _auto_enable_lan(font_name=font_name)

    def _on_result(text):
        # Two shapes we accept:
        #   - JSON object with our pairing fields → combined flow.
        #   - Plain text (e.g. a github URL someone made into a QR
        #     out-of-band) → fall back to the picker's clone-from-
        #     URL dialog. We don't try to handle that here; just
        #     surface a typed result the caller can dispatch on.
        try:
            payload = json.loads(text)
        except Exception:
            payload = None
        if not isinstance(payload, dict) or 'peer_id' not in payload:
            # Unknown shape — surface so the caller can route to the
            # plain-URL clone path. Stash the raw text on the
            # Status so the dispatcher has something to work with.
            _emit_status(_tr('Scanned data is not valid pairing '
                             'JSON.'))
            if on_done is not None:
                on_done(Result(statuses=[Status(
                    'SERVER_ERROR',
                    {'error': 'unknown_qr_payload',
                     'raw': text[:200]})]))
            return
        peer_id = str(payload.get('peer_id', '') or '')
        langcode = str(payload.get('langcode', '') or '')
        repo_url = str(payload.get('repo_url', '') or '')
        vernlang = str(payload.get('vernlang', '') or '')

        # User-visible progress popup for the synchronous pair +
        # clone RPCs. The previous pending_offers_popup was
        # dismissed before scan_to_pair was called (picker.py:
        # _scan → popup.dismiss → scan_to_pair), so without this
        # the user sees a frozen black screen for the full clone
        # duration (10 s — minutes on a fresh phone). The two
        # halves below must NOT run on the Kivy main thread, or
        # this popup we just mounted won't get drawn either —
        # offload to a worker and marshal back via Clock.
        phase_label = Label(
            text=_tr('Pairing with the other phone…'),
            font_size=sp(14), font_name=font_name,
            halign='center', valign='middle',
            text_size=(dp(280), None))
        sub_label = Label(
            text=_tr('First-time copy over the local network can '
                     'take a minute or two. Please keep both '
                     'phones close together.'),
            font_size=sp(11), color=theme.TEXT_DIM,
            font_name=font_name,
            halign='center', valign='top',
            size_hint_y=None, height=dp(80),
            text_size=(dp(280), dp(80)))
        body = BoxLayout(orientation='vertical', spacing=dp(12),
                         padding=dp(16))
        body.add_widget(phase_label)
        body.add_widget(sub_label)
        progress_popup = Popup(
            title=_tr('Receiving project'),
            content=body, size_hint=(0.9, 0.5),
            auto_dismiss=False)
        progress_popup.open()

        def _set_phase(label_text):
            phase_label.text = label_text

        def _worker():
            # Pair always happens first. Even an unpaired peer who
            # only wanted to clone needs to be paired so the cert
            # handshake for the LAN clone succeeds.
            try:
                w_result = lan_pair_accept(payload)
                # Combined: payload carried a project → fire the
                # LAN clone synchronously here on this worker
                # thread and merge its Result into ours so the
                # caller sees a single Result with both pair +
                # clone statuses. ``vernlang`` is the linguistic
                # code; separate from ``langcode`` (project name /
                # key) since 0.45.0.
                if (langcode and peer_id
                        and w_result.has(S.LAN_PAIRED)):
                    Clock.schedule_once(
                        lambda dt: _set_phase(
                            _tr('Copying project to this phone…')),
                        0)
                    clone_result = lan_clone(peer_id, langcode,
                                             repo_url,
                                             vernlang=vernlang)
                    for status in clone_result.statuses:
                        w_result.statuses.append(status)
            except Exception as ex:
                w_result = Result(statuses=[Status(
                    'SERVER_ERROR',
                    {'error': f'pair/clone raised: {ex!r}'})])
            Clock.schedule_once(
                lambda dt: _finish_on_main(w_result), 0)

        # Adopt-origin always-confirm prompt. If the LAN clone
        # stashed an adopt-origin pending decision, surface it now
        # (in-flow with the scan gesture) rather than deferring to
        # settings — the user just made an explicit pairing
        # gesture, the confirm belongs in the same moment.
        def _final_done(r):
            _offer_disable_when_done(auto_enabled,
                                     font_name=font_name)
            if on_done is not None:
                on_done(r)

        def _finish_on_main(result):
            try:
                progress_popup.dismiss()
            except Exception:
                pass
            # Surface a user-visible toast on the two failure shapes
            # that mean "the clone didn't land" but aren't crashes:
            # stalled transfer (LAN_CLONE_TIMEOUT) or no endpoint
            # reached (LAN_PEER_UNREACHABLE). Only when the result
            # doesn't ALSO carry a success code — a partial result
            # with both reopen + timeout (rare race) should not
            # render a scary timeout toast over a usable project.
            got_project = result.has_any(
                S.LAN_PROJECT_CLONED, S.LAN_PROJECT_REOPENED)
            if not got_project and result.has(S.LAN_CLONE_TIMEOUT):
                _show_lan_failure_popup(
                    _tr('Could not finish receiving the project'),
                    _tr('Copying the project timed out. Is the '
                        'other phone still nearby and on the same '
                        'Wi-Fi? Try the scan again when both '
                        'phones are close together.'),
                    font_name)
            elif (not got_project
                    and result.has(S.LAN_PEER_UNREACHABLE)):
                _show_lan_failure_popup(
                    _tr('Could not reach the other phone'),
                    _tr('The other phone did not respond on this '
                        'network. Check that both phones are on '
                        'the same Wi-Fi and that the sharing '
                        'phone has Local-network sharing turned '
                        'on.'),
                    font_name)
            elif result.has(S.CONTRIBUTOR_UNSET):
                # Daemon refused pair_accept because this device's
                # contributor name isn't set. Same routing as the
                # rest of the suite per CLIENT_INTEGRATION.md
                # § 17 — toast + open_server_ui() — so the user
                # lands directly on the settings page where they
                # can type their name and come back to re-scan.
                _emit_status(_tr(
                    'Set your name on the next screen, then scan '
                    'the QR again.'))
                try:
                    from .. import open_server_ui
                    open_server_ui(on_status=_emit_status)
                except Exception as ex:
                    print(f'[scan_to_pair] open_server_ui raised: '
                          f'{ex!r}', file=sys.stderr, flush=True)
            elif (not got_project
                    and result.has(S.LAN_PAIRED)
                    and not result.has(S.LAN_PROJECT_COLLISION_UNRELATED)):
                # Pair succeeded but no project arrived. Either
                # the QR carried no langcode (pair-only QR, rare
                # — current peer surface only exposes the
                # share-project QR path), or the daemon-side
                # share-allowlist gate refused. Tell the user the
                # likely fix.
                _show_lan_failure_popup(
                    _tr('Paired, but no project came with this QR'),
                    _tr('You\'re now paired with the other phone, '
                        'but the project didn\'t come over. On '
                        'the other phone, open the project you '
                        'want to share, tap "Share project (QR)", '
                        'and scan the new QR it shows.'),
                    font_name)
            elif (not got_project
                    and result.has_any(S.SERVER_ERROR,
                                       S.SERVER_UNAVAILABLE)):
                # pair_accept didn't even reach the daemon's
                # business logic — bad QR payload, transport
                # broke, etc. The error string lives on the
                # SERVER_ERROR status's params; render it so the
                # user (or a maintainer) can see what failed.
                detail = ''
                for s in result.statuses:
                    if s.code in (S.SERVER_ERROR,
                                  S.SERVER_UNAVAILABLE):
                        detail = (s.params or {}).get('error', '')
                        if not detail:
                            detail = (s.params or {}).get(
                                'detail', '')
                        break
                _show_lan_failure_popup(
                    _tr('Pairing failed'),
                    _tr('The pairing call did not succeed: '
                        '{detail}').format(detail=detail or '?'),
                    font_name)
            if result.has(S.LAN_ADOPT_ORIGIN_NEEDED):
                _resolve_adopt_origin_then_done(
                    result, _final_done, font_name)
                return
            _final_done(result)

        threading.Thread(target=_worker, daemon=True,
                         name='lan-scan-pair-clone').start()

    def _on_cancel():
        _emit_status(_tr('Pairing cancelled.'))

    qr_scan.scan_qr(on_result=_on_result, on_cancel=_on_cancel,
                    prompt=_tr('Scan the pairing QR shown on the '
                               'other phone.'))


def _build_peer_row(peer, on_manage, on_unpair, font_name):
    """One row in the paired-peers list. Per-row Unpair surfaced
    directly alongside Manage (0.45.37) so users can clean up
    stale paired-peer entries (e.g., after the peer wiped + re-
    paired and got a new ``peer_id``) without first drilling
    into Manage."""
    row = BoxLayout(orientation='horizontal', size_hint_y=None,
                    height=dp(56), spacing=dp(6), padding=dp(4))
    label_box = BoxLayout(orientation='vertical')
    name = peer.get('device_name') or _tr('Unnamed device')
    label_box.add_widget(Label(
        text=name, halign='left', valign='middle',
        size_hint_y=None, height=dp(28),
        font_size=sp(13), bold=True, font_name=font_name,
        text_size=(dp(190), dp(28))))
    pid = peer.get('peer_id', '')
    shared = ', '.join(peer.get('shared_projects') or []) or \
        _tr('(no projects shared)')
    label_box.add_widget(Label(
        text=f'{pid[:8]}… · {shared}',
        halign='left', valign='middle',
        size_hint_y=None, height=dp(22),
        font_size=sp(10), color=theme.TEXT_DIM,
        text_size=(dp(190), dp(22)),
        font_name=font_name))
    row.add_widget(label_box)
    manage_btn = Button(text=_tr('Manage'), size_hint=(None, None),
                        width=dp(76), height=dp(40),
                        font_size=sp(12), font_name=font_name)
    manage_btn.bind(on_release=lambda *_: on_manage(peer))
    row.add_widget(manage_btn)
    unpair_btn = Button(text=_tr('Unpair'), size_hint=(None, None),
                        width=dp(76), height=dp(40),
                        font_size=sp(12), font_name=font_name)
    unpair_btn.bind(on_release=lambda *_: on_unpair(peer))
    row.add_widget(unpair_btn)
    return row


def _manage_peer_popup(peer, on_refresh, font_name='Roboto'):
    """Per-peer detail popup: list shared projects (with toggles),
    static endpoints (with add/remove), and an unpair button."""
    from .. import (
        list_projects, lan_share_project, lan_unshare_project,
        lan_set_static_endpoints, lan_unpair, S,
    )

    pid = peer.get('peer_id', '')
    content = BoxLayout(orientation='vertical', spacing=dp(8),
                        padding=dp(10))

    content.add_widget(Label(
        text=peer.get('device_name') or _tr('Unnamed device'),
        size_hint_y=None, height=dp(28), font_size=sp(15),
        bold=True, color=theme.ACCENT, font_name=font_name))
    content.add_widget(Label(
        text=_tr('Peer ID: ') + pid,
        size_hint_y=None, height=dp(22), font_size=sp(10),
        color=theme.TEXT_DIM, font_name=font_name))

    # Shared projects toggle list.
    content.add_widget(Label(
        text=_tr('Shared projects'),
        size_hint_y=None, height=dp(24), font_size=sp(12),
        bold=True, font_name=font_name))
    shared_box = BoxLayout(orientation='vertical', size_hint_y=None,
                           spacing=dp(4))
    shared_box.bind(minimum_height=shared_box.setter('height'))
    shared_scroll = ScrollView(size_hint_y=None, height=dp(140))
    shared_scroll.add_widget(shared_box)

    shared = set(peer.get('shared_projects') or [])
    for project in list_projects():
        lang = project.langcode
        row = BoxLayout(orientation='horizontal', size_hint_y=None,
                        height=dp(34), spacing=dp(8))
        row.add_widget(Label(text=lang, halign='left', valign='middle',
                             font_size=sp(12), font_name=font_name,
                             text_size=(dp(140), dp(30))))
        btn = Button(
            text=_tr('Shared') if lang in shared else _tr('Share'),
            size_hint=(None, None), width=dp(110), height=dp(32),
            font_size=sp(12), font_name=font_name,
            background_color=(theme.ACCENT if lang in shared
                              else (0.4, 0.4, 0.4, 1)))

        def _toggle(_btn, lang_=lang):
            if lang_ in shared:
                lan_unshare_project(lang_, pid)
                shared.discard(lang_)
                _btn.text = _tr('Share')
                _btn.background_color = (0.4, 0.4, 0.4, 1)
            else:
                lan_share_project(lang_, pid)
                shared.add(lang_)
                _btn.text = _tr('Shared')
                _btn.background_color = theme.ACCENT

        btn.bind(on_release=_toggle)
        row.add_widget(btn)
        shared_box.add_widget(row)
    content.add_widget(shared_scroll)

    # Static endpoint field — comma-separated 'ip:port' list.
    content.add_widget(Label(
        text=_tr('Manual IP / port (comma-separated)'),
        size_hint_y=None, height=dp(24), font_size=sp(12),
        bold=True, font_name=font_name))
    endpoints_field = TextInput(
        text=', '.join(peer.get('static_endpoints') or []),
        multiline=False, size_hint_y=None, height=dp(40),
        font_size=sp(12), font_name=font_name)
    save_endpoints_btn = Button(
        text=_tr('Save manual IPs'), size_hint_y=None, height=dp(40),
        font_size=sp(12), font_name=font_name)

    def _save_endpoints(*_):
        raw = endpoints_field.text or ''
        eps = [s.strip() for s in raw.split(',') if s.strip()]
        lan_set_static_endpoints(pid, eps)

    save_endpoints_btn.bind(on_release=_save_endpoints)
    content.add_widget(endpoints_field)
    content.add_widget(save_endpoints_btn)

    # Unpair button.
    unpair_btn = Button(
        text=_tr('Forget this device'), size_hint_y=None, height=dp(44),
        font_size=sp(13), font_name=font_name,
        background_color=(0.7, 0.25, 0.25, 1))
    content.add_widget(unpair_btn)
    close_btn = Button(
        text=_tr('Close'), size_hint_y=None, height=dp(40),
        font_size=sp(12), font_name=font_name)
    content.add_widget(close_btn)

    popup = Popup(title=_tr('Manage paired device'),
                  content=content, size_hint=(0.95, 0.95),
                  auto_dismiss=False)

    def _unpair(*_):
        lan_unpair(pid)
        popup.dismiss()
        if on_refresh is not None:
            on_refresh()

    unpair_btn.bind(on_release=_unpair)

    def _close(*_):
        popup.dismiss()
        if on_refresh is not None:
            on_refresh()

    close_btn.bind(on_release=_close)
    popup.open()
    return popup


def paired_phones_popup(font_name='Roboto'):
    """List paired peers; tap any to open the manage-peer popup."""
    from .. import lan_list_peers

    container = BoxLayout(orientation='vertical', spacing=dp(8),
                          padding=dp(10))
    container.add_widget(Label(
        text=_tr('Paired devices'),
        size_hint_y=None, height=dp(28), font_size=sp(15),
        bold=True, color=theme.ACCENT, font_name=font_name))

    list_box = BoxLayout(orientation='vertical', size_hint_y=None,
                         spacing=dp(4))
    list_box.bind(minimum_height=list_box.setter('height'))
    scroll = ScrollView()
    scroll.add_widget(list_box)
    container.add_widget(scroll)

    close_btn = Button(
        text=_tr('Close'), size_hint_y=None, height=dp(44),
        font_size=sp(14), font_name=font_name)
    container.add_widget(close_btn)

    popup = Popup(title=_tr('Paired devices'),
                  content=container, size_hint=(0.95, 0.9),
                  auto_dismiss=False)

    def _confirm_unpair(peer):
        """Confirm dialog before calling ``lan_unpair``.
        Destructive: removes the peer from ``peers.json``, drops
        cached endpoint, drops any shared-project allowlist
        entries for them. Re-pairing requires scanning a fresh
        QR."""
        from .. import lan_unpair  # local import — same shape
        # as ``_manage_peer_popup``, keeps the module-level
        # imports lean.
        pid = peer.get('peer_id', '') or ''
        name = peer.get('device_name') or _tr('Unnamed device')
        body = BoxLayout(orientation='vertical', spacing=dp(8),
                         padding=dp(12))
        body.add_widget(Label(
            text=_tr('Unpair "{name}"?\n\n'
                     'This phone will no longer auto-share with '
                     'that device. Re-pair by scanning a new QR.'
                     ).format(name=name),
            font_size=sp(13), font_name=font_name,
            halign='center', valign='middle',
            text_size=(dp(280), None)))
        btn_row = BoxLayout(orientation='horizontal',
                            spacing=dp(8),
                            size_hint_y=None, height=dp(44))
        cancel_btn = Button(text=_tr('Cancel'), font_name=font_name)
        do_btn = Button(text=_tr('Unpair'), font_name=font_name)
        btn_row.add_widget(cancel_btn)
        btn_row.add_widget(do_btn)
        body.add_widget(btn_row)
        confirm_popup = Popup(
            title=_tr('Confirm unpair'),
            content=body, size_hint=(0.85, None), height=dp(240),
            auto_dismiss=False)

        def _do_it(*_):
            confirm_popup.dismiss()
            try:
                lan_unpair(pid)
            except Exception as ex:
                import sys
                print(f'[lan-unpair] {pid[:8]!r} failed: {ex!r}',
                      file=sys.stderr, flush=True)
            _refresh()

        cancel_btn.bind(on_release=lambda *_: confirm_popup.dismiss())
        do_btn.bind(on_release=_do_it)
        confirm_popup.open()

    def _refresh():
        list_box.clear_widgets()
        peers = lan_list_peers()
        if not peers:
            list_box.add_widget(Label(
                text=_tr('No paired devices. Use "Pair a phone" '
                         'to scan another phone\'s QR.'),
                size_hint_y=None, height=dp(60),
                font_size=sp(12), color=theme.TEXT_DIM,
                halign='center', valign='middle', font_name=font_name,
                text_size=(dp(320), dp(60))))
            return
        for peer in peers:
            list_box.add_widget(_build_peer_row(
                peer,
                lambda p=peer: _manage_peer_popup(
                    p, on_refresh=_refresh,
                    font_name=font_name),
                _confirm_unpair,
                font_name=font_name))

    _refresh()
    close_btn.bind(on_release=lambda *_: popup.dismiss())
    popup.open()
    return popup


def share_project_popup(langcode='', font_name='Roboto'):
    """Settings-side "Share {langcode} project" popup. Three
    sections in one panel:

      1. Share with already-paired phones — one-tap row per peer
         that fires ``lan_share_project`` (which both updates our
         ``shared_projects`` allowlist AND POSTs a courtesy offer
         to the peer's listener).
      2. Show QR code — opens the sub-popup that renders the
         combined pair-share-clone QR for an in-person new phone.
      3. Add permission by github username — folds in the existing
         ``grant_collaborator_popup`` for remote collaborators.
    """
    from .. import lan_list_peers, lan_share_project
    from .popups import grant_collaborator_popup

    # Auto-enable LAN BEFORE the QR is rendered. The QR payload's
    # ``endpoint`` field reads from ``lan_listener.bound_endpoint()``
    # — if the toggle is still off at QR-build time, that endpoint
    # is empty and the scanner records us with no place to push to
    # later, manifesting as ``[lan-push] no endpoint`` on the
    # owner-side fan-out. Calling ``_auto_enable_lan`` first (it's
    # synchronous and pins the bound endpoint before returning)
    # makes sure the QR carries the real listener address.
    auto_enabled = _auto_enable_lan(font_name=font_name)

    container = BoxLayout(orientation='vertical', spacing=dp(10),
                          padding=dp(12))

    title_text = (_tr('Share [{langcode}] project').format(
                      langcode=langcode)
                  if langcode else _tr('Share project'))
    container.add_widget(Label(
        text=title_text,
        size_hint_y=None, height=dp(28), font_size=sp(15),
        bold=True, color=theme.ACCENT, font_name=font_name))

    # --- Section 1: paired phones list (hidden if none) ---------
    # Only renders when at least one paired phone exists; otherwise
    # the QR section below is the natural next step and the empty
    # paired-phones header would just be visual noise.
    peers = lan_list_peers()
    if peers:
        container.add_widget(Label(
            text=_tr('Share with a paired phone:'),
            size_hint_y=None, height=dp(24),
            font_size=sp(12), bold=True, font_name=font_name))
        peers_box = BoxLayout(orientation='vertical',
                              size_hint_y=None, spacing=dp(4))
        peers_box.bind(minimum_height=peers_box.setter('height'))
        peers_scroll = ScrollView(size_hint_y=None, height=dp(140))
        peers_scroll.add_widget(peers_box)
        for peer in peers:
            row = BoxLayout(orientation='horizontal',
                            size_hint_y=None, height=dp(40),
                            spacing=dp(8))
            row.add_widget(Label(
                text=peer.get('device_name') or _tr('Unnamed device'),
                halign='left', valign='middle',
                font_size=sp(12), font_name=font_name,
                text_size=(dp(180), dp(36))))
            shared = langcode and langcode in (
                peer.get('shared_projects') or [])
            btn = Button(
                text=_tr('Shared') if shared else _tr('Share'),
                size_hint=(None, None), width=dp(110), height=dp(36),
                font_size=sp(12), font_name=font_name,
                background_color=(theme.ACCENT if shared
                                  else (0.4, 0.4, 0.4, 1)))

            def _share(_btn, p=peer):
                if not langcode:
                    return
                pid = p.get('peer_id', '')
                if not pid:
                    return
                lan_share_project(langcode, pid)
                _btn.text = _tr('Shared')
                _btn.background_color = theme.ACCENT

            btn.bind(on_release=_share)
            row.add_widget(btn)
            peers_box.add_widget(row)
        container.add_widget(peers_scroll)

    # --- Section 2: pairing QR (inline; no extra click) --------
    # Separator only when section 1 actually rendered above —
    # otherwise "or someone not yet paired" has nothing to be
    # "or" against.
    if peers:
        container.add_widget(Label(
            text=_tr('— or someone not yet paired —'),
            size_hint_y=None, height=dp(24),
            font_size=sp(11), color=theme.TEXT_DIM,
            halign='center', valign='middle', font_name=font_name,
            text_size=(dp(320), dp(24))))
    from .. import lan_pair_qr, lan_peer_id, lan_toggle
    info = lan_peer_id()
    toggle = lan_toggle()
    if not info.get('peer_id'):
        err = info.get('error', '') or _tr('unknown')
        container.add_widget(Label(
            text=_tr('LAN identity is not available on this device.'),
            size_hint_y=None, height=dp(28),
            font_size=sp(12), color=theme.TEXT_DIM,
            font_name=font_name))
        container.add_widget(Label(
            text=str(err), size_hint_y=None, height=dp(22),
            font_size=sp(10), color=theme.TEXT_DIM,
            font_name=font_name))
    else:
        if not toggle.get('on'):
            container.add_widget(Label(
                text=_tr('Turn on local-network sharing in sync '
                         'settings so the other phone can connect '
                         'after scanning.'),
                size_hint_y=None, height=dp(40),
                font_size=sp(10), color=theme.TEXT_DIM,
                halign='center', valign='middle',
                font_name=font_name,
                text_size=(dp(320), dp(40))))
        qr_payload = lan_pair_qr(langcode=langcode)
        if qr_payload:
            qr_widget, qr_err = _render_qr_widget(
                qr_payload, scale=6, border=2)
            if qr_widget is not None:
                qr_widget.size_hint_y = None
                qr_widget.height = dp(200)
                container.add_widget(qr_widget)
            else:
                container.add_widget(Label(
                    text=qr_err, size_hint_y=None, height=dp(28),
                    font_size=sp(11), color=theme.TEXT_DIM,
                    font_name=font_name))
            endpoint_text = qr_payload.get('endpoint', '')
            container.add_widget(Label(
                text=(info.get('device_name')
                      or _tr('this device')) + ' · '
                     + info['peer_id'][:8] + '…'
                     + (' · ' + endpoint_text
                        if endpoint_text else ''),
                size_hint_y=None, height=dp(22),
                font_size=sp(10), color=theme.TEXT_DIM,
                halign='center', valign='middle',
                font_name=font_name,
                text_size=(dp(320), dp(22))))
        else:
            container.add_widget(Label(
                text=_tr('Could not generate pairing QR.'),
                size_hint_y=None, height=dp(28),
                font_size=sp(11), color=theme.TEXT_DIM,
                font_name=font_name))

    # --- Section 3: add github collaborator ---------------------
    # Only shown when the project has a github remote — otherwise
    # the invite would NO_REMOTE-error on tap. A user with an
    # unpublished project should Publish first; the daemon settings
    # UI's separate Publish button is the right entry point for
    # that. Suppressing this section keeps the popup focused on
    # LAN-only sharing when github isn't an option yet.
    _project_has_remote = False
    if langcode:
        try:
            from .. import project_status as _project_status
            _ps = _project_status(langcode)
            if _ps is not None:
                _project_has_remote = bool(
                    (getattr(_ps, 'remote_url', '') or '').strip())
        except Exception:
            _project_has_remote = False
    if _project_has_remote:
        container.add_widget(Label(
            text=_tr('— Add permission by github username —'),
            size_hint_y=None, height=dp(24),
            font_size=sp(11), color=theme.TEXT_DIM,
            halign='center', valign='middle', font_name=font_name,
            text_size=(dp(320), dp(24))))
        invite_btn = Button(
            text=_tr('Invite someone who isn\'t here'),
            size_hint_y=None, height=dp(44),
            font_size=sp(13), font_name=font_name)
        invite_btn.bind(
            on_release=lambda *_: grant_collaborator_popup(
                langcode, font_name=font_name))
        container.add_widget(invite_btn)

    close_btn = Button(
        text=_tr('Close'), size_hint_y=None, height=dp(44),
        font_size=sp(14), font_name=font_name)
    container.add_widget(close_btn)

    # ``auto_enabled`` was computed at the top of this function so
    # the QR payload above could see the bound listener endpoint.

    popup = Popup(
        title=title_text, content=container,
        size_hint=(0.95, 0.95), auto_dismiss=False)

    def _on_close(*_):
        popup.dismiss()
        _offer_disable_when_done(auto_enabled, font_name=font_name)

    close_btn.bind(on_release=_on_close)
    popup.open()
    return popup


def adopt_origin_popup(decision, on_done=None, font_name='Roboto'):
    """Always-confirm prompt before setting ``origin`` on a
    project. *decision* is a pending-decision entry (dict with
    ``id``, ``params``). Calls ``on_done(result_or_None)`` when
    the user has accepted, declined, or dismissed.

    Shown inline by the post-scan flow when an
    ``LAN_ADOPT_ORIGIN_NEEDED`` lands, and from the settings-side
    pending-decisions list."""
    from .. import lan_adopt_origin
    params = decision.get('params') or {}
    langcode = str(params.get('langcode', '') or '')
    url = str(params.get('url', '') or '')
    device_name = str(params.get('device_name', '')
                      or _tr('a paired phone'))

    content = BoxLayout(orientation='vertical', spacing=dp(10),
                        padding=dp(12))
    content.add_widget(Label(
        text=_tr('Push project {langcode} to {url} too?').format(
            langcode=langcode, url=url),
        size_hint_y=None, height=dp(56),
        font_size=sp(13), bold=True, halign='center',
        valign='middle', font_name=font_name,
        text_size=(dp(320), dp(56))))
    content.add_widget(Label(
        text=_tr('{device_name} uses this URL for the same '
                 'project. Adopting it means future commits will '
                 'go to GitHub too, not just over the local '
                 'network.').format(device_name=device_name),
        font_size=sp(11), color=theme.TEXT_DIM,
        halign='left', valign='top', font_name=font_name,
        text_size=(dp(340), None)))

    btn_row = BoxLayout(orientation='horizontal',
                        size_hint_y=None, height=dp(52),
                        spacing=dp(8))
    decline_btn = Button(
        text=_tr('No, just local'),
        font_size=sp(13), font_name=font_name)
    accept_btn = Button(
        text=_tr('Yes, use it'),
        font_size=sp(13), font_name=font_name,
        background_color=theme.ACCENT)
    btn_row.add_widget(decline_btn)
    btn_row.add_widget(accept_btn)
    content.add_widget(btn_row)

    popup = Popup(
        title=_tr('Use the same GitHub URL?'),
        content=content,
        size_hint=(0.9, None), height=dp(280),
        auto_dismiss=False)

    def _resolve(accept):
        result = lan_adopt_origin(decision['id'], accept)
        popup.dismiss()
        if on_done is not None:
            try:
                on_done(result)
            except Exception:
                pass

    accept_btn.bind(on_release=lambda *_: _resolve(True))
    decline_btn.bind(on_release=lambda *_: _resolve(False))
    popup.open()
    return popup


def pending_offers_popup(on_done=None, font_name='Roboto'):
    """Picker-side entry: shows pending share-offers from already-
    paired peers (Accept / Decline buttons per row) and a
    fall-through "Scan QR code" button for first-time pair.

    ``on_done(result)`` fires after any action that produced a
    project the caller should pick up (accept-offer that cloned,
    scan-to-pair that cloned). Cancel / dismiss without action
    doesn't fire ``on_done``."""
    from .. import lan_pending, lan_accept_offer, lan_decline_offer

    container = BoxLayout(orientation='vertical', spacing=dp(8),
                          padding=dp(12))
    container.add_widget(Label(
        text=_tr('Receive a project from another phone'),
        size_hint_y=None, height=dp(28), font_size=sp(15),
        bold=True, color=theme.ACCENT, font_name=font_name))

    list_box = BoxLayout(orientation='vertical', size_hint_y=None,
                         spacing=dp(6))
    list_box.bind(minimum_height=list_box.setter('height'))
    scroll = ScrollView(size_hint_y=1)
    scroll.add_widget(list_box)

    def _refresh():
        list_box.clear_widgets()
        offers = [d for d in lan_pending()
                  if d.get('kind') == 'share_offer']
        if not offers:
            list_box.add_widget(Label(
                text=_tr('No paired phones are offering a project '
                         'right now.'),
                size_hint_y=None, height=dp(40),
                font_size=sp(11), color=theme.TEXT_DIM,
                halign='center', valign='middle',
                font_name=font_name,
                text_size=(dp(320), dp(40))))
            return
        for d in offers:
            params = d.get('params') or {}
            row = BoxLayout(orientation='vertical',
                            size_hint_y=None, height=dp(80),
                            spacing=dp(4), padding=dp(6))
            row.add_widget(Label(
                text=_tr('{device_name} offers project {langcode}'
                         ).format(
                             device_name=params.get('device_name')
                                 or _tr('Unnamed device'),
                             langcode=params.get('langcode', '')),
                size_hint_y=None, height=dp(28),
                font_size=sp(12), bold=True, halign='left',
                valign='middle', font_name=font_name,
                text_size=(dp(340), dp(28))))
            btn_row = BoxLayout(orientation='horizontal',
                                size_hint_y=None, height=dp(40),
                                spacing=dp(8))
            decline_btn = Button(
                text=_tr('Decline'),
                font_size=sp(12), font_name=font_name)
            accept_btn = Button(
                text=_tr('Accept'),
                font_size=sp(12), font_name=font_name,
                background_color=theme.ACCENT)
            btn_row.add_widget(decline_btn)
            btn_row.add_widget(accept_btn)
            row.add_widget(btn_row)

            def _accept(_btn, decision=d):
                from .. import S as _S
                result = lan_accept_offer(decision['id'])
                popup.dismiss()
                # Same in-flow adopt-origin confirm as scan_to_pair:
                # if the accepted offer stashed an adopt-origin
                # decision, surface it now before bubbling the
                # Result back to the picker host.
                if result.has(_S.LAN_ADOPT_ORIGIN_NEEDED):
                    _resolve_adopt_origin_then_done(
                        result, on_done, font_name)
                    return
                if on_done is not None:
                    try:
                        on_done(result)
                    except Exception:
                        pass

            def _decline(_btn, decision=d):
                lan_decline_offer(decision['id'])
                _refresh()

            accept_btn.bind(on_release=_accept)
            decline_btn.bind(on_release=_decline)
            list_box.add_widget(row)

    container.add_widget(scroll)

    # New-phone fallthrough: scan a QR code.
    container.add_widget(Label(
        text=_tr('— or pair with a new phone —'),
        size_hint_y=None, height=dp(24),
        font_size=sp(11), color=theme.TEXT_DIM,
        halign='center', valign='middle', font_name=font_name,
        text_size=(dp(320), dp(24))))
    scan_btn = Button(
        text=_tr('Scan QR code'),
        size_hint_y=None, height=dp(44),
        font_size=sp(13), font_name=font_name)

    def _scan(*_):
        popup.dismiss()
        scan_to_pair(on_done=on_done, font_name=font_name)

    scan_btn.bind(on_release=_scan)
    container.add_widget(scan_btn)

    close_btn = Button(
        text=_tr('Close'),
        size_hint_y=None, height=dp(44),
        font_size=sp(13), font_name=font_name)
    container.add_widget(close_btn)

    popup = Popup(
        title=_tr('Receive a project from another phone'),
        content=container,
        size_hint=(0.95, 0.95), auto_dismiss=False)
    close_btn.bind(on_release=lambda *_: popup.dismiss())
    _refresh()
    popup.open()
    return popup


def pending_share_offer_count():
    """Cheap query for the picker entry-point badge. Returns the
    integer number of pending share-offers; the picker uses this
    to suffix "(N waiting)" on the button text. Empty / failure →
    0 (button shows no badge)."""
    from .. import lan_pending
    try:
        return sum(1 for d in lan_pending()
                   if d.get('kind') == 'share_offer')
    except Exception:
        return 0
