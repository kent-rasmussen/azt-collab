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
from kivy.core.window import Window
from kivy.metrics import dp, sp
from kivy.uix.behaviors import ButtonBehavior
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


def _link_button(text, color=None, **kwargs):
    """A ThemedButton styled as a plain text link — no fill, colored
    text (default ``theme.RED``). Used for the passive pending-offer
    affordances; a solid red button read as alarming ("bit much",
    field 2026-07-23)."""
    kwargs.setdefault('bold', False)
    btn = Button(text=text, color=color or theme.RED, **kwargs)
    try:
        btn._fill_color.rgba = (0, 0, 0, 0)
    except Exception:
        pass
    return btn


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
    # auto-populates the endpoint(s) from the running listener when
    # present. Passing *langcode* makes the payload combined
    # (pair + share + clone) rather than pair-only. Rendered into a
    # replaceable box so the live-refresh below can swap it in place
    # when the machine's addresses change (USB-tether plug).
    from .. import lan_pair_qr
    qr_box = BoxLayout(orientation='vertical', spacing=dp(6))
    content.add_widget(qr_box)
    render_state = {'sig': None}

    def _endpoints_sig(payload):
        eps = payload.get('endpoints')
        if isinstance(eps, list) and eps:
            return tuple(eps)
        return (payload.get('endpoint', ''),)

    def _render_into(payload):
        qr_box.clear_widgets()
        if not payload:
            qr_box.add_widget(Label(
                text=_tr('Could not generate pairing QR.'),
                font_size=sp(12), color=theme.TEXT_DIM,
                font_name=font_name))
            render_state['sig'] = None
            return
        widget, err = _render_qr_widget(payload, scale=8, border=2)
        if widget is not None:
            qr_box.add_widget(widget)
        else:
            qr_box.add_widget(Label(
                text=err, font_size=sp(12),
                color=theme.TEXT_DIM, font_name=font_name))
        endpoint = payload.get('endpoint', '')
        if endpoint:
            qr_box.add_widget(Label(
                text=_tr('Endpoint: ') + endpoint,
                size_hint_y=None, height=dp(22),
                font_size=sp(11), color=theme.TEXT_DIM,
                font_name=font_name))
        render_state['sig'] = _endpoints_sig(payload)

    _render_into(lan_pair_qr(langcode=langcode))

    close_btn = Button(
        text=_tr('Close'), size_hint_y=None, height=dp(44),
        font_size=sp(14), font_name=font_name)
    content.add_widget(close_btn)

    popup = Popup(
        title=_tr('Pair a phone'),
        content=content, size_hint=(0.9, 0.9),
        auto_dismiss=False)
    close_btn.bind(on_release=lambda *_: popup.dismiss())

    from kivy.clock import Clock

    # Live-refresh the QR when this machine's addresses change — e.g.
    # the user plugs in a phone and enables USB tethering while the QR
    # is on screen, bringing up usb0. The daemon payload reflects the
    # current interfaces (bound_endpoints_all), so a re-fetch picks up
    # the new address; we only re-render the (relatively costly) QR
    # when the advertised endpoints actually changed. 0.54.36.
    refresh = {'ev': None}

    def _refresh_qr(_dt):
        try:
            payload = lan_pair_qr(langcode=langcode)
        except Exception:
            return
        if payload and _endpoints_sig(payload) != render_state['sig']:
            _render_into(payload)

    refresh['ev'] = Clock.schedule_interval(_refresh_qr, 4)

    # "Valid while displayed" share offer (0.52.26): the initial
    # ``lan_pair_qr`` above already armed the offer for *langcode*; while
    # this popup stays open we heartbeat so it stays armed, and we revoke
    # it the instant the popup closes. Multi-use daemon-side, so one
    # displayed QR can be scanned by several peers. Pair-only QRs carry no
    # langcode and share nothing, so no heartbeat needed.
    hb = {'ev': None}
    if langcode:
        from .. import lan_pair_qr_keepalive, lan_pair_qr_close

        def _beat(_dt):
            try:
                lan_pair_qr_keepalive(langcode)
            except Exception:
                pass

        # Every 10 s; the daemon keepalive window is 30 s, so one missed
        # beat is tolerated before the offer lapses.
        hb['ev'] = Clock.schedule_interval(_beat, 10)

    def _on_dismiss(*_):
        if refresh['ev'] is not None:
            refresh['ev'].cancel()
            refresh['ev'] = None
        if hb['ev'] is not None:
            hb['ev'].cancel()
            hb['ev'] = None
        if langcode:
            try:
                lan_pair_qr_close(langcode)
            except Exception:
                pass

    popup.bind(on_dismiss=_on_dismiss)

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
    from .. import lan_pair_accept, lan_clone, lan_clone_progress, S
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

        # Live transfer progress: poll the daemon's clone-progress
        # slot and surface the git sideband line ("Counting objects:
        # 12% (n/m)") in place of the static hint — a first copy can
        # run minutes, and an unmoving screen reads as hung (field,
        # 2026-07-17). RPC on the Kivy clock follows the
        # pair-request poll pattern (cheap loopback/CP call); the
        # daemon's HTTP server is threaded, so this gets through
        # while the clone RPC is still occupying its own thread.
        sub_default = sub_label.text
        seen = {'progress': False}
        def _poll_progress(_dt):
            try:
                snap = lan_clone_progress()
            except Exception:
                return
            if snap.get('active') and snap.get('text'):
                if not seen['progress']:
                    # First real sideband line — bytes are moving, so
                    # NOW the label may say so (0.55.4).
                    _set_phase(_tr('Copying project to this phone…'))
                seen['progress'] = True
                sub_label.text = snap['text']
            elif not seen['progress']:
                sub_label.text = sub_default
            # else: keep the LAST progress line. The slot goes
            # inactive between candidate-address attempts and during
            # post-transfer finalize (find LIFT / register / merge);
            # reverting to the "first time can take a while" hint
            # AFTER data visibly moved read as a silent restart
            # (field 2026-07-24).
        progress_poll_ev = Clock.schedule_interval(_poll_progress, 1.0)

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
                    # NOT "Copying…" yet (0.55.4): nothing is copying
                    # until the transfer actually starts. With an
                    # unreachable peer this phase lasts minutes —
                    # candidate addresses × dulwich retries × connect
                    # timeout — and claiming a copy throughout is
                    # simply false (field 2026-07-27: "it still says
                    # copying"). ``_poll_progress`` promotes the label
                    # the moment real sideband progress arrives.
                    Clock.schedule_once(
                        lambda dt: _set_phase(
                            _tr('Contacting the other phone…')),
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
                progress_poll_ev.cancel()
            except Exception:
                pass
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
                    and result.has(S.LAN_LOCAL_TLS_ERROR)):
                # OUR side's TLS/identity is broken — saying "the other
                # phone did not respond" here sent the user chasing
                # Wi-Fi when the peer had answered fine (2026-07-17).
                _show_lan_failure_popup(
                    _tr('Sharing setup problem on THIS device'),
                    _tr('This device could not make the secure '
                        'connection — its own sharing-identity files '
                        'are missing or damaged. This is not a network '
                        'problem. Restart the collaboration service '
                        'and try again; if it keeps happening, share '
                        'diagnostics from the settings screen.'),
                    font_name)
            elif (not got_project
                    and result.has(S.LAN_PROJECT_NOT_SHARED)):
                # The peer ANSWERED; its listener refused the repo
                # (not in its share allowlist, or not registered
                # there). "Did not respond" here blamed the network
                # when the fix is on the other device (2026-07-17).
                _show_lan_failure_popup(
                    _tr('The other phone is not sharing this project'),
                    _tr('The other phone answered, but it is not '
                        'offering this project to this device. On '
                        'the other phone, open the project and '
                        'share it with this device, then try '
                        'again.'),
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
                # contributor name isn't set. Route to the
                # contributor-settings screen so the user can type
                # their name and come back to re-scan.
                #
                # **Two host contexts** (0.50.17 fix):
                # 1. Scanned from a peer app (recorder, viewer):
                #    ``open_server_ui()`` fires a launcher intent
                #    against the server APK, which lands the user
                #    in PickerApp with ``launch_mode='internal'``
                #    → settings screen.
                # 2. Scanned from the server APK's own picker:
                #    ``open_server_ui()`` fires a launcher intent
                #    against the package we're already in →
                #    Android brings the picker to the front, no
                #    settings screen. User ends up back on the
                #    empty picker, confused.
                # Detect via ``hasattr(app, 'go')`` + the screen
                # being registered — if we can navigate in-
                # process, do so; otherwise fall through to the
                # cross-process intent.
                _emit_status(_tr(
                    'Set your name on the next screen, then scan '
                    'the QR again.'))
                navigated = False
                try:
                    from kivy.app import App
                    app = App.get_running_app()
                    if (app is not None
                            and hasattr(app, 'go')
                            and hasattr(app, 'sm')
                            and getattr(app.sm, 'has_screen', None)
                            and app.sm.has_screen('settings')):
                        app.go('settings')
                        navigated = True
                except Exception as ex:
                    print(f'[scan_to_pair] in-process settings '
                          f'navigation raised: {ex!r}',
                          file=sys.stderr, flush=True)
                if not navigated:
                    try:
                        from .. import open_server_ui
                        open_server_ui(on_status=_emit_status)
                    except Exception as ex:
                        print(f'[scan_to_pair] open_server_ui '
                              f'raised: {ex!r}',
                              file=sys.stderr, flush=True)
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
            elif not got_project:
                # CATCH-ALL (0.55.2). The branches above name five
                # specific failures; anything else fell off the end of
                # the chain and reached ``_final_done`` with NO message
                # — the popup closed, the picker repopulated, and the
                # user was told nothing at all. Field 2026-07-27: a
                # clone failed with `[lan-clone] ls-remote … [Errno
                # 101] Network is unreachable` in the daemon log, and
                # the UI simply returned to the picker. The log knowing
                # why is not the same as the user knowing why.
                #
                # Render whatever codes we DID get, translated. Vaguer
                # than the tailored branches by construction — it fires
                # precisely when we have no specific advice — but a
                # user who can see "did not finish: <reason>" can act,
                # or report it. Silence is the one outcome that leaves
                # them stuck.
                from ..translate import translate_result
                detail = ''
                try:
                    detail = (translate_result(result) or '').strip()
                except Exception:
                    detail = ''
                if not detail:
                    detail = ', '.join(result.codes()) or _tr('unknown')
                _show_lan_failure_popup(
                    _tr('Could not receive the project'),
                    detail, font_name)
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


class _TapBox(ButtonBehavior, BoxLayout):
    """BoxLayout that fires ``on_release`` on tap. Used as the
    composite tap target in the share popup so the whole "Shared
    / Offer share again" column registers a re-offer no matter
    where the finger lands. Kivy's ``ButtonBehavior`` is a mixin
    that adds tap semantics to any widget."""
    pass


def _section_header(text, font_name):
    """Bold accent-colour section header used by the LAN popups
    ("This phone" / "Unpaired" / "Paired")."""
    lbl = Label(
        text=text, halign='left', valign='middle',
        size_hint_y=None, height=dp(28), font_size=sp(13),
        bold=True, color=theme.ACCENT, font_name=font_name)
    lbl.bind(width=lambda w, *_: setattr(
        w, 'text_size', (w.width, dp(28))))
    return lbl


def _build_full_row(*, name, peer_id='', endpoint='', projects='',
                    buttons=None, right_widget=None,
                    font_name='Roboto'):
    """Wide row used in the paired-phones / share popups.

    Four stacked labels in the left column: name (bold, falls back
    to wrap only if it can't fit the popup's full width), full
    peer_id, endpoint (``ip:port``), shared projects (wraps if
    long). Right column is either the *buttons* list stacked
    vertically (Manage/Unpair, Pair, …), or — when the caller
    needs more than buttons (e.g. a "Shared" label + "Offer share
    again" link) — a pre-built composite widget supplied as
    *right_widget*. ``buttons=None`` and ``right_widget=None``
    together produce an info-only row (the "This phone" header
    row uses this)."""
    row = BoxLayout(orientation='horizontal', size_hint_y=None,
                    spacing=dp(8), padding=(dp(4), dp(4)))

    info = BoxLayout(orientation='vertical', size_hint_y=None,
                     spacing=dp(2))
    info.bind(minimum_height=info.setter('height'))

    def _add(text, *, bold=False, size=sp(10),
             color=None, min_height=dp(18), max_lines=0):
        lbl = Label(
            text=text or '—',
            halign='left', valign='top',
            size_hint_y=None, font_size=size, bold=bold,
            font_name=font_name)
        if max_lines:
            # Cap to N lines with an ellipsis — a peer can accumulate a
            # LOT of observed/static endpoints, and an uncapped label
            # grows tall enough to shove the rest of the row (and the
            # list) off the popup. shorten adds the '…'; max_lines is
            # the hard clip that guarantees the height ceiling below is
            # never exceeded regardless of Kivy's shorten quirks.
            lbl.shorten = True
            lbl.shorten_from = 'right'
            lbl.max_lines = max_lines
        if color is not None:
            lbl.color = color
        lbl.bind(width=lambda w, *_: setattr(
            w, 'text_size', (w.width, None)))

        def _sync_h(w, ts):
            h = max(min_height, ts[1])
            if max_lines:
                h = min(h, min_height * max_lines + dp(2) * (max_lines - 1))
            w.height = h
        lbl.bind(texture_size=_sync_h)
        info.add_widget(lbl)
        return lbl

    _add(name or _tr('Unnamed device'),
         bold=True, size=sp(14), min_height=dp(24))
    _add(peer_id, color=theme.TEXT_DIM)
    _add(endpoint, color=theme.TEXT_DIM, max_lines=2)
    _add(projects, color=theme.TEXT_DIM)

    row.add_widget(info)

    if right_widget is not None:
        # Caller-owned composite. We just add it; the caller is
        # responsible for its size_hint / width.
        row.add_widget(right_widget)
    elif buttons:
        # Right column: size_hint=(None, 1) so it stretches to the
        # info column's height; buttons stack from the top.
        btn_col = BoxLayout(orientation='vertical',
                            size_hint=(None, 1),
                            width=dp(100), spacing=dp(4),
                            padding=(0, dp(4)))
        for b in buttons:
            btn_col.add_widget(b)
        # Filler so buttons don't stretch to fill the column.
        btn_col.add_widget(BoxLayout())
        row.add_widget(btn_col)

    def _resize(*_):
        right_h = 0
        if right_widget is not None:
            right_h = getattr(right_widget, 'height', 0) or 0
        row.height = max(info.height + dp(8),
                         right_h + dp(8), dp(56))
    info.bind(height=_resize)
    if right_widget is not None:
        try:
            right_widget.bind(height=_resize)
        except Exception:
            pass
    _resize()
    return row


def _peer_endpoint_str(peer):
    """Best-effort 'ip:port' string for a paired-peer entry: prefer
    the most recently observed live endpoint, fall back to manually-
    configured static endpoints, '' if neither set."""
    eps = list(peer.get('endpoints') or [])
    eps += [e for e in (peer.get('static_endpoints') or [])
            if e not in eps]
    return ', '.join(eps)


def _pending_offers_for(peer_id):
    """Return the pending share-offer decisions whose
    ``params.peer_id`` matches *peer_id*. Never raises — returns []
    on any error so callers can fold it into row-building without a
    guard."""
    try:
        from .. import lan_pending
        out = []
        for d in (lan_pending() or []):
            if not isinstance(d, dict):
                continue
            if d.get('kind') != 'share_offer':
                continue
            params = d.get('params') or {}
            if str(params.get('peer_id', '') or '') == str(peer_id or ''):
                out.append(d)
        return out
    except Exception:
        return []


def _offer_confirm_popup(decision, on_done, font_name='Roboto'):
    """Accept / Decline confirm for a single pending share-offer.

    Accept runs ``lan_accept_offer`` off the UI thread and renders
    the outcome (copied / kept-for-later when the peer is absent /
    generic failure). Either action calls ``on_done()`` — the parent
    screen's refresh — before dismissing."""
    from .. import (lan_accept_offer, lan_decline_offer,
                    lan_clone_progress, S)
    from ..status import Result, Status
    from ..translate import translate_status

    params = decision.get('params') or {}
    decision_id = decision.get('id', '')
    peer_id = str(params.get('peer_id', '') or '')
    device = (params.get('device_name')
              or (peer_id[:8] if peer_id else _tr('Unnamed device')))
    project = str(params.get('langcode', '') or '')

    body = BoxLayout(orientation='vertical', spacing=dp(12),
                     padding=dp(16))
    msg = Label(
        text=_tr('{device} wants to share “{project}” with you.'
                 ).format(device=device, project=project),
        font_size=sp(13), font_name=font_name,
        halign='center', valign='top', size_hint_y=None,
        text_size=(dp(280), None))
    msg.bind(texture_size=lambda w, ts: setattr(w, 'height', ts[1]))
    body.add_widget(msg)

    status_lbl = Label(
        text='', font_size=sp(12), font_name=font_name,
        halign='center', valign='middle', color=theme.TEXT_DIM,
        size_hint_y=None, height=dp(0), text_size=(dp(280), None))
    body.add_widget(status_lbl)

    btn_row = BoxLayout(orientation='horizontal', size_hint_y=None,
                        height=dp(44), spacing=dp(8))
    decline_btn = Button(text=_tr('Decline'), font_size=sp(13),
                         font_name=font_name)
    accept_btn = Button(text=_tr('Accept'), font_size=sp(13),
                        font_name=font_name,
                        background_color=theme.ACCENT)
    btn_row.add_widget(decline_btn)
    btn_row.add_widget(accept_btn)
    body.add_widget(btn_row)

    popup = Popup(title=_tr('Share invitation'), content=body,
                  size_hint=(0.9, 0.5), auto_dismiss=False)

    # Holder for the clone-progress poll event so _accept can start
    # it and _on_accept_result / _finish can cancel it.
    _prog = {'ev': None}

    def _stop_progress():
        ev = _prog.get('ev')
        if ev is not None:
            try:
                ev.cancel()
            except Exception:
                pass
            _prog['ev'] = None

    def _finish():
        _stop_progress()
        try:
            if on_done is not None:
                on_done()
        except Exception:
            pass
        popup.dismiss()

    def _on_accept_result(result):
        _stop_progress()
        delay = 3.0
        if result.has_any(S.LAN_PROJECT_CLONED, S.LAN_PROJECT_REOPENED):
            status_lbl.text = _tr('Project copied to this phone.')
            delay = 1.4
            if result.has(S.LAN_ADOPT_ORIGIN_NEEDED):
                # Surface the "adopt github remote?" confirm IN-FLOW,
                # right after this popup closes — same gesture, same
                # device. Without this it fell through to the
                # background decisions watcher and popped on whatever
                # peer app opened next (field 2026-07-23: the
                # recorder). Mirrors scan_to_pair.
                Clock.schedule_once(
                    lambda _dt: _resolve_adopt_origin_then_done(
                        result, None, font_name),
                    delay + 0.2)
        elif result.has(S.LAN_OFFER_PEER_ABSENT):
            # Render just the peer-absent status ("kept — ask again
            # when nearby"), not the whole joined result.
            absent = next((s for s in result.statuses
                           if s.code == S.LAN_OFFER_PEER_ABSENT), None)
            status_lbl.text = (translate_status(absent) if absent
                               else _tr('Could not copy the project.'))
        else:
            # Surface the SPECIFIC failure (LAN_LOCAL_TLS_ERROR,
            # LAN_PROJECT_NOT_SHARED, LAN_CLONE_TIMEOUT, …) — the clone
            # adds one terminal status; translating it tells the user
            # (and us) what actually blocked the copy, instead of a
            # flat "couldn't copy" that hides a fixable cause.
            worst = result.statuses[-1] if result.statuses else None
            status_lbl.text = (translate_status(worst) if worst
                               else _tr('Could not copy the project.'))
        # Let the user read the outcome line, then refresh + dismiss.
        Clock.schedule_once(lambda _dt: _finish(), delay)

    def _accept(*_):
        accept_btn.disabled = True
        decline_btn.disabled = True
        status_lbl.height = dp(24)
        status_lbl.text = _tr('Working…')

        # Surface the daemon's live clone progress ("Counting objects:
        # 12% (n/m)") while the accept RPC runs on its own thread — a
        # first copy can run for a while, and an unmoving screen reads
        # as hung. Same poll pattern as the pairing/receive flow.
        def _poll_progress(_dt):
            try:
                snap = lan_clone_progress()
            except Exception:
                return
            if snap.get('active') and snap.get('text'):
                status_lbl.text = snap['text']
        _prog['ev'] = Clock.schedule_interval(_poll_progress, 1.0)

        def _worker():
            try:
                result = lan_accept_offer(decision_id)
            except Exception as ex:
                result = Result(statuses=[Status(
                    'SERVER_ERROR', {'error': f'{ex!r}'})])
            Clock.schedule_once(
                lambda _dt: _on_accept_result(result), 0)

        threading.Thread(target=_worker, daemon=True,
                         name='lan-offer-accept').start()

    def _decline(*_):
        accept_btn.disabled = True
        decline_btn.disabled = True

        def _worker():
            try:
                lan_decline_offer(decision_id)
            except Exception:
                pass
            Clock.schedule_once(lambda _dt: _finish(), 0)

        threading.Thread(target=_worker, daemon=True,
                         name='lan-offer-decline').start()

    accept_btn.bind(on_release=_accept)
    decline_btn.bind(on_release=_decline)
    popup.open()
    return popup


def _retarget_this_app(peer_id, device_name, font_name='Roboto'):
    """Point THIS running app at *peer_id*'s daemon (0.55.127).

    Android's answer to "open that device's settings", because a second
    Kivy process cannot exist here. Every subsequent RPC in this process
    goes to that peer, so the settings screens show ITS values.

    Loud and reversible on purpose — the objection to retargeting is that
    you forget which machine you are changing, and on a phone there is no
    second window to make it obvious. So: a confirm naming the device
    before it happens, and a way back that is always visible afterwards.

    The transport is built daemon-side (it needs the private key and
    ``peers.json``), so this asks the local daemon to hand one over rather
    than importing ``azt_collabd`` — hard rule #3."""
    from .. import transports as _t

    def _go(*_a):
        try:
            # The daemon owns the pinned dial + signing; ask IT to verify
            # the grant first so a refusal is a sentence, not a screen of
            # empty fields.
            from .. import lan_can_admin
            res = lan_can_admin(peer_id) or {}
            if not res.get('allowed'):
                _show_info_popup(
                    _tr('Cannot open that device'),
                    str(res.get('detail')
                        or _tr('That device has not allowed this one to '
                               'change its settings.')),
                    font_name=font_name)
                return
            _t.target_remote_peer(peer_id)
        except Exception as ex:
            _show_info_popup(_tr('Cannot open that device'),
                             str(ex), font_name=font_name)
            return
    # NO CONFIRM (0.55.128). There was a "Change settings on X instead of
    # this device? / Yes, edit that device" step here. Kent: *"what's with
    # the 'yes, edit that device?'"* — fair: it existed to make an
    # otherwise-invisible retarget visible, and the persistent banner
    # (`EDITING <device> — not this device`) plus the "Back to this
    # device" button already do that, continuously, where a one-time
    # dialog does it once. So the tap acts, and only a FAILURE speaks.
    _go()


def _show_info_popup(title, body, font_name='Roboto'):
    """One-line-per-sentence read-only popup (0.55.121).

    Exists because the remote-settings refusal is ACTIONABLE — it names
    what to tap on the other device — and a button label truncated to 40
    characters threw exactly that half away."""
    box = BoxLayout(orientation='vertical', spacing=dp(10),
                    padding=dp(12))
    lbl = Label(text=str(body), font_size=sp(12), font_name=font_name,
                halign='left', valign='top')
    lbl.bind(size=lambda w, *_: setattr(
        w, 'text_size', (w.width, None)))
    box.add_widget(lbl)
    pop = Popup(title=str(title), content=box,
                size_hint=(0.9, None), height=dp(240))
    close = Button(text=_tr('Close'), size_hint_y=None, height=dp(40),
                   font_name=font_name)
    close.bind(on_release=lambda *_a: pop.dismiss())
    box.add_widget(close)
    pop.open()


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

    # Pending invitations from this peer. Passive surface: the user
    # affirms once here; an affirmed offer auto-completes when the
    # peer is next nearby. Rendered nothing-when-empty (no header).
    pending_box = BoxLayout(orientation='vertical', size_hint_y=None,
                            spacing=dp(4))
    pending_box.bind(minimum_height=pending_box.setter('height'))
    content.add_widget(pending_box)

    def _refresh_pending():
        pending_box.clear_widgets()
        offers = _pending_offers_for(pid)
        if not offers:
            return
        pending_box.add_widget(Label(
            text=_tr('Pending invitations'),
            size_hint_y=None, height=dp(24), font_size=sp(12),
            bold=True, font_name=font_name))
        for d in offers:
            o_params = d.get('params') or {}
            lang = str(o_params.get('langcode', '') or '')
            affirmed = bool(o_params.get('affirmed'))
            label_text = (
                _tr('{project} — will sync when nearby') if affirmed
                else _tr('{project} pending')).format(project=lang)
            row = BoxLayout(orientation='horizontal', size_hint_y=None,
                            height=dp(34), spacing=dp(8))
            row.add_widget(Label(
                text=label_text, halign='left', valign='middle',
                font_size=sp(12), color=theme.RED, font_name=font_name,
                text_size=(dp(180), dp(30))))
            review_btn = _link_button(
                _tr('Review'),
                size_hint=(None, None), width=dp(110), height=dp(32),
                font_size=sp(12), font_name=font_name)
            review_btn.bind(on_release=lambda _b, dd=d:
                            _offer_confirm_popup(
                                dd, on_done=_refresh_pending,
                                font_name=font_name))
            row.add_widget(review_btn)
            pending_box.add_widget(row)

    _refresh_pending()

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

    # REMOTE SETTINGS GRANT (0.55.120). Belongs here, in the per-peer
    # detail popup that already owns "which projects do I share with this
    # device" and Unpair — 0.55.118 put it on the sync-board rows at the
    # bottom of the settings page instead, which is a status readout, not
    # a place anyone goes to change what a device may do. Kent: *"I don't
    # see 'allow' … nor in the detail of the page."*
    #
    # Deliberately separate from sharing: sharing is about DATA, this is
    # about CONTROL. Every field phone is paired and shares projects; a
    # lost one must not be able to reconfigure a desktop. So this is its
    # own row, its own words, and it starts off.
    # Two ordinary buttons in the existing stack (0.55.122). 0.55.120 gave
    # this a bold header, a grey subtitle and a two-button row; Kent:
    # *"this is too complex … No extra section, just a couple more buttons
    # to ignore if you don't understand them."* The label has to carry the
    # whole meaning, so it is a sentence, not a state word.
    admin_state = {'on': bool(peer.get('admin'))}

    def _admin_label():
        # Normal button colour in both states (0.55.123). Green-for-on
        # against the blue stack was, in Kent's words, *"dizzying"* — and
        # colour was carrying meaning the words can carry alone. STOP in
        # capitals does the work instead: it reads as the reversal it is,
        # without implying danger the way red would.
        return (_tr('STOP letting this device change my settings')
                if admin_state['on']
                else _tr('Allow this device to change my settings'))

    admin_btn = Button(
        text=_admin_label(),
        size_hint_y=None, height=dp(44),
        font_size=sp(13), font_name=font_name)

    def _toggle_admin(_btn):
        from .. import lan_set_admin
        want = not admin_state['on']
        _btn.disabled = True
        resp = lan_set_admin(pid, want) or {}
        _btn.disabled = False
        if not resp.get('ok'):
            # Say it failed rather than flipping the label on a write that
            # didn't land — a permission control that lies about its own
            # state is worse than one that's hard to find.
            _btn.text = _tr('Failed — try again')
            return
        admin_state['on'] = bool(resp.get('admin'))
        _btn.text = _admin_label()

    admin_btn.bind(on_release=_toggle_admin)
    # Created here, ADDED after "Save manual IPs" (0.55.139) — Kent: *"allow
    # this device should be after save manual IPs, not above it."* Sharing
    # and addressing are about a device's ordinary participation; permission
    # to change this machine is a different order of thing and belongs below
    # them, next to diagnostics and Forget.

    # OPEN THAT DEVICE'S SETTINGS (0.55.120). The mirror of the grant
    # above: that button controls what THEY may do here, this one uses
    # what WE have been allowed to do THERE. Only they can know whether
    # they granted us, so this is always offered and a refusal explains
    # itself rather than being pre-emptively hidden.
    #
    # Desktop only — a second Kivy window is not a thing on Android, and
    # on a phone the person is holding the device already.
    open_btn = Button(
        text=_tr('Open settings for this device'),
        size_hint_y=None, height=dp(44),
        font_size=sp(13), font_name=font_name)

    def _open_remote(_btn):
        # Spawn, not import: the client may not import azt_collabd (hard
        # rule #3). Same shape as pick_project's picker subprocess.
        import subprocess
        import sys as _sys
        # ``on_android()``, NOT ``platform == 'android'`` (0.55.127).
        # Unlike Kivy's ``platform``, this module's is a FUNCTION, so the
        # comparison was function-vs-string — always false. The guard
        # never fired, the spawn ran on Android, and the user got a bare
        # ``[Errno 13] Permission denied`` with nothing else, which says
        # nothing about the actual reason.
        from .._platform import on_android as _on_android
        if _on_android():
            # In-place retarget — the only mechanism Android has, since a
            # second Kivy process cannot exist there (one interpreter per
            # PythonActivity; a second fighting the same GIL is the
            # documented SIGSEGV).
            #
            # This reinstates a mode Kent had rejected, and I removed it
            # again when he pointed that out — then he weighed it against
            # the alternative: *"better than leave android
            # non-functional."* The original objection assumed two windows
            # were available; here they aren't, so the mode stays and the
            # persistent banner plus "Back to this device" carry the state.
            _retarget_this_app(pid, peer.get('device_name') or '',
                               font_name=font_name)
            return
        _btn.disabled = True
        try:
            proc = subprocess.Popen(
                [_sys.executable, '-m', 'azt_collabd', 'ui',
                 '--peer', str(pid)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True)
            try:
                # It exits 2 immediately when that device hasn't granted
                # us, or can't be reached. A short wait turns "no window
                # ever appeared" into a sentence saying why.
                # Must exceed the launcher's grant probe (0.55.121, 8 s)
                # or we report "opened" for a process about to exit 2.
                _out, _err = proc.communicate(timeout=15)
                if proc.returncode not in (None, 0):
                    # Show the daemon's own explanation, not a generic
                    # failure: it names which of the three gates refused
                    # and, for the common case, what to tap on the other
                    # device. Truncating that to 40 chars threw away the
                    # actionable half, so put it where it can be read.
                    tail = [ln for ln in (_err or b'').decode(
                        'utf-8', 'replace').strip().splitlines()
                        if '[ui]' in ln]
                    why = (tail[-1].split('[ui]')[-1].strip() if tail
                           else _tr('Refused'))
                    _btn.text = _tr('Not allowed there')
                    _show_info_popup(
                        _tr('Cannot open that device'), why,
                        font_name=font_name)
                else:
                    _btn.text = _tr('Window closed')
            except subprocess.TimeoutExpired:
                _btn.text = _tr('Opened — see REMOTE window')
        except Exception as ex:
            _btn.text = str(ex)[:40]
        finally:
            _btn.disabled = False

    open_btn.bind(on_release=_open_remote)

    # (0.55.138 added an on-screen hint label here explaining why the
    # button was absent; removed in 0.55.140 — it described a non-event on
    # every peer nobody wants to control. The reason is logged instead.)

    # SHOWN ONLY ONCE THAT DEVICE HAS EVER SAID YES (0.55.130).
    #
    # Kent: *"I can still see a settings button on every screen, even when
    # unuseful."* Right — 0.55.125 made it unconditional, which put a
    # button on every peer including the ones that had granted nothing.
    #
    # The version he actually asked for two steps earlier: *"can we not
    # hide it until at least one OK ping on the permission?"* — hidden
    # until confirmed once, then STICKY, so it doesn't come and go with
    # reachability. ``they_admin_us`` (peers.json) is that memory: set on
    # the first successful admin call, cleared only by an explicit refusal,
    # never by a device merely being asleep.
    #
    # No probe on popup open, so nothing flickers and nothing costs a
    # network round trip here.
    def _reveal_open_btn(*_a):
        open_btn.height = dp(44)
        open_btn.opacity = 1
        open_btn.disabled = False

    if not bool(peer.get('they_admin_us')):
        open_btn.height = 0
        open_btn.opacity = 0
        open_btn.disabled = True

        # BOOTSTRAP (0.55.131). Without this the feature can never start:
        # the sticky flag is set by a successful admin call, and the only
        # way to make one is this button, which is hidden until the flag is
        # set. A deadlock — Kent: *"how long should that take to appear?"*
        # Never, as 0.55.130 shipped.
        #
        # So probe once per popup open, in the background, and use it ONLY
        # to reveal — never to hide. That keeps 0.55.123's flicker away
        # (nothing ever disappears) while giving the first success a way to
        # happen. A hit also persists the flag daemon-side, so every later
        # visit shows the button immediately with no probe at all.
        def _bootstrap():
            try:
                from .. import lan_can_admin
                if (lan_can_admin(pid) or {}).get('allowed'):
                    Clock.schedule_once(_reveal_open_btn, 0)
            except Exception:
                pass        # unreachable is not a no; stay hidden quietly

        threading.Thread(target=_bootstrap, daemon=True).start()

    # Static endpoint field — comma-separated 'ip:port' list.
    content.add_widget(Label(
        text=_tr('Manual IP / port (comma-separated)'),
        size_hint_y=None, height=dp(24), font_size=sp(12),
        bold=True, font_name=font_name))
    # The label alone assumed you already knew what belongs here and
    # where to find it — field 2026-07-25: "no idea how to use this;
    # haven't so far". Say whose address it is, and name the screen
    # to copy it from. Optional, not required: discovery tries mDNS
    # first and a manual entry is only a fallback, so this is for the
    # case where a device never announces (or announces too late) on
    # a tether link.
    content.add_widget(Label(
        text=_tr('Optional. Its own “Listening on…” address.'),
        size_hint_y=None, height=dp(22), font_size=sp(10),
        color=theme.TEXT_DIM, font_name=font_name,
        halign='left', valign='middle',
        text_size=(dp(320), dp(20))))
    endpoints_field = TextInput(
        text=', '.join(peer.get('static_endpoints') or []),
        hint_text='192.168.42.129:34501',
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

    # Remote settings, BELOW the sharing/addressing controls (0.55.139).
    # Those govern a device's ordinary participation; these govern whether
    # it may reconfigure this machine — closer in kind to diagnostics and
    # Forget, which follow.
    content.add_widget(admin_btn)
    content.add_widget(open_btn)

    # Diagnostics pull (0.54.74). Field workflow: the operator is on
    # SOMEONE ELSE'S machine, which normally has no collaboration UI
    # open — booting it to press their Share-diagnostics button
    # interrupts their work. Pulling collects the same bundle from
    # here instead; pairing was their consent. Also carries the
    # remote-restart escape hatch for a wedged peer daemon.
    diag_status = Label(
        text='', size_hint_y=None, height=dp(0), font_size=sp(11),
        color=theme.TEXT_DIM, font_name=font_name, opacity=0)
    diag_btn = Button(
        text=_tr('Get diagnostics from this device'),
        size_hint_y=None, height=dp(44),
        font_size=sp(13), font_name=font_name)
    content.add_widget(diag_btn)
    content.add_widget(diag_status)
    # Holder so a runtime-added affordance (the restart escape hatch)
    # lands HERE, next to its status line — ``content.add_widget``
    # at that point would append below Close.
    diag_extra = BoxLayout(orientation='vertical', size_hint_y=None,
                           height=dp(0), spacing=dp(4))
    content.add_widget(diag_extra)

    def _say(msg, height=dp(34)):
        diag_status.text = msg
        diag_status.height = height
        diag_status.opacity = 1 if msg else 0

    def _offer_share(items):
        """Hand the landed bundle to the platform share sheet so it
        can leave this device (email / messaging / file manager).
        Single item → ``share_files`` routes it as ACTION_SEND, which
        is the only shape some receivers accept for non-media."""
        from .share import share_files
        from ..diagnostics import DIAGNOSTICS_MIME
        from ..transports.android_cp import CANONICAL_AUTHORITY
        uri_items = [
            {'uri': f'content://{CANONICAL_AUTHORITY}/'
                    f'{it.get("uri_path", "")}',
             'display_name': it.get('display_name', '')}
            for it in items
            if it.get('uri_path') and it.get('display_name')]
        if not uri_items:
            return
        share_files(items=uri_items, mime_type=DIAGNOSTICS_MIME,
                    on_error=lambda m: _say(m, dp(46)))

    def _pull(_btn=None, allow_restart=True):
        from .. import lan_pull_diagnostics, S
        from ..translate import translate_result
        diag_extra.clear_widgets()
        diag_extra.height = dp(0)
        diag_btn.disabled = True
        _say(_tr('Collecting from {device}…').format(
            device=peer.get('device_name') or _tr('that device')))

        def _worker():
            # Off the main thread: the peer builds + gzips its bundle
            # inline before the first response byte, which on a slow
            # field machine is many seconds. A main-thread RPC here
            # would land as an OS "not responding" dialog (the
            # 0.54.69 lesson).
            result, items = lan_pull_diagnostics(pid)

            def _land(_dt):
                diag_btn.disabled = False
                msg = translate_result(result)
                if result.has(S.LAN_PULL_DONE):
                    _say(msg or _tr('Diagnostics collected.'), dp(34))
                    _offer_share(items)
                    return
                _say(msg or _tr('Could not collect diagnostics.'),
                     dp(58))
                # Wedge escape hatch: a peer whose listener answers
                # but whose worker threads are stuck can be restarted
                # from here — that is what the operator would
                # otherwise have opened the owner's UI to do. Offered
                # once per attempt, never automatic: a restart is
                # cheap but it is still someone else's machine.
                if (allow_restart
                        and result.has_any(
                            S.LAN_PULL_FAILED,
                            S.LAN_PULL_PEER_UNREACHABLE)):
                    _offer_restart()

            Clock.schedule_once(_land, 0)

        threading.Thread(target=_worker, daemon=True,
                         name='lan-pull-diagnostics').start()

    def _offer_restart():
        diag_extra.clear_widgets()
        diag_extra.height = dp(34)
        restart_btn = _link_button(
            _tr('Restart their service and retry'),
            size_hint_y=None, height=dp(34), font_size=sp(12),
            font_name=font_name)

        def _do_restart(*_):
            from .. import lan_restart_peer, S
            from ..translate import translate_result
            restart_btn.disabled = True
            _say(_tr('Asking that device to restart its service…'))

            def _worker():
                res = lan_restart_peer(pid)

                def _land(_dt):
                    msg = translate_result(res)
                    _say(msg or '', dp(58))
                    if res.has(S.LAN_RESTART_SENT):
                        # Give the peer time to come back up, then
                        # retry once. No second restart offer — an
                        # endless restart/retry loop on someone
                        # else's machine is not acceptable.
                        Clock.schedule_once(
                            lambda _t: _pull(allow_restart=False), 8)

                Clock.schedule_once(_land, 0)

            threading.Thread(target=_worker, daemon=True,
                             name='lan-restart-peer').start()

        restart_btn.bind(on_release=_do_restart)
        diag_extra.add_widget(restart_btn)

    diag_btn.bind(on_release=lambda _b: _pull())

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

    # KEEP LOOKING WHILE THIS POPUP IS OPEN (0.55.134). The bootstrap probe
    # (0.55.131) ran once at build time, so granting on the OTHER device
    # after opening this had nothing to notice it — Kent: *"after I went out
    # and came back it did, but not on just polling."* Correct, and closing
    # and reopening to see a permission you just granted is silly.
    #
    # Re-probe on a slow timer, REVEAL ONLY (so nothing can flicker), and
    # stop the moment it succeeds — the daemon persists the grant, so one
    # success is the last probe this peer ever needs. Cancelled on dismiss
    # so a closed popup leaves no timer behind.
    _probe_ev = {'ev': None}
    _said = {}          # last reason logged, so repeats stay quiet

    def _stop_probe(*_a):
        if _probe_ev['ev'] is not None:
            _probe_ev['ev'].cancel()
            _probe_ev['ev'] = None

    def _tick_probe(_dt):
        def _work():
            try:
                from .. import lan_can_admin
                res = lan_can_admin(pid) or {}
            except Exception as ex:
                # NEVER SWALLOW THIS (0.55.145). ``except Exception:
                # return`` here is why an evening went into finding why a
                # button never appeared: the grant persisted, the challenge
                # was served, the canonical form matched — and the actual
                # failure was thrown away without a word, in the one place
                # that knew it.
                print(f'[lan-admin-ui] permission probe for '
                      f'{str(pid)[:8]} raised: {ex!r}',
                      file=sys.stderr, flush=True)
                return
            if res.get('allowed'):
                Clock.schedule_once(
                    lambda _d: (_reveal_open_btn(), _stop_probe()), 0)
                return

            # LOG IT, DON'T DISPLAY IT (0.55.140). 0.55.138 put the reason
            # on screen; Kent: *"the button doesn't show, and shouldn't,
            # without permission. there is no way to ask a peer for
            # permission … why would anyone expect an answer in this
            # case?"*
            #
            # Right. A device you never granted has no button, that is
            # unremarkable, and a line explaining the absence would appear
            # on every peer you will never want to control — text about a
            # non-event, leading nowhere, since no "request permission"
            # flow exists.
            #
            # The case that IS confusing is a pair you DID grant where the
            # button still doesn't appear. That is diagnosis, so it goes to
            # the log where diagnosis lives.
            # ONCE PER ANSWER, not once per tick (0.55.141). The probe
            # repeats every 6 s while this popup is open — 0.55.140 would
            # have logged on every one of them. Kent: *"will I see this each
            # time I open that page"* — you would have seen it ten times a
            # minute.
            #
            # The reason only matters when it CHANGES; a steady "refused" is
            # the same fact restated. Same rule as the watchdog's in-flight
            # line: suppress the repeat, keep the transition.
            _r = str(res.get('reason', '?'))
            if _said.get('reason') != _r:
                _said['reason'] = _r
                # ``[lan-admin-ui]``, not ``[lan-admin-client]`` (0.55.142).
                # The latter is the DAEMON module's prefix
                # (azt_collabd/lan_admin_client.py); reusing it here made a
                # UI-process line look like a daemon one, so "do I need to
                # restart the server for this?" had no answer from the log.
                # One prefix per process.
                print(f'[lan-admin-ui] no remote-settings button for '
                      f'{str(pid)[:8]}: {_r} '
                      f'({str(res.get("detail", ""))[:120]})',
                      file=sys.stderr, flush=True)
        threading.Thread(target=_work, daemon=True).start()

    if not bool(peer.get('they_admin_us')):
        _probe_ev['ev'] = Clock.schedule_interval(_tick_probe, 6.0)
        popup.bind(on_dismiss=_stop_probe)

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
    """List paired peers + mDNS-discovered unpaired peers.

    Two stacked sections:

    - **Nearby (unpaired)** — mDNS-discovered devices not in our
      ``peers.json``. Each row has a Pair button that fires
      ``lan_pair_request_send``; the row then polls
      ``lan_pair_request_status`` until accepted / declined /
      timeout. On accept the peer moves into the Paired section
      automatically on the next refresh.
    - **Paired** — devices already in ``peers.json``. Tap Manage
      for the per-peer settings, Unpair to remove.

    Polling: each in-flight pair-request gets a Clock event
    polling status every 2 s. Events are tracked in
    ``_active_polls`` and cancelled on popup dismiss so we don't
    leak Clock callbacks after the user closes the screen.
    """
    from .. import (
        lan_list_peers, lan_nearby_unpaired, lan_peer_id,
        lan_toggle, list_projects,
        lan_pair_request_send, lan_pair_request_status,
        translate_result, S,
    )

    _container_padding = dp(10)
    _container_spacing = dp(8)
    _btn_row_h = dp(44)
    # Chrome the popup adds around its content (title bar + borders);
    # eyeballed from the ThemedPopup defaults. ``size_hint=(_, None)``
    # plus an explicit ``height`` includes the title bar, so add it.
    _popup_chrome = dp(60)

    container = BoxLayout(orientation='vertical',
                          spacing=_container_spacing,
                          padding=_container_padding)

    # Three sections — each is a vertical BoxLayout whose first
    # child is the section header and whose remaining children are
    # the rows (or a placeholder when empty). All three headers
    # render unconditionally so the user can see at a glance which
    # bucket is empty.
    this_phone_box = BoxLayout(orientation='vertical',
                               size_hint_y=None, spacing=dp(4))
    this_phone_box.bind(
        minimum_height=this_phone_box.setter('height'))
    nearby_box = BoxLayout(orientation='vertical', size_hint_y=None,
                           spacing=dp(4))
    nearby_box.bind(minimum_height=nearby_box.setter('height'))
    paired_box = BoxLayout(orientation='vertical', size_hint_y=None,
                           spacing=dp(4))
    paired_box.bind(minimum_height=paired_box.setter('height'))

    list_box = BoxLayout(orientation='vertical', size_hint_y=None,
                         spacing=dp(12))
    list_box.bind(minimum_height=list_box.setter('height'))
    list_box.add_widget(this_phone_box)
    list_box.add_widget(nearby_box)
    list_box.add_widget(paired_box)
    scroll = ScrollView(size_hint_y=None)
    scroll.add_widget(list_box)
    container.add_widget(scroll)

    btn_row = BoxLayout(orientation='horizontal', spacing=dp(8),
                        size_hint_y=None, height=_btn_row_h)
    refresh_btn = Button(
        text=_tr('Refresh'), size_hint_x=None, width=dp(120),
        font_size=sp(14), font_name=font_name)
    close_btn = Button(
        text=_tr('Close'), font_size=sp(14), font_name=font_name)
    btn_row.add_widget(refresh_btn)
    btn_row.add_widget(close_btn)
    container.add_widget(btn_row)

    # Shrink-to-fit (0.50.40): the previous ``size_hint=(0.95, 0.9)``
    # parked the popup at 90% of screen height regardless of content,
    # leaving most of the dialog empty when the user had only a
    # paired phone or two. Now the popup sizes itself to content,
    # capped at 90% of the screen so a long list still scrolls.
    popup = Popup(title=_tr('Nearby & paired devices'),
                  content=container, size_hint=(0.95, None),
                  auto_dismiss=False)

    def _resize_to_content(*_args):
        # The viewport must be capped at the space actually available
        # inside a 90%-of-window popup — NOT set to the full content
        # height. If scroll.height == list_box.height the ScrollView is
        # exactly as tall as its child, so there's nothing to scroll and
        # the overflow is shoved off the (capped) popup. Capping the
        # viewport BELOW content height is what lets it scroll.
        # (Pre-0.54.33 this was ``max(list_box.height, dp(80))`` — full
        # content height — so a long list never scrolled: it just ran
        # off the page. Field 2026-07-23.)
        max_popup = Window.height * 0.9
        avail_scroll = (max_popup - _popup_chrome - _btn_row_h
                        - _container_spacing - _container_padding * 2)
        avail_scroll = max(avail_scroll, dp(80))
        scroll.height = max(min(list_box.height, avail_scroll), dp(80))
        # Body = list + buttons + inter-child spacing + top/bottom
        # padding (no inner title widget — the popup chrome shows
        # it once).
        body = (scroll.height + _btn_row_h
                + _container_spacing
                + _container_padding * 2)
        popup.height = min(body + _popup_chrome, max_popup)

    def _on_window_resize(*_a):
        _resize_to_content()

    list_box.bind(height=_resize_to_content)
    Window.bind(height=_on_window_resize)

    # Per-peer Clock events tracking outbound pair-request polls.
    # Mapped by peer_id → Kivy event handle; cancelled on popup
    # dismiss so closing the screen mid-poll doesn't leak.
    _active_polls = {}

    def _cancel_all_polls():
        for ev in list(_active_polls.values()):
            try:
                ev.cancel()
            except Exception:
                pass
        _active_polls.clear()

    def _confirm_unpair(peer):
        """Confirm dialog before calling ``lan_unpair``.
        Destructive: removes the peer from ``peers.json``, drops
        cached endpoint, drops any shared-project allowlist
        entries for them. Re-pairing requires scanning a fresh
        QR."""
        from .. import lan_unpair
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
        btn_row_ = BoxLayout(orientation='horizontal',
                             spacing=dp(8),
                             size_hint_y=None, height=dp(44))
        cancel_btn = Button(text=_tr('Cancel'), font_name=font_name)
        do_btn = Button(text=_tr('Unpair'), font_name=font_name)
        btn_row_.add_widget(cancel_btn)
        btn_row_.add_widget(do_btn)
        body.add_widget(btn_row_)
        confirm_popup = Popup(
            title=_tr('Confirm unpair'),
            content=body, size_hint=(0.85, None), height=dp(240),
            auto_dismiss=False)

        def _do_it(*_):
            confirm_popup.dismiss()
            try:
                lan_unpair(pid)
            except Exception as ex:
                print(f'[lan-unpair] {pid[:8]!r} failed: {ex!r}',
                      file=sys.stderr, flush=True)
            _refresh()

        cancel_btn.bind(on_release=lambda *_: confirm_popup.dismiss())
        do_btn.bind(on_release=_do_it)
        confirm_popup.open()

    def _build_nearby_row(entry):
        """One row in the Nearby (unpaired) list: name / full peer
        id / endpoint, plus a Pair button. The button mutates in
        place once tapped: Pair → "Waiting…" while the poll runs."""
        pid = entry.get('peer_id', '') or ''
        device_name = entry.get('device_name') or ''
        endpoint = entry.get('endpoint', '') or ''
        primary = device_name or (
            _tr('Device {id}…').format(id=pid[:8])
            if pid else _tr('Unnamed device'))

        action_btn = Button(
            text=_tr('Pair'),
            size_hint=(None, None), width=dp(96), height=dp(40),
            font_size=sp(12), font_name=font_name)

        def _set_waiting():
            action_btn.text = _tr('Waiting…')
            action_btn.disabled = True

        def _set_pair():
            action_btn.text = _tr('Pair')
            action_btn.disabled = False

        def _poll(_dt):
            state = lan_pair_request_status(pid)
            if state == 'pending':
                return  # keep polling
            ev = _active_polls.pop(pid, None)
            if ev is not None:
                try:
                    ev.cancel()
                except Exception:
                    pass
            if state == 'accepted':
                # Peer moved to paired list — rebuild.
                _refresh()
                return
            # declined / timeout / none — surface briefly, revert.
            if state == 'declined':
                msg = _tr(
                    '{device_name} declined the pair request.'
                ).format(device_name=primary)
            elif state == 'timeout':
                msg = _tr(
                    'Pair request to {device_name} timed out.'
                ).format(device_name=primary)
            else:
                msg = _tr('Pair request lost.')
            action_btn.text = msg
            action_btn.disabled = True
            # Restore the Pair button after a short delay so the
            # user has time to read the message.
            Clock.schedule_once(lambda _t: _set_pair(), 3.0)

        def _on_pair(*_):
            if not pid:
                return
            _set_waiting()

            def _work():
                # Off the UI thread: the send dials the peer's
                # listener and can block for seconds — running it on
                # the Kivy main thread froze the whole screen ("the
                # button won't push", field 2026-07-24). Same disease
                # as the dead-buttons item.
                try:
                    result = lan_pair_request_send(pid, '')
                except Exception:
                    result = None

                def _land(_dt):
                    if (result is not None
                            and result.has(S.LAN_PAIR_REQUEST_PENDING)):
                        # Poll every 2 s; daemon's outbound state
                        # clears on read after a terminal state, so
                        # we'll see accepted/declined/timeout exactly
                        # once.
                        ev = Clock.schedule_interval(_poll, 2.0)
                        _active_polls[pid] = ev
                        return
                    # Anything else is a failure to even send the
                    # request — show the translated reason and
                    # revert. Common cases: LAN_TOGGLE_OFF,
                    # LAN_PEER_UNREACHABLE, SERVER_*.
                    text = ((translate_result(result)
                             if result is not None else '')
                            or _tr('Could not send pair request.'))
                    action_btn.text = text[:40]
                    Clock.schedule_once(lambda _t: _set_pair(), 3.0)
                Clock.schedule_once(_land, 0)

            threading.Thread(target=_work, daemon=True,
                             name='lan-pair-send').start()

        action_btn.bind(on_release=_on_pair)
        return _build_full_row(
            name=primary, peer_id=pid, endpoint=endpoint,
            projects='', buttons=[action_btn],
            font_name=font_name)

    def _placeholder(text):
        """Single-line dim helper shown when a section is empty,
        so the three section headers stay anchored even with no
        peers to list."""
        lbl = Label(
            text=text, halign='left', valign='middle',
            size_hint_y=None, height=dp(28),
            font_size=sp(11), color=theme.TEXT_DIM,
            font_name=font_name)
        lbl.bind(width=lambda w, *_: setattr(
            w, 'text_size', (w.width, dp(28))))
        return lbl

    def _refresh():
        # Cancel any in-flight polls before we wipe the rows
        # holding their button refs.
        _cancel_all_polls()
        this_phone_box.clear_widgets()
        nearby_box.clear_widgets()
        paired_box.clear_widgets()

        nearby = lan_nearby_unpaired() or []
        paired = lan_list_peers() or []

        # --- This phone ----------------------------------------
        # Same row format as the peers below so a third party in
        # the room can read off name / uid / IP / projects without
        # having to interpret a different layout for "us."
        my_info = lan_peer_id() or {}
        my_toggle = lan_toggle() or {}
        my_projects = ', '.join(
            p.langcode for p in (list_projects() or []))
        this_phone_box.add_widget(_section_header(
            _tr('This phone'), font_name))
        this_phone_box.add_widget(_build_full_row(
            name=my_info.get('device_name') or _tr('this device'),
            peer_id=my_info.get('peer_id', '') or '',
            endpoint=my_toggle.get('endpoint', '') or '',
            projects=my_projects, buttons=None,
            font_name=font_name))

        # --- Unpaired (nearby) ---------------------------------
        nearby_box.add_widget(_section_header(
            _tr('Unpaired'), font_name))
        if nearby:
            for entry in nearby:
                nearby_box.add_widget(_build_nearby_row(entry))
        else:
            nearby_box.add_widget(_placeholder(
                _tr('No nearby phones detected. Tap Refresh after a '
                    'few seconds.')))

        # --- Paired --------------------------------------------
        paired_box.add_widget(_section_header(
            _tr('Paired'), font_name))
        if paired:
            for peer in paired:
                shared = ', '.join(
                    peer.get('shared_projects') or []) or \
                    _tr('(no projects shared)')
                manage_btn = Button(
                    text=_tr('Manage'),
                    size_hint=(None, None),
                    width=dp(96), height=dp(40),
                    font_size=sp(12), font_name=font_name)
                manage_btn.bind(
                    on_release=lambda *_args, p=peer:
                        _manage_peer_popup(
                            p, on_refresh=_refresh,
                            font_name=font_name))
                unpair_btn = Button(
                    text=_tr('Unpair'),
                    size_hint=(None, None),
                    width=dp(96), height=dp(40),
                    font_size=sp(12), font_name=font_name)
                unpair_btn.bind(
                    on_release=lambda *_args, p=peer:
                        _confirm_unpair(p))
                row_buttons = [manage_btn, unpair_btn]
                # Passive pending-offer affordance: a red button that
                # opens the affirm/decline confirm for the first
                # pending offer from this peer (or a count if >1).
                offers = _pending_offers_for(peer.get('peer_id', ''))
                if offers:
                    first_lang = str(
                        (offers[0].get('params') or {}).get(
                            'langcode', '') or '')
                    offer_label = (
                        _tr('{n} pending').format(n=len(offers))
                        if len(offers) > 1
                        else _tr('{project} pending').format(
                            project=first_lang))
                    # Plain red text link, not a filled button —
                    # "bit much" (field 2026-07-23).
                    offer_btn = _link_button(
                        offer_label,
                        size_hint=(None, None),
                        width=dp(96), height=dp(40),
                        font_size=sp(11), font_name=font_name)
                    offer_btn.bind(
                        on_release=lambda *_args, d=offers[0]:
                            _offer_confirm_popup(
                                d, on_done=_refresh,
                                font_name=font_name))
                    row_buttons.append(offer_btn)
                paired_box.add_widget(_build_full_row(
                    name=peer.get('device_name')
                        or _tr('Unnamed device'),
                    peer_id=peer.get('peer_id', '') or '',
                    endpoint=_peer_endpoint_str(peer),
                    projects=shared,
                    buttons=row_buttons,
                    font_name=font_name))
        else:
            paired_box.add_widget(_placeholder(
                _tr('No phones paired yet. Use "Pair a phone" to '
                    'scan another phone\'s QR.')))

    # Offers watcher: cheap ``lan_pending`` poll (3 s); rebuilds the
    # rows ONLY when the pending share-offer id set changes — e.g. an
    # affirmed offer auto-completed on the peer's arrival, or a fresh
    # offer landed — so the red "{project} pending" link clears or
    # appears without closing/reopening the screen. Kept OUTSIDE
    # ``_active_polls`` because ``_refresh`` cancels those wholesale.
    from .. import lan_pending as _lan_pending_rpc
    _offers_watch = {'ev': None, 'ids': None}

    def _poll_offers(_dt):
        try:
            ids = sorted(
                d.get('id', '') for d in (_lan_pending_rpc() or [])
                if d.get('kind') == 'share_offer')
        except Exception:
            return
        if _offers_watch['ids'] is None:
            _offers_watch['ids'] = ids
            return
        if ids != _offers_watch['ids']:
            _offers_watch['ids'] = ids
            _refresh()

    _offers_watch['ev'] = Clock.schedule_interval(_poll_offers, 3.0)

    def _on_close(*_):
        _cancel_all_polls()
        ev = _offers_watch.get('ev')
        if ev is not None:
            try:
                ev.cancel()
            except Exception:
                pass
        try:
            Window.unbind(height=_on_window_resize)
        except Exception:
            pass
        popup.dismiss()

    _refresh()
    refresh_btn.bind(on_release=lambda *_: _refresh())
    close_btn.bind(on_release=_on_close)
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
    from .. import (
        lan_list_peers, lan_share_project, list_projects,
        translate_result, S,
    )
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

    title_text = (_tr('Share [{langcode}] project').format(
                      langcode=langcode)
                  if langcode else _tr('Share project'))

    # Outer container: scrollable body + fixed Close button at the
    # bottom. The body got bigger (this-phone row + larger QR) and
    # would otherwise clip on shorter screens.
    container = BoxLayout(orientation='vertical', spacing=dp(8),
                          padding=dp(12))

    body = BoxLayout(orientation='vertical', size_hint_y=None,
                     spacing=dp(10))
    body.bind(minimum_height=body.setter('height'))
    body_scroll = ScrollView(size_hint=(1, 1))
    body_scroll.add_widget(body)
    container.add_widget(body_scroll)

    # --- Section: This phone -----------------------------------
    # Mirrors the layout used in paired_phones_popup so the user
    # (and anyone in the room reading over their shoulder) sees
    # name / uid / IP / projects in the same format for "us" as
    # for "them."
    from .. import lan_peer_id, lan_toggle
    info = lan_peer_id() or {}
    toggle = lan_toggle() or {}
    my_projects = ', '.join(
        p.langcode for p in (list_projects() or []))
    body.add_widget(_section_header(
        _tr('This phone'), font_name))
    body.add_widget(_build_full_row(
        name=info.get('device_name') or _tr('this device'),
        peer_id=info.get('peer_id', '') or '',
        endpoint=toggle.get('endpoint', '') or '',
        projects=my_projects, buttons=None,
        font_name=font_name))

    # --- Section: Paired (with per-row share state) -----------
    # Always render the header so the user can see whether there
    # are any paired phones to share with; placeholder copy when
    # empty.
    #
    # Two states for the right column:
    #   - **not shared** — a "Share" button. Tap fires
    #     ``lan_share_project`` and the column rerenders into the
    #     shared state.
    #   - **shared** — a tappable composite (whole column is the
    #     target, not just a small link) reading "Shared" on top
    #     with an "Offer share again" hint underneath. Tap re-
    #     fires ``lan_share_project``. The flash text after a tap
    #     is driven by the typed :class:`Result` the daemon
    #     returns: "Already in sync." (receiver already had it),
    #     "Sent — waiting…" (receiver got a new pending decision),
    #     "Could not reach the other phone…" (POST didn't 2xx),
    #     or one of the configuration-error messages
    #     (PROJECT_UNBORN, LAN_TOGGLE_OFF, …).
    #
    # Dedup: each ``_TapBox`` tracks a per-row ``_in_flight`` flag
    # plus a 3 s cool-down after the flash text shows. Repeated
    # taps during the cool-down are absorbed — no second HTTPS
    # POST. Prevents accidental double-fire (finger bounce) and
    # spam-tapping when the link "doesn't seem to do anything."
    _DEDUP_SECONDS = 3.0

    def _render_share_unshared(p, col):
        col.clear_widgets()
        btn = Button(
            text=_tr('Share'),
            size_hint_y=None, height=dp(40),
            font_size=sp(12), font_name=font_name,
            background_color=(0.4, 0.4, 0.4, 1))

        def _do_share(*_):
            if not langcode:
                return
            pid = p.get('peer_id', '') or ''
            if not pid:
                return
            lan_share_project(langcode, pid)
            _render_share_shared(p, col)

        btn.bind(on_release=_do_share)
        col.add_widget(btn)

    def _render_share_shared(p, col):
        col.clear_widgets()

        tap = _TapBox(orientation='vertical',
                      size_hint_y=None, spacing=dp(2),
                      padding=(dp(4), dp(6)))
        tap.bind(minimum_height=tap.setter('height'))

        # Top label — bold accent "Shared." Also reads as the
        # main visual state, so visually distinct from a button.
        top = Label(
            text=_tr('Shared'),
            size_hint_y=None, height=dp(26),
            font_size=sp(13), bold=True,
            color=theme.ACCENT, font_name=font_name,
            halign='center', valign='middle')
        top.bind(width=lambda w, *_: setattr(
            w, 'text_size', (w.width, dp(26))))
        tap.add_widget(top)

        # Sub-line: the affordance text the user reads to know
        # the column does something. Underlined via Kivy markup
        # for a link-y feel; the actual hit area is the whole
        # tap box, so finger placement is forgiving.
        sub_default = ('[u]' + _tr('Tap to offer share again')
                       + '[/u]')
        sub = Label(
            text=sub_default, markup=True,
            size_hint_y=None, height=dp(22),
            font_size=sp(10), color=theme.ACCENT,
            font_name=font_name,
            halign='center', valign='middle')
        sub.bind(width=lambda w, *_: setattr(
            w, 'text_size', (w.width, dp(22))))
        tap.add_widget(sub)

        state = {'in_flight': False, 'cooldown': False}

        def _flash(message):
            """Show *message* for the dedup window in place of the
            sub-line; suppress further taps for the same window so
            the user can't queue a second POST while reading the
            outcome."""
            state['cooldown'] = True
            sub.text = message
            sub.color = theme.TEXT_DIM

            def _restore(_dt):
                state['cooldown'] = False
                sub.text = sub_default
                sub.color = theme.ACCENT

            Clock.schedule_once(_restore, _DEDUP_SECONDS)

        def _reoffer(*_args):
            if state['in_flight'] or state['cooldown']:
                return
            if not langcode:
                return
            pid = p.get('peer_id', '') or ''
            if not pid:
                return
            state['in_flight'] = True

            # The RPC is synchronous (waits on the HTTPS POST to
            # the peer's listener — up to 5 s connect + 10 s
            # read), so kick it onto a worker so the UI doesn't
            # freeze for a slow peer. The result lands back on
            # the main thread via Clock.
            def _worker():
                result = lan_share_project(langcode, pid)
                Clock.schedule_once(
                    lambda _t: _on_done(result), 0)

            def _on_done(result):
                state['in_flight'] = False
                # Pick the most-relevant Status to translate.
                # Order matters: prefer the typed daemon refusals
                # (which mean "didn't even try") over the
                # transport codes.
                preference = (
                    S.LAN_TOGGLE_OFF, S.CONTRIBUTOR_UNSET,
                    S.PEER_UNKNOWN, S.PROJECT_NOT_INITIALISED,
                    S.PROJECT_UNBORN,
                    S.LAN_OFFER_NOT_DELIVERED,
                    S.LAN_OFFER_DELIVERED,
                    S.SERVER_ERROR, S.SERVER_UNAVAILABLE,
                )
                msg = ''
                for code in preference:
                    if result.has(code):
                        # ``translate_result`` walks the whole
                        # result; passing a single-status
                        # narrow Result lets us pin the message
                        # we want.
                        from ..status import Result as _R
                        for s in result.statuses:
                            if s.code == code:
                                msg = translate_result(_R(
                                    statuses=[s])) or ''
                                break
                        if msg:
                            break
                if not msg:
                    msg = _tr('Offer sent.')
                _flash(msg)

            threading.Thread(target=_worker, daemon=True,
                             name='lan-share-reoffer').start()

        tap.bind(on_release=_reoffer)
        col.add_widget(tap)

    peers = lan_list_peers() or []
    body.add_widget(_section_header(_tr('Paired'), font_name))
    if peers:
        for peer in peers:
            shared_list = peer.get('shared_projects') or []
            is_shared = bool(langcode and langcode in shared_list)
            right = BoxLayout(
                orientation='vertical',
                size_hint=(None, None),
                width=dp(140), spacing=dp(4),
                padding=(0, dp(4)))
            right.bind(minimum_height=right.setter('height'))
            if is_shared:
                _render_share_shared(peer, right)
            else:
                _render_share_unshared(peer, right)
            body.add_widget(_build_full_row(
                name=peer.get('device_name')
                    or _tr('Unnamed device'),
                peer_id=peer.get('peer_id', '') or '',
                endpoint=_peer_endpoint_str(peer),
                projects=', '.join(shared_list)
                         or _tr('(no projects shared)'),
                right_widget=right, font_name=font_name))
    else:
        empty = Label(
            text=_tr('No phones paired yet. Show the QR below to '
                     'pair a phone now.'),
            halign='left', valign='middle',
            size_hint_y=None, height=dp(28),
            font_size=sp(11), color=theme.TEXT_DIM,
            font_name=font_name)
        empty.bind(width=lambda w, *_: setattr(
            w, 'text_size', (w.width, dp(28))))
        body.add_widget(empty)

    # --- Section: Pair a new phone (inline QR) -----------------
    from .. import lan_pair_qr
    body.add_widget(_section_header(
        _tr('Pair a new phone'), font_name))
    if not info.get('peer_id'):
        err = info.get('error', '') or _tr('unknown')
        body.add_widget(Label(
            text=_tr('LAN identity is not available on this device.'),
            size_hint_y=None, height=dp(28),
            font_size=sp(12), color=theme.TEXT_DIM,
            font_name=font_name))
        body.add_widget(Label(
            text=str(err), size_hint_y=None, height=dp(22),
            font_size=sp(10), color=theme.TEXT_DIM,
            font_name=font_name))
    else:
        if not toggle.get('on'):
            body.add_widget(Label(
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
            # Bigger QR (0.50.41): scale 6 → 8 makes each module
            # easier for a phone camera to lock on at arm's length;
            # height 200 → 320 gives it real estate proportional to
            # how often this is the actual share gesture.
            qr_widget, qr_err = _render_qr_widget(
                qr_payload, scale=8, border=2)
            if qr_widget is not None:
                qr_widget.size_hint_y = None
                qr_widget.height = dp(320)
                body.add_widget(qr_widget)
            else:
                body.add_widget(Label(
                    text=qr_err, size_hint_y=None, height=dp(28),
                    font_size=sp(11), color=theme.TEXT_DIM,
                    font_name=font_name))
            endpoint_text = qr_payload.get('endpoint', '')
            body.add_widget(Label(
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
            body.add_widget(Label(
                text=_tr('Could not generate pairing QR.'),
                size_hint_y=None, height=dp(28),
                font_size=sp(11), color=theme.TEXT_DIM,
                font_name=font_name))

    # --- Section: add github collaborator ----------------------
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
        body.add_widget(_section_header(
            _tr('Add permission by github username'), font_name))
        invite_btn = Button(
            text=_tr('Invite someone who isn\'t here'),
            size_hint_y=None, height=dp(44),
            font_size=sp(13), font_name=font_name)
        invite_btn.bind(
            on_release=lambda *_: grant_collaborator_popup(
                langcode, font_name=font_name))
        body.add_widget(invite_btn)

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
