"""
Kivy popups for the LAN sync transport (parked design phase 8 / UI).

Three flows live here:

  - ``share_pairing_qr_popup`` — renders our daemon's pairing QR
    as an image (segno) so another phone can scan it to pair.
  - ``scan_to_pair`` — picker entry point. Launches the existing
    zxing-android-embedded scanner, decodes the JSON payload, and
    calls ``lan_pair_accept`` on the local daemon.
  - ``paired_phones_popup`` — settings-UI list of paired peers with
    per-peer manage actions (share/unshare projects, set static
    endpoint, unpair).

All three live in the shared client package because both the daemon
settings UI (``azt_collabd.ui.app``) and the picker (peer-side
"Scan to pair" entry) consume them. Translations route through
``azt_collab_client.translate.tr``.
"""

from __future__ import annotations

import io
import json
import sys

from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.image import Image
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput

from . import theme
from ..translate import tr as _tr


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


def share_pairing_qr_popup(font_name='Roboto'):
    """Open a popup that shows this daemon's pairing QR. The other
    phone scans it with ``scan_to_pair``. Top of the popup shows the
    device name + peer-id prefix so the user can verbally confirm
    across the table before accepting the pair on the other side."""
    from .. import lan_peer_id, lan_toggle

    content = BoxLayout(orientation='vertical', spacing=dp(10),
                        padding=dp(12))

    info = lan_peer_id()
    toggle = lan_toggle()
    if not info.get('peer_id'):
        content.add_widget(Label(
            text=_tr('LAN identity is not available on this device.'),
            font_size=sp(13), font_name=font_name))
    else:
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
        # auto-populates the endpoint from the running listener
        # when present.
        from .. import lan_pair_qr
        payload = lan_pair_qr()
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
    """Picker entry: launch the QR scanner, decode the pairing
    payload, call ``lan_pair_accept`` on the local daemon. Calls
    ``on_done(result)`` with the ``Result`` returned by
    ``lan_pair_accept`` (or a synthetic ``Result`` carrying
    ``SERVER_ERROR`` if anything upstream failed).

    No-op + ``on_status`` toast if QR scanning isn't available on
    this platform; callers should gate visibility of the entry
    point on ``qr_scan.available()`` to avoid that path."""
    from . import qr_scan
    from .. import lan_pair_accept, S
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

    def _on_result(text):
        try:
            payload = json.loads(text)
        except Exception as ex:
            _emit_status(_tr('Scanned data is not valid pairing '
                             'JSON.'))
            print(f'[scan_to_pair] payload parse failed: {ex!r}',
                  file=sys.stderr, flush=True)
            if on_done is not None:
                on_done(Result(statuses=[Status(
                    'SERVER_ERROR',
                    {'error': f'json decode failed: {ex}'})]))
            return
        result = lan_pair_accept(payload)
        if on_done is not None:
            on_done(result)

    def _on_cancel():
        _emit_status(_tr('Pairing cancelled.'))

    qr_scan.scan_qr(on_result=_on_result, on_cancel=_on_cancel,
                    prompt=_tr('Scan the pairing QR shown on the '
                               'other phone.'))


def _build_peer_row(peer, on_manage, font_name):
    """One row in the paired-peers list."""
    row = BoxLayout(orientation='horizontal', size_hint_y=None,
                    height=dp(56), spacing=dp(8), padding=dp(4))
    label_box = BoxLayout(orientation='vertical')
    name = peer.get('device_name') or _tr('Unnamed device')
    label_box.add_widget(Label(
        text=name, halign='left', valign='middle',
        size_hint_y=None, height=dp(28),
        font_size=sp(13), bold=True, font_name=font_name,
        text_size=(dp(220), dp(28))))
    pid = peer.get('peer_id', '')
    shared = ', '.join(peer.get('shared_projects') or []) or \
        _tr('(no projects shared)')
    label_box.add_widget(Label(
        text=f'{pid[:8]}… · {shared}',
        halign='left', valign='middle',
        size_hint_y=None, height=dp(22),
        font_size=sp(10), color=theme.TEXT_DIM,
        text_size=(dp(220), dp(22)),
        font_name=font_name))
    row.add_widget(label_box)
    manage_btn = Button(text=_tr('Manage'), size_hint=(None, None),
                        width=dp(96), height=dp(40),
                        font_size=sp(13), font_name=font_name)
    manage_btn.bind(on_release=lambda *_: on_manage(peer))
    row.add_widget(manage_btn)
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
                font_name=font_name))

    _refresh()
    close_btn.bind(on_release=lambda *_: popup.dismiss())
    popup.open()
    return popup
