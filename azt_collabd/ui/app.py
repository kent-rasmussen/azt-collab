"""
Standalone Kivy app for the A-Z+T collab server.

Three screens:
    settings  — credentials status + host toggle + Connect/Disconnect.
    github    — GitHub device flow: shows user_code, opens browser,
                polls until authorized, saves tokens through the
                client (server owns the credentials store).
    gitlab    — GitLab username + PAT form.
    projects  — read-only list of registered projects with last-sync.

Launched with::

    python -m azt_collabd ui

The UI process talks to the daemon through azt_collab_client; the
daemon is auto-spawned on first call.
"""

import datetime
import os
import threading
import webbrowser

from kivy.app import App
from kivy.clock import Clock
from kivy.lang import Builder
from kivy.metrics import dp, sp
from kivy.properties import StringProperty
from kivy.uix.button import Button
from kivy.uix.screenmanager import (
    NoTransition, Screen, ScreenManager, SlideTransition,
)

import azt_collab_client
from azt_collab_client import i18n as _client_i18n
from azt_collab_client.ui import (
    check_for_update, icon_path, register_charis, share_running_apk,
    theme,
)

import azt_collabd
from azt_collabd.status import AuthError
from azt_collab_client import (
    S,
    cawl_cache_status,
    get_contributor,
    get_credentials_status,
    github_app_install_url,
    init_project,
    is_online,
    last_project,
    mark_github_app_installed,
    open_project,
    project_status,
    save_github_tokens,
    save_gitlab_credentials,
    test_github_credentials,
    test_gitlab_credentials,
    set_contributor,
    get_cawl_prefetch_all_variants,
    set_cawl_prefetch_all_variants,
    translate_result,
    translate_status,
)
from azt_collab_client._debug import first_try_log


_tr = _client_i18n._

# Cold-start credentials re-poll budget (settings screen). On launch
# the daemon (:provider process on Android) is still spawning, so the
# first get_credentials_status returns the unreachable fallback; we
# re-poll until it answers, holding the presplash until then. Budget:
# _CRED_RETRY_MAX × _CRED_RETRY_INTERVAL_S ≈ 4.8 s, well under the
# presplash_hold 45 s watchdog.
_CRED_RETRY_MAX = 6
_CRED_RETRY_INTERVAL_S = 0.8


def _show_share_repo_qr_popup(url, langcode, font_name='Roboto'):
    """Modal popup that renders ``url`` as a QR code via segno
    and shows the URL underneath, plus a "Copy URL" button as a
    fallback for receivers without a QR scanner.

    Lives at module level (not a method on SettingsScreen) so it
    can be opened from anywhere the daemon UI later wants to —
    project picker, future per-project context menu, etc. The
    only inputs are ``url`` (what to encode), ``langcode`` (for
    the popup title so the user can confirm which project), and
    ``font_name`` (matches the rest of the daemon UI's
    CharisSIL look)."""
    import io
    from kivy.core.image import Image as CoreImage
    from kivy.graphics import Color, RoundedRectangle
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.button import Button
    from kivy.uix.image import Image
    from kivy.uix.label import Label
    from kivy.uix.modalview import ModalView

    # segno is bundled via the server APK's ``requirements``.
    # On desktop it must be pip-installed; if missing we surface
    # an actionable error instead of crashing the popup-open.
    try:
        import segno  # type: ignore[import-not-found]
    except ImportError:
        _show_segno_missing_popup(font_name)
        return

    # Generate the QR. segno.make() picks an error-correction
    # level that fits the URL; ``error='M'`` (15% correction)
    # gives a good camera-tolerance margin without inflating the
    # symbol size. ``border=2`` matches the canonical 4-module
    # quiet zone halved — enough for most decoders, keeps the
    # rendered widget compact.
    qr = segno.make(url, error='M')
    png_buf = io.BytesIO()
    # scale: 8 → ~250 px square at typical QR sizes. Plenty for
    # camera capture, fits comfortably in a Kivy popup.
    qr.save(png_buf, kind='png', scale=8, border=2)
    png_buf.seek(0)
    core_img = CoreImage(png_buf, ext='png')

    view = ModalView(size_hint=(0.92, None), height=dp(520),
                     auto_dismiss=False,
                     background_color=theme.OVERLAY_DARK)
    box = BoxLayout(orientation='vertical', padding=dp(16),
                    spacing=dp(12))
    with box.canvas.before:
        Color(*theme.SURFACE)
        box._bg = RoundedRectangle(pos=box.pos, size=box.size,
                                   radius=[dp(8)])
    box.bind(pos=lambda w, p: setattr(w._bg, 'pos', p),
             size=lambda w, s: setattr(w._bg, 'size', s))

    title = Label(
        text=_tr('Share project {langcode}').format(langcode=langcode),
        color=theme.ACCENT, font_size=sp(17), bold=True,
        font_name=font_name,
        size_hint_y=None, height=dp(36),
        halign='center', valign='middle')
    title.bind(size=lambda w, s: setattr(w, 'text_size', s))
    box.add_widget(title)

    instr = Label(
        text=_tr('Scan with another device to clone the repo.'),
        color=theme.TEXT_DIM, font_size=sp(13),
        font_name=font_name,
        size_hint_y=None, height=dp(28),
        halign='center', valign='middle')
    instr.bind(size=lambda w, s: setattr(w, 'text_size', s))
    box.add_widget(instr)

    qr_widget = Image(texture=core_img.texture,
                      size_hint=(1, None), height=dp(260),
                      allow_stretch=True, keep_ratio=True)
    box.add_widget(qr_widget)

    url_label = Label(
        text=url, color=theme.TEXT, font_size=sp(12),
        font_name=font_name,
        size_hint_y=None, height=dp(36),
        halign='center', valign='middle')
    url_label.bind(size=lambda w, s: setattr(w, 'text_size', s))
    box.add_widget(url_label)

    btn_row = BoxLayout(orientation='horizontal',
                        size_hint_y=None, height=dp(48),
                        spacing=dp(10))

    def _make_btn(label, fill, on_release):
        btn = Button(
            text=label, size_hint_y=None, height=dp(48),
            background_color=theme.TRANSPARENT,
            background_normal='',
            color=(1, 1, 1, 1), font_size=sp(15), bold=True,
            font_name=font_name)
        with btn.canvas.before:
            Color(*fill)
            btn._bg = RoundedRectangle(pos=btn.pos, size=btn.size,
                                       radius=[dp(8)])
        btn.bind(pos=lambda w, p: setattr(w._bg, 'pos', p),
                 size=lambda w, s: setattr(w._bg, 'size', s))
        btn.bind(on_release=on_release)
        return btn

    btn_row.add_widget(_make_btn(
        _tr('Close'), theme.ACCENT, lambda *_: view.dismiss()))
    box.add_widget(btn_row)

    view.add_widget(box)
    view.open()


def _show_segno_missing_popup(font_name):
    """Fallback popup for desktop installs that haven't installed
    ``segno`` (the suite's server APK bundles it; pip-installed
    daemon hosts may not have it). One-button modal pointing the
    user at the install command. Better than a silent crash."""
    from kivy.graphics import Color, RoundedRectangle
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.button import Button
    from kivy.uix.label import Label
    from kivy.uix.modalview import ModalView
    view = ModalView(size_hint=(0.85, None), height=dp(220),
                     auto_dismiss=False,
                     background_color=theme.OVERLAY_DARK)
    box = BoxLayout(orientation='vertical', padding=dp(16),
                    spacing=dp(12))
    with box.canvas.before:
        Color(*theme.SURFACE)
        box._bg = RoundedRectangle(pos=box.pos, size=box.size,
                                   radius=[dp(8)])
    box.bind(pos=lambda w, p: setattr(w._bg, 'pos', p),
             size=lambda w, s: setattr(w._bg, 'size', s))
    msg = Label(
        text=_tr('QR generation requires the "segno" package. '
                 'Install with:\n\n    pip install segno'),
        color=theme.TEXT, font_size=sp(14), font_name=font_name,
        halign='left', valign='middle')
    msg.bind(size=lambda w, s: setattr(w, 'text_size', s))
    box.add_widget(msg)
    btn = Button(
        text=_tr('OK'), size_hint_y=None, height=dp(48),
        background_color=theme.TRANSPARENT,
        background_normal='',
        color=(1, 1, 1, 1), font_size=sp(15), bold=True,
        font_name=font_name)
    with btn.canvas.before:
        Color(*theme.ACCENT)
        btn._bg = RoundedRectangle(pos=btn.pos, size=btn.size,
                                   radius=[dp(8)])
    btn.bind(pos=lambda w, p: setattr(w._bg, 'pos', p),
             size=lambda w, s: setattr(w._bg, 'size', s))
    btn.bind(on_release=lambda *_: view.dismiss())
    box.add_widget(btn)
    view.add_widget(box)
    view.open()


_AZT_ICON = os.path.join(
    os.path.dirname(azt_collab_client.__file__), 'azt.png')


KV_TEMPLATE = '''
#:import dp kivy.metrics.dp
#:import sp kivy.metrics.sp
#:import T azt_collab_client.ui.theme
#:import _ azt_collab_client.translate.tr
#:set FONT '{font_name}'
#:set SHARE_ICON '{share_icon}'

<RootSM>:
    SettingsScreen:
        name: 'settings'
    GitHubConnectScreen:
        name: 'github'
    GitLabFormScreen:
        name: 'gitlab'

<RecBtn@Button>:
    normal_color: T.ACCENT
    size_hint_y: None
    height: dp(52)
    background_color: T.TRANSPARENT
    background_normal: ''
    canvas.before:
        Color:
            rgba: self.normal_color or T.ACCENT
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(8)]
    color: 1, 1, 1, 1
    font_size: sp(16)
    font_name: FONT
    bold: True

<NavBtn@Button>:
    size_hint_y: None
    height: dp(48)
    background_color: T.TRANSPARENT
    background_normal: ''
    canvas.before:
        Color:
            rgba: T.SURFACE
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(8)]
    color: T.ACCENT
    font_size: sp(15)
    font_name: FONT
    bold: True

<IconBtn@Button>:
    background_color: T.TRANSPARENT
    background_normal: ''
    color: T.TEXT_DIM
    font_size: sp(20)
    font_name: FONT

<SectionLabel@Label>:
    size_hint_y: None
    height: dp(32)
    font_size: sp(13)
    font_name: FONT
    bold: True
    color: T.ACCENT
    halign: 'left'
    valign: 'middle'
    text_size: self.size

<HeaderLabel@Label>:
    font_name: FONT
    bold: True
    color: T.ACCENT
    font_size: sp(17)
    size_hint_y: None
    height: dp(40)
    halign: 'left'
    valign: 'middle'
    text_size: self.size

<BodyLabel@Label>:
    font_name: FONT
    color: T.TEXT
    font_size: sp(14)
    # Width-bound, height-free wrap. Several BodyLabel instances size
    # themselves with ``height: self.texture_size[1] + dp(8)``, which
    # forms a do_layout / texture_update feedback loop with the
    # previous ``text_size: self.size``. ``(self.width, None)`` keeps
    # wrapping at the widget width but lets texture_size[1] flow
    # from content alone — no cycle.
    text_size: self.width, None
    halign: 'left'
    valign: 'top'

<DimLabel@Label>:
    font_name: FONT
    color: T.TEXT_DIM
    font_size: sp(13)
    text_size: self.size
    halign: 'left'
    valign: 'middle'

<ThemedInput@TextInput>:
    multiline: False
    size_hint_y: None
    height: dp(48)
    font_size: sp(15)
    font_name: FONT
    background_color: T.SURFACE
    foreground_color: T.TEXT
    cursor_color: T.ACCENT
    hint_text_color: T.HINT

<TopBar@BoxLayout>:
    title: ''
    size_hint_y: None
    height: dp(52)
    padding: dp(8), dp(6)
    canvas.before:
        Color:
            rgba: T.SURFACE
        Rectangle:
            pos: self.pos
            size: self.size
    Label:
        text: root.title
        font_name: FONT
        bold: True
        color: T.ACCENT
        font_size: sp(17)
        halign: 'left'
        valign: 'middle'
        text_size: self.size
        padding_x: dp(8)

<SettingsScreen>:
    canvas.before:
        Color:
            rgba: T.BG
        Rectangle:
            pos: self.pos
            size: self.size
    BoxLayout:
        orientation: 'vertical'
        TopBar:
            title: _('AZT Collaboration — Settings')
        # CAWL image-cache progress banner. Pinned ABOVE the
        # ScrollView (not inside it) so it stays visible while the
        # user scrolls; the whole point is to let them know the
        # daemon is using network in the background so they don't
        # disconnect Wi-Fi mid-fetch. Visible only while a prefetch
        # is in flight (``cached < total``); hidden via height /
        # opacity once the cache catches up. The bottom-of-page
        # Status section is the wrong home — by design it's
        # supposed to fade into the background.
        BoxLayout:
            id: cawl_cache_status_banner
            orientation: 'vertical'
            size_hint_y: None
            height: 0
            opacity: 0
            padding: dp(10), dp(6)
            canvas.before:
                Color:
                    rgba: T.ACCENT
                Rectangle:
                    pos: self.pos
                    size: self.size
            BodyLabel:
                id: cawl_cache_status_label
                text: ''
                color: T.BG
                bold: True
                size_hint_y: None
                height: self.texture_size[1]
        ScrollView:
            do_scroll_x: False
            BoxLayout:
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                padding: dp(20)
                spacing: dp(14)
                # Back button — only present when this SettingsScreen
                # is reached from another screen (e.g. the picker's
                # gear sets ``back_to: 'picker'`` in the picker_app
                # _PickerRoot KV). Hidden / disabled in the standalone
                # settings host where back has no meaning.
                NavBtn:
                    # ``«`` (U+00AB) instead of ``←`` (U+2190) because
                    # the latter isn't in CharisSIL's glyph table —
                    # would render as tofu under the linguistic font.
                    # Guillemet is in every Latin font, reads as a
                    # back-pointer, and Title-cases nicely in French.
                    text: '«  ' + _('Back')
                    size_hint_y: None
                    height: dp(48) if root.back_to else 0
                    opacity: 1 if root.back_to else 0
                    disabled: not root.back_to
                    on_press: app.go(root.back_to) if root.back_to else None
                # ── Interface language ─────────────────────────────────
                # Language switcher at the very top, no section header
                # — the row of language-name buttons is self-evident.
                # Populated by _build_lang_selector() from the
                # azt_collab_client catalog discovery.
                BoxLayout:
                    id: lang_selector_row
                    size_hint_y: None
                    height: dp(40)
                    spacing: dp(8)
                # ── Share + Update ─────────────────────────────────────
                # Two utility actions on one row, half-width each.
                # Share is a no-op on desktop (surfaces a translated
                # "Android only" message); on Android it shares the
                # running APK so a user can hand it to a teammate.
                # Update polls the configured GitHub repo for a newer
                # release asset and triggers the system installer on
                # Android. Status messages flow through update_msg
                # below.
                BoxLayout:
                    size_hint_y: None
                    height: dp(52)
                    spacing: dp(10)
                    RecBtn:
                        text: _('Share')
                        normal_color: T.SURFACE
                        on_press: app.share_apk()
                        Image:
                            source: SHARE_ICON
                            size_hint: None, None
                            size: dp(24), dp(24)
                            x: self.parent.x + dp(12)
                            center_y: self.parent.center_y
                    RecBtn:
                        id: update_btn
                        text: _('Update')
                        normal_color: T.SURFACE
                        on_press: app.update_app()
                BodyLabel:
                    id: update_msg
                    text: ''
                    color: T.TEXT_DIM
                    size_hint_y: None
                    height: dp(20)
                SectionLabel:
                    text: _('Your name (appears in commits)')
                ThemedInput:
                    id: contributor_input
                    hint_text: _('first_name last_name')
                    on_focus: root.save_contributor() if not self.focus else None
                BodyLabel:
                    id: contributor_msg
                    text: ''
                    color: T.TEXT_DIM
                    size_hint_y: None
                    # Auto-grow so the multi-line "Required: …"
                    # warning isn't truncated when it appears.
                    height: self.texture_size[1] + dp(4)
                    text_size: self.width, None
                # ── Servers ────────────────────────────────────────────
                # All sync-target / on-the-wire-toggle controls live
                # under one header. The two host-credential buttons
                # are state-aware (refresh() flips Connect ↔ Settings
                # based on gh/gl.confirmed); Publish is conditional
                # on the most-recent project lacking a remote; Work
                # offline is the daemon-wide push-suppression toggle;
                # Cache images is the CAWL prefetch-eagerness toggle
                # (a server-side decision but exposed here as part of
                # the same "what does the daemon do on the network"
                # group).
                SectionLabel:
                    text: _('Servers (set up at least one)')
                # GitHub: a single state-aware button. ``refresh()``
                # flips the label between "Connect to GitHub" (until
                # verified) and "GitHub Settings" (once verified).
                # Both states navigate to the same screen, which
                # renders step-by-step setup or the manage view based
                # on credentials_status — Disconnect lives inside
                # there, not here, because re-auth costs the user the
                # 8-field code-typing dance and a fat-finger Disconnect
                # next to the main settings button has bitten people.
                # ``on_press`` (not ``on_release``) because ScrollView's
                # touch-grab heuristic ate every ``on_release`` here;
                # see the matching note on gh_primary_btn in
                # <GitHubConnectScreen>.
                RecBtn:
                    id: gh_action_btn
                    text: _('Connect to GitHub')
                    normal_color: T.GREEN
                    on_press: app.go('github')
                # GitLab: same shape. Label flipped in refresh() from
                # gl.confirmed; GitLabFormScreen owns Disconnect now.
                RecBtn:
                    id: gl_action_btn
                    text: _('Connect to GitLab')
                    normal_color: T.GREEN
                    on_press: app.go('gitlab')
                # Publish — visible only for the most-recent project
                # when it has no remote yet AND at least one host's
                # credentials have been confirmed. ``refresh()`` flips
                # the height/opacity to hide the entire row when not
                # applicable (the Kivy hide/show pattern from
                # ~/.claude-sil/CLAUDE.md).
                BoxLayout:
                    id: publish_row
                    orientation: 'vertical'
                    size_hint_y: None
                    height: 0
                    opacity: 0
                    spacing: dp(8)
                    RecBtn:
                        id: publish_btn
                        text: _('Publish data')
                        normal_color: T.GREEN
                        on_press: root.publish()
                    BodyLabel:
                        id: publish_msg
                        text: ''
                        color: T.TEXT_DIM
                        size_hint_y: None
                        height: dp(20)
                # Work-offline toggle (0.43.0). When on, the
                # connectivity watcher's push-drain is a no-op
                # and the user-gestured Sync button returns
                # ``S.WORK_OFFLINE_ENABLED`` (peers route to
                # this screen). Commits via commit_project are
                # unaffected — local work still groups into
                # commits; only the push half is suppressed.
                # Toggling OFF fires an immediate drain
                # server-side so the user doesn't wait a full
                # connectivity_poll_s tick for pending commits
                # to push.
                BoxLayout:
                    size_hint_y: None
                    height: dp(52)
                    spacing: dp(8)
                    BodyLabel:
                        text: _('Work offline:')
                        size_hint_x: None
                        width: dp(160)
                        halign: 'left'
                        valign: 'middle'
                        text_size: self.size
                    RecBtn:
                        id: work_offline_yes_btn
                        text: _('yes')
                        on_press: root.set_work_offline_mode(True)
                    RecBtn:
                        id: work_offline_no_btn
                        text: _('no')
                        on_press: root.set_work_offline_mode(False)
                # LAN sync (0.45.0). Daemon-wide toggle for the
                # device-to-device fan-out transport (parked design
                # in ``docs/local_lan_sync_stub.md``). When on, the
                # listener thread runs + (Android) the :provider
                # service is promoted to a foreground service of
                # type specialUse. Hot-applied — flipping does NOT
                # need a daemon restart.
                BoxLayout:
                    size_hint_y: None
                    height: dp(52)
                    spacing: dp(8)
                    BodyLabel:
                        text: _('Local-network sync:')
                        size_hint_x: None
                        width: dp(160)
                        halign: 'left'
                        valign: 'middle'
                        text_size: self.size
                    RecBtn:
                        id: lan_yes_btn
                        text: _('yes')
                        on_press: root.set_lan_allow_sync(True)
                    RecBtn:
                        id: lan_no_btn
                        text: _('no')
                        on_press: root.set_lan_allow_sync(False)
                # "Pair a phone" entry was vestigial after 0.45.0 —
                # showing the daemon's QR is now the "Show QR code"
                # affordance inside the per-project Share popup,
                # which carries the project + repo_url too (combined
                # pair-share-clone). "Paired devices" stays as the
                # management surface (unpair, static endpoints).
                BoxLayout:
                    size_hint_y: None
                    height: dp(48)
                    spacing: dp(8)
                    Button:
                        text: _('Paired devices')
                        on_press: root.open_paired_phones()
                BodyLabel:
                    id: lan_status_label
                    text: ''
                    size_hint_y: None
                    height: dp(28)
                    halign: 'left'
                    valign: 'middle'
                    text_size: self.size
                    font_size: sp(11)
                # Cache images — daemon-side CAWL prefetch policy.
                # Default is one image per CAWL line (the preferred
                # ``__`` variant); peers can still on-demand-fetch
                # other variants as the user navigates to them.
                # Flipping to "all" warms every variant in the index
                # — heavier on network / disk but useful when
                # bandwidth is cheap and the user wants the broader
                # set available offline.
                BoxLayout:
                    size_hint_y: None
                    height: dp(52)
                    spacing: dp(8)
                    BodyLabel:
                        text: _('Cache images:')
                        size_hint_x: None
                        width: dp(160)
                        halign: 'left'
                        valign: 'middle'
                        text_size: self.size
                    RecBtn:
                        id: cawl_variants_one_btn
                        text: _('1 per line')
                        on_press: root.set_cawl_prefetch_mode(False)
                    RecBtn:
                        id: cawl_variants_all_btn
                        text: _('all')
                        on_press: root.set_cawl_prefetch_mode(True)
                # Project actions for an already-published project:
                # invite a GitHub collaborator. Gated on
                # ``refresh()`` finding a last_project with a remote
                # URL; hidden otherwise via the height/opacity
                # pattern. Both actions are operations on the
                # current project, so they live alongside the
                # publish row rather than on a separate screen — the
                # user's mental model is "settings page = manage
                # what I'm doing right now."
                BoxLayout:
                    id: project_actions_row
                    orientation: 'vertical'
                    size_hint_y: None
                    height: 0
                    opacity: 0
                    spacing: dp(8)
                    SectionLabel:
                        id: project_actions_label
                        text: _('Current project')
                    BodyLabel:
                        id: project_actions_info
                        text: ''
                        color: T.TEXT_DIM
                        size_hint_y: None
                        height: self.texture_size[1] + dp(4)
                    # Per-peer sync status for THIS project (one line
                    # per paired peer): up to date / incoming / N to
                    # send. Populated by ``_tick_peer_sync``; sits just
                    # above the Share button per the layout Kent asked
                    # for (2026-07-23).
                    BoxLayout:
                        id: peer_sync_this
                        orientation: 'vertical'
                        size_hint_y: None
                        height: self.minimum_height
                        spacing: dp(2)
                    RecBtn:
                        id: share_project_btn
                        # The Share popup folds in three sharing
                        # modes (paired phones, QR for an in-person
                        # new phone, github invite for a remote
                        # collaborator). Used to be three buttons
                        # before 0.45.0; consolidated so the user
                        # sees one entry point. {{ }} are doubled
                        # because KV_TEMPLATE itself goes through
                        # Python ``.format()`` in ``register_kv``,
                        # which would otherwise try to substitute
                        # ``{{langcode}}`` at load time and
                        # KeyError on the missing kwarg.
                        text: _('Share [{{langcode}}] project').format(langcode=root.current_langcode_label)
                        normal_color: T.SURFACE
                        on_press: root.share_project()
                    BodyLabel:
                        id: project_actions_msg
                        text: ''
                        color: T.TEXT_DIM
                        size_hint_y: None
                        height: dp(20)
                # Per-peer sync status for OTHER projects (one line per
                # peer × project): between the current-project row and
                # Switch project, per Kent's layout (2026-07-23). Lives
                # OUTSIDE the gated project_actions_row so it shows even
                # when the current project has no remote.
                BoxLayout:
                    id: peer_sync_others
                    orientation: 'vertical'
                    size_hint_y: None
                    height: self.minimum_height
                    spacing: dp(2)
                # Switch project — always visible, outside the gated
                # project_actions_row. A user may want to abandon an
                # unpublished project for another without publishing
                # first; the row above is gated on a remote being
                # set, this button isn't.
                RecBtn:
                    id: switch_project_btn
                    text: _('Switch project')
                    size_hint_y: None
                    height: dp(52)
                    normal_color: T.SURFACE
                    on_press: root.switch_project()
                # Status — read-only credential / online state, parked
                # at the bottom now that the actionable rows live up
                # top. Refresh Status sits directly under it so the
                # button affordance for "I changed something elsewhere,
                # pull it again" is right next to the data it updates.
                SectionLabel:
                    text: _('Status')
                BodyLabel:
                    id: status_label
                    text: _('Loading...')
                    size_hint_y: None
                    height: self.texture_size[1] + dp(8)
                NavBtn:
                    text: _('Refresh Status')
                    on_press: root.refresh()
                Widget:
                    size_hint_y: None
                    height: dp(8)
                # Service-control row. Diagnostic-log capture is
                # always-on since 0.52.7 (per-day rotation + 3-day
                # retention bound disk cost); no toggle here. The
                # ``Share diagnostics`` button ships the same multi-
                # attachment bundle the picker's button ships —
                # snapshot + per-day daemon logs. Same label, same
                # underlying ``share_files`` action, two affordances
                # (one here for when the user is already in settings,
                # one on the picker for the "stuck on empty picker"
                # case). Status label below fields restart feedback.
                RecBtn:
                    id: restart_server_btn
                    text: _('Restart server')
                    normal_color: T.SURFACE
                    on_press: root.restart_server()
                RecBtn:
                    id: share_diagnostics_btn
                    text: _('Share diagnostics')
                    normal_color: T.SURFACE
                    on_press: root.share_diagnostics()
                BodyLabel:
                    id: service_status
                    text: ''
                    color: T.TEXT_DIM
                    size_hint_y: None
                    height: self.texture_size[1] + dp(4)
                Widget:
                    size_hint_y: None
                    height: dp(8)
                # Debug "force /v1/health to 503" sentinel — the UI
                # surface is removed for now (was clutter for normal
                # users). The daemon-side mechanics stay in
                # ``server.py:_h_health`` so a tester can still
                # plant the sentinel via adb:
                #     adb shell run-as org.atoznback.aztcollab \
                #         touch files/azt/_debug_force_503
                # The ``toggle_debug_503`` / ``_debug_503_path`` /
                # ``_refresh_debug_503_state`` methods on
                # SettingsScreen are kept around as a REPL-callable
                # convenience for future UI re-enablement.
                Widget:
                    size_hint_y: None
                    height: dp(8)
                Label:
                    text: app.version_string
                    font_name: FONT
                    color: T.TEXT_DIM
                    font_size: sp(13)
                    size_hint_y: None
                    height: dp(22)
                    halign: 'center'
                    text_size: self.size

<GitHubConnectScreen>:
    # Step-by-step connect flow. Pre-flight explanation + a 3-step
    # indicator (Authorize → Install App → Verify) gates progress; a
    # single state-aware "primary" button presents only the next
    # action. No auto-firing — the user always taps Begin / Install
    # GitHub App / Verify setup explicitly, so a partial setup that
    # picks back up later resumes from where it stopped (server state
    # owns "what's done" — connected / app_installed / confirmed).
    # All show/hide goes through the Kivy hide/show pattern.
    canvas.before:
        Color:
            rgba: T.BG
        Rectangle:
            pos: self.pos
            size: self.size
    BoxLayout:
        orientation: 'vertical'
        TopBar:
            title: _('Connect to GitHub')
        ScrollView:
            do_scroll_x: False
            BoxLayout:
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                padding: dp(20)
                spacing: dp(14)
                # ── Pre-flight explanation ─────────────────────────────
                # Fixed height keeps the BoxLayout's ``minimum_height``
                # stable across frames so buttons below don't shift
                # position after the texture re-wraps. Growing
                # ``texture_size[1]``-based heights cause the buttons
                # below to migrate as the BodyLabel resizes, leaving
                # the user tapping "where the button used to be" while
                # hit-testing checks "where the button is now."
                BodyLabel:
                    id: gh_preflight
                    text: _('GitHub is a free service for backing up your project to the cloud. You will need a free GitHub account.')
                    size_hint_y: None
                    height: dp(80)
                    text_size: self.width, None
                # See the ``on_press``-vs-``on_release`` rationale on
                # gh_primary_btn below: ScrollView ate on_release for
                # both buttons in testing. Same fix applies here.
                NavBtn:
                    id: gh_signup_btn
                    text: _('Create a GitHub account (free)')
                    on_press: root.open_signup()
                # ── Step indicator (1. Authorize → 2. Install → 3. Verify)
                # State (done / current / pending) is rendered via colour +
                # bold by ``_render_steps`` from the server-tracked flags
                # (gh.connected / app_installed / confirmed). Visual only —
                # the steps aren't tappable; the primary button below
                # carries the active action.
                BoxLayout:
                    id: gh_steps
                    orientation: 'vertical'
                    size_hint_y: None
                    height: dp(84)
                    spacing: dp(4)
                    Label:
                        id: gh_step1
                        text: ''
                        font_name: FONT
                        font_size: sp(14)
                        size_hint_y: None
                        height: dp(24)
                        halign: 'left'
                        valign: 'middle'
                        text_size: self.size
                    Label:
                        id: gh_step2
                        text: ''
                        font_name: FONT
                        font_size: sp(14)
                        size_hint_y: None
                        height: dp(24)
                        halign: 'left'
                        valign: 'middle'
                        text_size: self.size
                    Label:
                        id: gh_step3
                        text: ''
                        font_name: FONT
                        font_size: sp(14)
                        size_hint_y: None
                        height: dp(24)
                        halign: 'left'
                        valign: 'middle'
                        text_size: self.size
                # Same fixed-height treatment as gh_preflight: 4 lines
                # of body text covers every message we set (the
                # multi-line "Opening URL ..." block is the longest).
                # Stops the button below from migrating mid-tap.
                BodyLabel:
                    id: gh_message
                    text: ''
                    size_hint_y: None
                    height: dp(80)
                    text_size: self.width, None
                # ── Primary action — Begin / Install GitHub App / Verify
                # setup. Hidden during device-flow polling (the code box
                # below carries that affordance) and once all three steps
                # are done.
                # ``on_press`` (not ``on_release``) because ScrollView's
                # touch-grab heuristic ate every ``on_release`` here:
                # the touch reached the button (state went to 'down'),
                # but on touch_up ScrollView claimed it as a tiny
                # scroll attempt and the Button's state machine never
                # got to fire on_release. ``on_press`` runs at
                # touch_down — before ScrollView has a chance to
                # claim — so the action triggers regardless of how
                # ScrollView resolves the gesture.
                RecBtn:
                    id: gh_primary_btn
                    text: _('Begin')
                    normal_color: T.GREEN
                    on_press: root.primary_action()
                # ── Device-flow code section ───────────────────────────
                # Visible only while polling for user authorization on
                # GitHub. Shows the user_code so it's still readable if
                # the auto-paste failed and the user has to type it on
                # the device-flow page.
                BoxLayout:
                    id: gh_device_flow_box
                    orientation: 'vertical'
                    size_hint_y: None
                    height: 0
                    opacity: 0
                    spacing: dp(14)
                    SectionLabel:
                        text: _('Your one-time code')
                    BoxLayout:
                        size_hint_y: None
                        height: dp(72)
                        spacing: dp(10)
                        Label:
                            id: gh_user_code
                            text: ''
                            font_name: FONT
                            bold: True
                            color: T.ACCENT
                            font_size: sp(28)
                            halign: 'center'
                            valign: 'middle'
                            text_size: self.size
                        NavBtn:
                            size_hint_x: None
                            width: dp(96)
                            text: _('Copy')
                            on_press: root.copy_code()
                # ── Secondary actions ──────────────────────────────────
                # Re-authenticate + Disconnect — only when a token is on
                # file. Re-auth restarts the device flow without first
                # disconnecting, so a mid-flow bail leaves the existing
                # token intact.
                BoxLayout:
                    id: gh_manage_box
                    orientation: 'vertical'
                    size_hint_y: None
                    height: 0
                    opacity: 0
                    spacing: dp(10)
                    NavBtn:
                        text: _('Re-authenticate')
                        on_press: root.reauthenticate()
                    NavBtn:
                        text: _('Disconnect')
                        on_press: root.disconnect()
                NavBtn:
                    text: _('Back')
                    on_press: app.go('settings')

<GitLabFormScreen>:
    canvas.before:
        Color:
            rgba: T.BG
        Rectangle:
            pos: self.pos
            size: self.size
    BoxLayout:
        orientation: 'vertical'
        TopBar:
            title: _('Connect to GitLab')
        ScrollView:
            do_scroll_x: False
            BoxLayout:
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                padding: dp(20)
                spacing: dp(14)
                BodyLabel:
                    text: _('Enter your GitLab username and a personal access token (read/write to repos).')
                    size_hint_y: None
                    height: self.texture_size[1] + dp(8)
                    text_size: self.width, None
                DimLabel:
                    text: _('Username')
                    size_hint_y: None
                    height: dp(24)
                ThemedInput:
                    id: gl_user
                DimLabel:
                    text: _('Personal access token')
                    size_hint_y: None
                    height: dp(24)
                ThemedInput:
                    id: gl_token
                    password: True
                BodyLabel:
                    id: gl_msg
                    text: ''
                    color: T.TEXT_DIM
                    size_hint_y: None
                    height: dp(40)
                Widget:
                    size_hint_y: None
                    height: dp(8)
                RecBtn:
                    id: gl_test_btn
                    text: _('Verify setup')
                    normal_color: T.GREEN
                    on_press: root.test()
                # Disconnect lives here, not on the SettingsScreen,
                # so a fat-finger from the main menu can't blow away
                # a working PAT — the user has to navigate INTO the
                # GitLab page first. Visible only when ``connected``,
                # so the form acts as the connect surface for
                # not-yet-connected users without an out-of-place
                # Disconnect cluttering the layout.
                BoxLayout:
                    id: gl_manage_box
                    orientation: 'vertical'
                    size_hint_y: None
                    height: 0
                    opacity: 0
                    spacing: dp(10)
                    NavBtn:
                        text: _('Disconnect')
                        on_press: root.disconnect()
                NavBtn:
                    text: _('Back')
                    on_press: app.go('settings')
'''


_kv_loaded = False


def register_kv(font_name='Roboto'):
    """Load the settings/connect/form screen KV with the host's font.

    Idempotent: safe to call from both ``CollabUIApp.build()`` (the
    standalone settings subprocess) and ``picker_app.PickerApp.build()``
    (the in-process settings hosted inside the picker, reachable via
    the gear). Subsequent calls are no-ops so a second host doesn't
    redefine class rules.

    Hosts that want the settings screens in their own ``ScreenManager``
    can add the screen instances after calling this — class rules
    (``<SettingsScreen>``, ``<GitHubConnectScreen>``,
    ``<GitLabFormScreen>``, plus the shared ``<RecBtn>``, ``<NavBtn>``,
    ``<SectionLabel>``, ``<TopBar>`` etc.) will already be in scope."""
    global _kv_loaded
    if _kv_loaded:
        return
    Builder.load_string(KV_TEMPLATE.format(
        font_name=font_name,
        share_icon=icon_path('share_dark'),
    ))
    _kv_loaded = True


class RootSM(ScreenManager):
    pass


def _github_backup_line(ps):
    """One-line GitHub backup-progress summary for the settings
    'Current project' block — the surface a user can screenshot to
    show how far a slow trickle-up still has to go.

    Reads the daemon's sync counters with 0.53.3 semantics:
    ``wan_unshared`` is how many commits still need their bytes on
    github and TICKS DOWN as a chunked topic-push uploads history
    (pre-0.53 it stayed pinned at the full divergence until the final
    merge); ``main_merged`` gates the fully-backed-up state, because
    all bytes can be on github via a topic ref before the final merge
    to main lands — the "WAN-0 / finishing" window. Returns '' when
    the status is unknown (never guesses "backed up")."""
    if ps is None:
        return ''
    try:
        wan = int(getattr(ps, 'wan_unshared', 0) or 0)
        merged = bool(getattr(ps, 'main_merged', True))
        offline = bool(getattr(ps, 'work_offline', False))
    except Exception:
        return ''
    if wan <= 0 and merged:
        return _tr('GitHub backup: ✓ backed up')
    if wan <= 0 and not merged:
        # All bytes uploaded to a topic ref; the final merge to main
        # hasn't completed yet. Not "backed up" per the user contract
        # ("no OK until it's merged"); the count stays at 0.
        return _tr('GitHub backup: finishing (merging)…')
    # wan > 0: still uploading. Show how far it has to go.
    if offline:
        return _tr('GitHub backup: {n} commit(s) to go '
                   '(paused — work offline)').format(n=wan)
    return _tr('GitHub backup: {n} commit(s) to go').format(n=wan)


# ── Settings ────────────────────────────────────────────────────────────────

class SettingsScreen(Screen):
    # Set by the host KV when this screen is reachable from somewhere
    # else (e.g. ``back_to: 'picker'`` in ``picker_app._PickerRoot``).
    # Empty string → no back button (standalone settings host).
    back_to = StringProperty('')

    # Updated by ``_refresh_project_actions_row`` so the
    # "Share {langcode} project" button label tracks the
    # currently-resolved project. Empty until the row finds a
    # publish candidate.
    current_langcode_label = StringProperty('')

    def on_enter(self):
        # Defer to next frame: on_enter can fire before the KV rule's
        # nested BoxLayout children have all been added to ``self.ids``
        # on Kivy >= 2.3, which raises a confusing
        # "'super' object has no attribute '__getattr__'" from
        # ObservableDict when a key is missing.
        def _ready(*_):
            # Fresh retry budget per screen entry so a visit while the
            # daemon is down still re-polls (and, at startup, holds the
            # splash until the daemon answers). The presplash release
            # itself lives in refresh(), gated on the daemon actually
            # answering — see the comment there.
            self._credentials_retry_count = 0
            self._build_lang_selector()
            self.refresh()
            self._start_cawl_cache_poll()
            self._start_peer_sync_poll()
            self._focus_contributor_if_unset()
        Clock.schedule_once(_ready, 0)

    def on_leave(self):
        # Stop the polls when the user navigates away — don't leave
        # Clock-scheduled callbacks waking the daemon for a screen the
        # user can't see.
        self._stop_cawl_cache_poll()
        self._stop_peer_sync_poll()

    _cawl_cache_event = None

    _cawl_cache_langcode = ''

    def _start_cawl_cache_poll(self):
        """Begin polling the daemon's CAWL cache_status endpoint so
        the in-flight prefetch progress is visible alongside the
        rest of the daemon's status block. Auto-stops once the
        cache catches up to the index. The peer-side indicator
        (CLIENT_INTEGRATION.md § 10) covers the same need from the
        recorder's main screen; this is the daemon-UI mirror for
        users who navigate here while caching is running.

        1 Hz feels live without dragging — the daemon's
        ``cache_status`` is memoised after first call (counter
        increment per image fetch, no per-poll os.walk), so 1 Hz
        polling is near-zero CPU."""
        self._stop_cawl_cache_poll()
        # Resolve the langcode ONCE at poll start; the user
        # doesn't switch projects while sitting on the settings
        # screen, and polling ``last_project()`` per tick was
        # adding two RPCs and two log lines per second.
        self._cawl_cache_langcode = (last_project() or '').strip()
        self._tick_cawl_cache_status()  # immediate first read
        self._cawl_cache_event = Clock.schedule_interval(
            lambda _dt: self._tick_cawl_cache_status(), 1.0)

    def _stop_cawl_cache_poll(self):
        first_try_log('settings.stop_cawl_cache_poll',
                      had_event=self._cawl_cache_event is not None)
        if self._cawl_cache_event is not None:
            try:
                self._cawl_cache_event.cancel()
            except Exception:
                pass
            self._cawl_cache_event = None

    # ── Peer-sync overlay (Tier A: "where do I stand with my peers") ──
    _peer_sync_event = None

    def _start_peer_sync_poll(self):
        """Poll per-peer sync status every ~2.5 s while the settings
        screen is up. Cheap server-side on the steady path (a caught-up
        peer does no git walk); the fetch runs off the UI thread."""
        self._stop_peer_sync_poll()
        self._tick_peer_sync()   # immediate first paint
        self._peer_sync_event = Clock.schedule_interval(
            lambda _dt: self._tick_peer_sync(), 2.5)

    def _stop_peer_sync_poll(self):
        if self._peer_sync_event is not None:
            try:
                self._peer_sync_event.cancel()
            except Exception:
                pass
            self._peer_sync_event = None

    def _tick_peer_sync(self):
        # Fetch off the UI thread — the daemon walks git per peer — then
        # render back on the UI thread.
        def _work():
            try:
                from azt_collab_client import lan_peer_sync, last_project
                rows = lan_peer_sync() or []
                current = (last_project() or '').strip()
            except Exception:
                rows, current = [], ''
            Clock.schedule_once(
                lambda _dt: self._render_peer_sync(rows, current), 0)
        threading.Thread(target=_work, daemon=True).start()

    def _peer_sync_status_text(self, row):
        """The right-hand status phrase for one peer row: 'up to date'
        / 'incoming' / 'N to send' (+ 'incoming' when diverged), or
        '?' when we couldn't compute the outbound count."""
        if not row.get('to_send_known', True):
            return _tr('awaiting first sync')
        parts = []
        n = int(row.get('to_send', 0) or 0)
        if n > 0:
            shown = (str(n) + '+') if row.get('capped') else str(n)
            parts.append(_tr('{n} to send').format(n=shown))
        if row.get('incoming'):
            parts.append(_tr('incoming'))
        return ' · '.join(parts) if parts else _tr('up to date')

    def _render_peer_sync(self, rows, current_langcode):
        from kivy.uix.label import Label
        from azt_collab_client.ui import theme as _theme
        this_box = self.ids.get('peer_sync_this')
        others_box = self.ids.get('peer_sync_others')
        if this_box is None or others_box is None:
            return
        this_box.clear_widgets()
        others_box.clear_widgets()
        font = getattr(App.get_running_app(), 'font_name', 'Roboto')
        for row in rows:
            name = (row.get('device_name')
                    or (row.get('peer_id', '')[:8] + '…'))
            lang = row.get('langcode', '')
            status = self._peer_sync_status_text(row)
            if lang and lang == current_langcode:
                text = f'{name} · {status}'
                box = this_box
            else:
                text = f'{name} · {lang} · {status}'
                box = others_box
            lbl = Label(text=text, size_hint_y=None, height=dp(20),
                        font_size=sp(11), color=_theme.TEXT_DIM,
                        halign='left', valign='middle', font_name=font)
            lbl.bind(width=lambda w, *_: setattr(
                w, 'text_size', (w.width, None)))
            box.add_widget(lbl)
        # The current-project lines live INSIDE the gated actions row,
        # whose height is set explicitly — grow it to fit them.
        self._sync_actions_row_height()

    def _sync_actions_row_height(self):
        """Add the current-project peer lines' height to the gated
        actions row (its base height is stamped by
        ``_refresh_project_actions_row``). No-op until that base exists
        or when the row is hidden (base 0)."""
        row = self.ids.get('project_actions_row')
        this_box = self.ids.get('peer_sync_this')
        base = getattr(self, '_actions_row_base_h', 0)
        if row is None or this_box is None or not base:
            return
        row.height = base + this_box.height

    def _tick_cawl_cache_status(self):
        banner = self.ids.get('cawl_cache_status_banner')
        label = self.ids.get('cawl_cache_status_label')
        first_try_log(
            'settings.cawl_tick',
            current_screen=(self.manager.current
                            if self.manager else None),
            has_banner=banner is not None,
            has_label=label is not None,
            cached_langcode=self._cawl_cache_langcode)
        if banner is None or label is None:
            return
        langcode = self._cawl_cache_langcode
        if not langcode:
            self._hide_cawl_cache_banner(banner, label)
            return
        status = cawl_cache_status(langcode)
        total = status['total']
        cached = status['cached']
        offline = status.get('offline', False)
        circuit_open = status.get('circuit_open', False)
        # Diagnostic (0.50.35): log the response the server UI
        # received. Mirrors the recorder's ``[cache-status]`` log
        # line so we can triangulate where ``last_source`` is
        # losing its value — daemon process (`:provider`) emits
        # the outbound response via cawl.cache_status; this line
        # captures what the picker_app process saw on the other
        # side of the ContentProvider transport; the recorder's
        # own log captures what IT saw. Mismatch points to the
        # process boundary that drops the field. Will be removed
        # or rate-limited once the bug is found.
        import sys
        print(f'[cache-status] (server-ui) cached={cached} '
              f"total={total} offline={offline} "
              f"circuit_open={circuit_open} "
              f"last_source={status.get('last_source', '')!r} "
              f"from_cache={status.get('from_cache', 0)} "
              f"from_lan={status.get('from_lan', 0)} "
              f"from_upstream={status.get('from_upstream', 0)} "
              f"image_repo={status.get('image_repo', '')!r}",
              file=sys.stderr, flush=True)
        if total == 0:
            self._hide_cawl_cache_banner(banner, label)
            return
        if cached >= total:
            # Cache is warm; stop polling.
            self._hide_cawl_cache_banner(banner, label)
            self._stop_cawl_cache_poll()
            return
        if offline:
            # Worker bailed before iterating because device was
            # offline. Banner stays polling at 1 Hz so we
            # auto-update when the scheduler's connectivity
            # watcher fires on_online_edge and the next prefetch
            # actually runs. RPC cost is in-memory dict lookups;
            # the [first-try] probe is already suppressed for
            # this path.
            label.text = _tr(
                'Image cache: {cached} / {total} '
                '(offline — will resume when online)').format(
                    cached=cached, total=total)
        elif circuit_open:
            # Mid-prefetch connectivity loss tripped the breaker.
            # Same auto-resume path via the scheduler edge.
            label.text = _tr(
                'Image cache: {cached} / {total} '
                '(paused — connectivity lost)').format(
                    cached=cached, total=total)
        else:
            # Live progress with the source tag (since 0.50.38)
            # so the user sees whether bytes are coming from a
            # paired LAN peer (free) or upstream GitHub
            # (metered). ``last_source`` is the most-recent
            # successful fetch's source; ``''`` until anything
            # lands. Cache hits get the generic "network in
            # use" wording — the message is for active fetches,
            # not on-disk warmups.
            last_source = status.get('last_source', '')
            if last_source == 'lan':
                label.text = _tr(
                    'Caching images: {cached} / {total} '
                    '· via LAN').format(
                        cached=cached, total=total)
            elif last_source == 'upstream':
                label.text = _tr(
                    'Caching images: {cached} / {total} '
                    '· via Internet (please stay online)'
                ).format(cached=cached, total=total)
            else:
                # 'cache', 'unknown', or '' — fall back to the
                # generic "network in use" line. Cache hits
                # don't justify a "via" tag (no current network
                # serving anything); 'unknown' / '' indicate
                # initial state or a bug already loud in the
                # daemon log via ``[cawl] cache_status bug:``.
                label.text = _tr(
                    'Caching images: {cached} / {total} '
                    '(network in use — please stay online)').format(
                        cached=cached, total=total)
        label.height = label.texture_size[1]
        banner.height = label.height + dp(12)
        banner.opacity = 1

    def _hide_cawl_cache_banner(self, banner, label):
        label.text = ''
        label.height = 0
        banner.height = 0
        banner.opacity = 0

    def _build_lang_selector(self):
        """Populate the language selector row with one button per
        catalog discovered under ``azt_collab_client/locales/`` plus
        English. Selected language is highlighted; tapping a different
        button calls ``_set_ui_language`` which rebuilds every screen
        in the manager so translations take effect live."""
        row = self.ids.get('lang_selector_row')
        if row is None:
            return
        row.clear_widgets()
        cur = _client_i18n.current_language()
        for code, name in _client_i18n.available_languages():
            btn = Button(
                text=name,
                font_size=sp(14),
                size_hint_x=1,
                background_color=(
                    theme.ACCENT if code == cur else theme.SURFACE),
                background_normal='',
                color=theme.TEXT,
            )
            # ``on_press`` (not ``on_release``) for the same
            # ScrollView-eats-on_release reason documented on the
            # KV-side action buttons.
            btn.bind(on_press=lambda b, c=code: self._set_ui_language(c))
            row.add_widget(btn)

    def _set_ui_language(self, lang_code):
        if lang_code == _client_i18n.current_language():
            return
        _client_i18n.set_language(lang_code)
        # Rebuild all screens so KV ``text: _('...')`` bindings
        # re-evaluate against the new catalog. Same dance the
        # recorder's ConfigScreen uses. Capture host-set properties
        # (currently just ``back_to``) before clearing — they live on
        # the *instance*, not the class, so recreating from the class
        # alone loses them and (e.g.) the picker host's "← Back"
        # button vanishes after a language toggle.
        app = App.get_running_app()
        sm = app.sm
        old_transition = sm.transition
        sm.transition = NoTransition()
        screens_info = [
            {'name': s.name, 'cls': type(s),
             'back_to': getattr(s, 'back_to', '')}
            for s in list(sm.screens)
        ]
        sm.clear_widgets()
        for info in screens_info:
            screen = info['cls'](name=info['name'])
            if info['back_to']:
                screen.back_to = info['back_to']
            sm.add_widget(screen)
        sm.current = 'settings'
        Clock.schedule_once(
            lambda dt: setattr(sm, 'transition', old_transition), 0.1)

    def _retry_refresh_credentials(self):
        """Bounded follow-up refresh after a cold-start
        ``get_credentials_status`` returned the daemon-unreachable
        fallback. Each ``refresh()`` re-checks and, while the daemon
        is still cold and the budget (``_CRED_RETRY_MAX``) isn't
        spent, schedules the next tick — and releases the presplash
        the moment the daemon answers (or the budget runs out). We
        only re-run when the screen is still current, otherwise we'd
        wake an off-screen settings page."""
        if self.manager is None or self.manager.current != self.name:
            return
        self.refresh()

    def refresh(self):
        # Debug-section indicator stays in sync regardless of
        # whether status fetching succeeded.
        try:
            self._refresh_debug_503_state()
        except Exception:
            pass
        try:
            self._refresh_work_offline_state()
        except Exception:
            pass
        try:
            self._refresh_lan_state()
        except Exception:
            pass
        try:
            self._refresh_cawl_variants_state()
        except Exception:
            pass
        # Split the two RPCs so a transient ``is_online`` failure
        # doesn't skip the GitHub/GitLab button update — pre-fix,
        # any exception in the combined try block bailed before
        # the button text was set, leaving "Connect to GitHub" on
        # screen even when credentials were confirmed. Field
        # symptom: button state on first open of the settings page
        # was inconsistent across launches; the buttons reflected
        # the daemon answer only when *both* RPCs happened to
        # succeed in this single try.
        try:
            status = get_credentials_status()
        except Exception as ex:
            label = self.ids.get('status_label')
            if label is not None:
                label.text = _tr('Error: {error}').format(error=ex)
            status = {}
        try:
            online = is_online()
        except Exception:
            online = False
        gh = status.get('github', {})
        gl = status.get('gitlab', {})
        # ``get_credentials_status`` returns a ServerUnavailable
        # fallback dict (no ``confirmed`` field) when the daemon
        # isn't yet reachable. On a cold launch the daemon
        # (:provider process on Android) is still spawning, so the
        # first call gets the fallback; re-poll until it answers so
        # the buttons reflect reality once the daemon finishes
        # booting. Bounded so we don't poll forever if the daemon
        # really is gone.
        daemon_answered = 'confirmed' in gh
        if not daemon_answered and getattr(
                self, '_credentials_retry_count', 0) < _CRED_RETRY_MAX:
            self._credentials_retry_count = getattr(
                self, '_credentials_retry_count', 0) + 1
            Clock.schedule_once(
                lambda _dt: self._retry_refresh_credentials(),
                _CRED_RETRY_INTERVAL_S)
        # Presplash release is tied to DAEMON-ANSWERED, not to
        # refresh() merely returning. Releasing after the cold-start
        # fallback dict dropped the splash onto correct-looking
        # chrome that then repainted ~1 s later when the retry filled
        # in real button states (field 2026-07-22: "still a settings
        # load after the splash"). Hold until the daemon answers — or,
        # if it never does within the retry budget, release anyway so
        # a genuinely-down daemon can't outlast the budget (best-effort
        # UI beats a stuck splash; the 45 s watchdog is the last
        # resort). Idempotent + no-op off Android / after first call.
        if daemon_answered or getattr(
                self, '_credentials_retry_count', 0) >= _CRED_RETRY_MAX:
            try:
                from azt_collab_client.ui import presplash_hold
                presplash_hold.release()
            except Exception:
                pass
        # GitHub: single state-aware button. We gate on ``confirmed``
        # rather than ``connected`` because a half-finished setup
        # (token saved but App not yet installed / Verify not yet
        # tapped) still needs the user to *finish connecting*, not
        # disconnect. Surfacing "Disconnect" in that intermediate
        # state was a real footgun — a user who couldn't finish the
        # install only had a Disconnect button to tap and ended up
        # blowing away the partial work. Now: not-confirmed → "Connect
        # to GitHub" (resumes the step where they stopped); confirmed
        # → "GitHub Settings" (opens the same screen which renders
        # the manage view with Disconnect / Re-auth inside).
        gh_confirmed = bool(gh.get('confirmed'))
        gh_btn = self.ids.get('gh_action_btn')
        if gh_btn is not None:
            if gh_confirmed:
                gh_btn.text = _tr('GitHub Settings')
                gh_btn.normal_color = theme.SURFACE
            else:
                gh_btn.text = _tr('Connect to GitHub')
                gh_btn.normal_color = theme.GREEN
        # GitLab: same pattern — "Connect to GitLab" until verified,
        # "GitLab Settings" once verified. Disconnect lives inside
        # the GitLab form (same rationale as GitHub: re-auth means
        # re-typing the PAT, not something we want users doing on a
        # mistap).
        gl_confirmed = bool(gl.get('confirmed'))
        gl_btn = self.ids.get('gl_action_btn')
        if gl_btn is not None:
            if gl_confirmed:
                gl_btn.text = _tr('GitLab Settings')
                gl_btn.normal_color = theme.SURFACE
            else:
                gl_btn.text = _tr('Connect to GitLab')
                gl_btn.normal_color = theme.GREEN
        # Contributor field — only repopulate when the user isn't
        # actively editing it, so a refresh during typing doesn't
        # clobber in-progress input.
        contrib_input = self.ids.get('contributor_input')
        if contrib_input is not None and not contrib_input.focus:
            contrib_input.text = status.get('contributor', '') or ''
        yes = _tr('yes')
        no = _tr('no')
        lines = [
            f"{_tr('Online:')}   {yes if online else no}",
            "",
            "GitHub",
            f"  {_tr('Connected:')}     {yes if gh.get('connected') else no}",
            f"  {_tr('Username:')}      {gh.get('username', '') or '-'}",
            f"  {_tr('App installed:')} "
            f"{yes if gh.get('app_installed') else no}",
            f"  {_tr('Confirmed:')}     {yes if gh.get('confirmed') else no}",
            "",
            "GitLab",
            f"  {_tr('Connected:')} {yes if gl.get('connected') else no}",
            f"  {_tr('Username:')}  {gl.get('username', '') or '-'}",
            f"  {_tr('Confirmed:')} {yes if gl.get('confirmed') else no}",
        ]
        self.ids.status_label.text = '\n'.join(lines)
        self._refresh_publish_row(status)
        self._refresh_project_actions_row(status)

    def _refresh_project_actions_row(self, status):
        """Show / hide the "Current project" actions row (Grant
        collaborator, etc.) and seed its info label.

        Visibility rule: there's a ``last_project()`` that resolves
        to a registered project AND that project has a remote URL.
        If no project is selected, or the selected project hasn't
        been published, this row stays hidden and the
        publish_row sibling takes precedence.

        The two rows are mutually exclusive by construction —
        publish_row shows ONLY when remote is empty; this row
        shows ONLY when remote is present — so the user sees one
        "what can I do with this project" surface at a time,
        appropriate to the project's current state."""
        row = self.ids.get('project_actions_row')
        info = self.ids.get('project_actions_info')
        msg = self.ids.get('project_actions_msg')
        btn = self.ids.get('share_project_btn')
        if row is None or btn is None:
            return
        # Default hidden; flip on only if all gates pass. Detach
        # children so their RecBtn ``on_press`` handlers can't
        # intercept touches that should reach the
        # gh_action_btn / gl_action_btn / publish_btn ABOVE this
        # row — same hide-by-detach pattern used in
        # _refresh_publish_row. Without this, taps on Connect to
        # GitHub fired the grant_collab_btn handler (this row's
        # first button), Publish fired switch_project_btn, etc.
        row.height = 0
        row.opacity = 0
        self._actions_row_base_h = 0   # hidden ⇒ peer-sync adds nothing
        if info is not None:
            info.text = ''
        if msg is not None:
            msg.text = ''
        self._detach_project_actions_children()
        project = self._pick_publish_candidate()
        if project is None:
            self.current_langcode_label = ''
            return
        # Used by the consolidated "Share {langcode} project" button
        # label (0.45.0). Empty when no project is resolved so the
        # button text reads "Share  project" (cosmetic — the button
        # is hidden in that case too).
        self.current_langcode_label = project.langcode
        # Live remote_url — same plumbing the publish row uses to
        # detect a freshly-pushed remote that hasn't propagated to
        # the cached Project record yet.
        live_remote_url = (project.remote_url or '').strip()
        try:
            ps = project_status(project.langcode)
        except Exception:
            ps = None
        if ps is not None and (ps.remote_url or '').strip():
            live_remote_url = ps.remote_url.strip()
        # Pre-0.45.0 this row was gated on ``live_remote_url`` being
        # non-empty because the button it carried was a github
        # collaborator-invite. 0.45.0 collapsed the row's button into
        # the consolidated "Share project" popup, which carries
        # paired-phones LAN-share + pair-QR + github-invite sections.
        # The first two work fine without a github remote — a user
        # who wants to share an unpublished project over LAN should
        # not have to Publish to GitHub first just to see the Share
        # button. So the gate is gone; the popup adapts internally
        # (hides the github-invite section when remote_url is empty).
        # GitHub backup-progress line (0.53.3). Only meaningful when
        # the project has a github remote; for LAN-only / unpublished
        # projects the info block already says "not published".
        backup_line = _github_backup_line(ps) if live_remote_url else ''
        if info is not None:
            if live_remote_url:
                info.text = _tr(
                    'Project: {langcode}\nRemote: {remote_url}'
                ).format(langcode=project.langcode,
                         remote_url=live_remote_url)
                if backup_line:
                    info.text = info.text + '\n' + backup_line
            else:
                info.text = _tr(
                    'Project: {langcode}\n'
                    '(not published to GitHub — '
                    'share over local network only)'
                ).format(langcode=project.langcode)
        # Heights (0.45.0): section label (dp 32) + info (~dp 40, +dp 20
        # for the optional backup line) + share-project button (dp 52) +
        # msg (dp 20). Spacing: dp(8) between children — now 4 gaps
        # since the peer-sync-this box sits between info and the share
        # button (0.54.38). The peer-sync box's own height is added on
        # top by ``_sync_actions_row_height`` (it's dynamic — grows with
        # the number of paired peers). Switch-project lives OUTSIDE this
        # row; the other-projects peer box does too.
        self._actions_row_base_h = (
            dp(32) + dp(40) + (dp(20) if backup_line else 0)
            + dp(52) + dp(20) + dp(8) * 4)
        row.opacity = 1
        self._sync_actions_row_height()
        # Restore the children that ``_detach_project_actions_children``
        # may have removed during a previous refresh while the row
        # was hidden. Idempotent if they're already attached.
        self._reattach_project_actions_children()

    def grant_collaborator(self):
        """Open the shared ``grant_collaborator_popup`` for the
        current project. The popup itself handles the GitHub-
        username input + server-side ``grant_collaborator`` RPC,
        and surfaces the resulting Result through its own status
        line — we just hand it the langcode and a status sink for
        post-dismiss feedback."""
        project = self._pick_publish_candidate()
        if project is None:
            self._set_project_actions_msg(_tr(
                'No project selected.'))
            return
        from azt_collab_client.ui.popups import grant_collaborator_popup
        grant_collaborator_popup(
            langcode=project.langcode,
            font_name=App.get_running_app().font_name,
            on_done=lambda _r: Clock.schedule_once(
                lambda _dt: self.refresh(), 0),
        )

    def switch_project(self):
        """Navigate to the project picker so the user can pick a
        different project. Only available when the host app's
        ScreenManager includes a ``'picker'`` screen — true for
        the unified server-APK app (since 0.41.22 picker+settings
        live in one Kivy App), false for the desktop-only
        settings-without-picker entry point (``python -m
        azt_collabd ui``).

        Falls back to a translated message on the inline status
        line when picker isn't reachable — keeps the button safe
        to bind regardless of host."""
        app = App.get_running_app()
        sm = getattr(app, 'sm', None)
        if sm is not None and sm.has_screen('picker'):
            app.go('picker')
            return
        self._set_project_actions_msg(_tr(
            'Switch project is unavailable from this entry point.'))

    def share_project(self):
        """Open the consolidated "Share {langcode} project" popup.
        Folds three sharing modes into one panel: paired phones
        list, in-person QR for a new phone, github invite for a
        remote collaborator. Replaced the three separate buttons
        (Grant collaborator + Share repo QR + …) in 0.45.0 to give
        the user a single entry point. The popup itself dispatches
        per the user's choice."""
        project = self._pick_publish_candidate()
        if project is None:
            self._set_project_actions_msg(_tr(
                'No project selected.'))
            return
        try:
            from azt_collab_client.ui.lan_popups import (
                share_project_popup,
            )
            share_project_popup(
                langcode=project.langcode,
                font_name=App.get_running_app().font_name)
        except Exception as ex:
            self._set_project_actions_msg(_tr(
                'Could not open share popup: {error}').format(
                    error=str(ex)))

    def share_repo_qr(self):
        """Open a popup that renders the current project's remote
        URL as a QR code. Pairs with the picker's "Scan QR to
        clone" affordance on the receiving device — the user
        flashes one device's QR at another, the receiver decodes
        the URL and pre-fills its clone textbox.

        Generates via ``segno`` (pure Python) → PNG bytes →
        ``CoreImage`` → Kivy ``Image`` widget. Also offers a
        "Copy URL" button as a fallback for receivers that don't
        have a camera or QR scanner available."""
        project = self._pick_publish_candidate()
        if project is None:
            self._set_project_actions_msg(_tr(
                'No project selected.'))
            return
        # Use live remote_url — same plumbing _refresh_project_actions_row
        # uses — so a freshly-published project shows the right URL
        # without a manual refresh.
        live_remote_url = (project.remote_url or '').strip()
        try:
            ps = project_status(project.langcode)
        except Exception:
            ps = None
        if ps is not None and (ps.remote_url or '').strip():
            live_remote_url = ps.remote_url.strip()
        if not live_remote_url:
            self._set_project_actions_msg(_tr(
                'No remote URL — publish first.'))
            return
        _show_share_repo_qr_popup(
            url=live_remote_url,
            langcode=project.langcode,
            font_name=App.get_running_app().font_name,
        )

    def set_cawl_prefetch_mode(self, all_variants):
        """Set the CAWL prefetch policy explicitly. Bound from the
        two buttons in the "Cache images:" row (False = "1 per
        line", True = "all"). Idempotent — tapping the active
        button is a no-op on the daemon side.

        Takes effect on the next ``auto_prefetch`` trigger (next
        project-load / scheduler edge); does NOT retroactively
        re-warm an in-flight worker. Existing on-disk cache
        entries are preserved either way."""
        new_state = bool(all_variants)
        set_cawl_prefetch_all_variants(new_state)
        self._cawl_variants_all = new_state
        self._refresh_cawl_variants_buttons()

    def _refresh_cawl_variants_state(self):
        """Read the daemon's current prefetch policy + active
        wordlist name. Called from ``refresh()`` so the button
        highlight and section label are correct on screen entry.
        Tolerant of transport failure on either side (policy
        defaults to False, label falls back to the generic
        translation)."""
        self._cawl_variants_all = bool(get_cawl_prefetch_all_variants())
        self._refresh_cawl_variants_buttons()
        self._refresh_cawl_section_label()

    def _refresh_cawl_variants_buttons(self):
        all_variants = bool(getattr(self, '_cawl_variants_all', False))
        one_btn = self.ids.get('cawl_variants_one_btn')
        all_btn = self.ids.get('cawl_variants_all_btn')
        # Active button gets GREEN, inactive gets SURFACE. Matches
        # the language-selector row's highlight convention.
        if one_btn is not None:
            one_btn.normal_color = (
                theme.SURFACE if all_variants else theme.GREEN)
        if all_btn is not None:
            all_btn.normal_color = (
                theme.GREEN if all_variants else theme.SURFACE)

    def _refresh_cawl_section_label(self):
        """Update the wordlist section label with the active
        wordlist name. Source: ``cawl_cache_status(langcode)``
        carries ``image_repo`` already, so we don't need a new RPC.
        Empty langcode or empty image_repo falls back to the
        generic ``Wordlist images`` label."""
        label = self.ids.get('cawl_section_label')
        if label is None:
            return
        langcode = (last_project() or '').strip()
        repo = ''
        if langcode:
            try:
                status = cawl_cache_status(langcode)
                repo = (status.get('image_repo') or '').strip()
            except Exception:
                repo = ''
        from azt_collabd import cawl as _cawl_mod
        name = _cawl_mod.wordlist_name(repo)
        if name:
            label.text = _tr('Wordlist ({name}) images').format(name=name)
        else:
            label.text = _tr('Wordlist images')

    def set_work_offline_mode(self, enabled):
        """Set the work-offline toggle explicitly. Bound from the
        yes / no buttons. Idempotent.

        Toggling OFF triggers an immediate push-drain server-side
        so pending commits go out without waiting a full
        connectivity_poll_s tick (the daemon handles this in
        ``_h_set_work_offline``)."""
        from azt_collab_client import set_work_offline as _swo
        new_state = bool(enabled)
        try:
            applied = _swo(new_state)
        except Exception as ex:
            status = self.ids.get('work_offline_status')
            if status is not None:
                status.text = _tr(
                    'Failed to update work-offline setting: {error}').format(
                        error=str(ex))
            return
        self._work_offline_enabled = bool(applied)
        self._refresh_work_offline_buttons()
        self._refresh_work_offline_status_text(
            just_toggled_off=not self._work_offline_enabled)

    def _refresh_work_offline_state(self):
        """Read the daemon's current toggle state. Called from
        ``refresh()`` so the button highlight is correct on
        screen entry."""
        from azt_collab_client import get_work_offline as _gwo
        try:
            self._work_offline_enabled = bool(_gwo())
        except Exception:
            self._work_offline_enabled = False
        self._refresh_work_offline_buttons()
        self._refresh_work_offline_status_text(just_toggled_off=False)

    def _refresh_work_offline_status_text(self, just_toggled_off=False):
        """Compose the status line under the Work-offline yes/no
        buttons. Considers BOTH ``work_offline`` and ``lan.allow_sync``
        so the user sees the actual delivery shape:

          - work_offline=off → only show a transient "pushing enabled"
            confirmation when the user just flipped it off; otherwise
            the row is silent (default state needs no label).
          - work_offline=on + LAN=off → push fully suppressed; commits
            accumulate.
          - work_offline=on + LAN=on  → "LAN-only": github push
            suppressed but paired phones still receive commits over
            the local network."""
        status = self.ids.get('work_offline_status')
        if status is None:
            return
        if not self._work_offline_enabled:
            if just_toggled_off:
                status.text = _tr(
                    'Pushing enabled. Pending commits will go out '
                    'when the network is reachable.')
            else:
                status.text = ''
            return
        try:
            from azt_collab_client import lan_toggle as _lt
            lan_on = bool(_lt().get('on'))
        except Exception:
            lan_on = False
        if lan_on:
            status.text = _tr(
                'LAN-only mode. GitHub push is suppressed, but '
                'paired phones still receive commits over the '
                'local network.')
        else:
            status.text = _tr(
                'Push suppressed. Commits will accumulate locally '
                'until you turn this off.')

    def _refresh_work_offline_buttons(self):
        enabled = bool(getattr(self, '_work_offline_enabled', False))
        yes_btn = self.ids.get('work_offline_yes_btn')
        no_btn = self.ids.get('work_offline_no_btn')
        if yes_btn is not None:
            yes_btn.normal_color = theme.GREEN if enabled else theme.SURFACE
        if no_btn is not None:
            no_btn.normal_color = theme.SURFACE if enabled else theme.GREEN

    # LAN sync (parked design, 0.45.0). Toggle + open-pair / open-list
    # entry points; the actual UI lives in
    # ``azt_collab_client.ui.lan_popups`` so the picker can reuse the
    # scan-to-pair flow from the same module.

    def set_lan_allow_sync(self, enabled):
        from azt_collab_client import lan_set_toggle as _lst
        new_state = bool(enabled)
        try:
            applied = _lst(new_state)
        except Exception as ex:
            label = self.ids.get('lan_status_label')
            if label is not None:
                label.text = _tr(
                    'Failed to update local-network setting: {error}'
                ).format(error=str(ex))
            return
        self._lan_enabled = bool(applied.get('on'))
        self._lan_endpoint = applied.get('endpoint', '')
        self._refresh_lan_buttons()
        self._refresh_lan_status()
        # The work-offline status text describes the joint state
        # of (work_offline, lan_allow_sync) — flipping LAN flips
        # the meaning from "fully offline" to "LAN-only" without
        # changing the work_offline toggle itself. Re-render so
        # the user sees the change immediately.
        try:
            self._refresh_work_offline_status_text(
                just_toggled_off=False)
        except Exception:
            pass

    def _refresh_lan_state(self):
        from azt_collab_client import lan_toggle as _lt
        try:
            state = _lt()
        except Exception:
            state = {'on': False, 'endpoint': ''}
        self._lan_enabled = bool(state.get('on'))
        self._lan_endpoint = state.get('endpoint', '')
        self._refresh_lan_buttons()
        self._refresh_lan_status()

    def _refresh_lan_buttons(self):
        enabled = bool(getattr(self, '_lan_enabled', False))
        yes_btn = self.ids.get('lan_yes_btn')
        no_btn = self.ids.get('lan_no_btn')
        if yes_btn is not None:
            yes_btn.normal_color = theme.GREEN if enabled else theme.SURFACE
        if no_btn is not None:
            no_btn.normal_color = theme.SURFACE if enabled else theme.GREEN

    def _refresh_lan_status(self):
        label = self.ids.get('lan_status_label')
        if label is None:
            return
        enabled = bool(getattr(self, '_lan_enabled', False))
        endpoint = getattr(self, '_lan_endpoint', '') or ''
        if not enabled:
            label.text = ''
            return
        if endpoint:
            label.text = _tr('Listening on {endpoint}').format(
                endpoint=endpoint)
        else:
            label.text = _tr('Local-network sharing is on (listener '
                             'not yet bound).')

    def open_pair_phone(self):
        try:
            from azt_collab_client.ui.lan_popups import (
                share_pairing_qr_popup,
            )
            share_pairing_qr_popup()
        except Exception as ex:
            label = self.ids.get('lan_status_label')
            if label is not None:
                label.text = _tr('Could not open pairing QR: '
                                 '{error}').format(error=str(ex))

    def open_paired_phones(self):
        try:
            from azt_collab_client.ui.lan_popups import (
                paired_phones_popup,
            )
            paired_phones_popup()
        except Exception as ex:
            label = self.ids.get('lan_status_label')
            if label is not None:
                label.text = _tr('Could not open paired-devices '
                                 'list: {error}').format(error=str(ex))

    def share_diagnostics(self):
        """Daemon-settings affordance for the canonical share-
        diagnostics action (since 0.52.8). Same label and same
        underlying ``share_diagnostics_action`` the picker's
        button fires — two affordances over one collapsed
        implementation. The settings surface exists for the
        common case where the user is already in settings (just
        configured something, just compared two settings, etc.)
        and wants to share what just happened without
        navigating back to the picker first."""
        from azt_collab_client.ui.share import share_diagnostics_action
        status = self.ids.get('service_status')

        def _on_err(msg):
            if status is not None:
                status.text = msg
        share_diagnostics_action(on_error=_on_err)

    def restart_server(self):
        """Ask the daemon to restart itself. The settings UI lives in
        a separate process (``python -m azt_collabd ui`` on desktop;
        PythonActivity on Android) from the daemon process, so the
        restart is safe from here — the UI keeps running. Status
        shows up in the ``service_status`` label below the button.

        Two paths:

        1. **Cooperative.** ``client.restart_server()`` POSTs
           ``/v1/admin/restart``. The daemon flushes the response,
           waits 0.5 s, then re-execs (desktop) or ``_exit(0)``s
           (Android, where the ContentProvider contract auto-spawns
           the next caller).

        2. **Non-cooperative fallback.** Step 1 fails when the
           running daemon is too old to know the endpoint (the
           exact case the user usually wants Restart for — "I just
           installed a new APK and need to get rid of the old
           daemon"), or when the daemon is wedged in a long-running
           op and not draining its dispatch queue. Fall through to
           the same kill-by-PID mechanism
           ``SuiteSelfReplaceReceiver`` uses on
           ``ACTION_MY_PACKAGE_REPLACED``: enumerate same-UID
           sibling processes (Android) or read
           ``$AZT_HOME/server.json`` and ``os.kill`` (desktop).
           Same-UID kill needs no permission, ignores process-
           importance, and works regardless of the daemon's
           version.
        """
        from azt_collab_client import restart_server as _restart
        status = self.ids.get('service_status')

        def _do():
            res = _restart()

            def _show(text):
                if status is not None:
                    status.text = text
            if res.has(S.RESTARTING):
                # Desktop re-exec can take a few seconds while the
                # new interpreter boots; Android `:provider` lazy-
                # respawn is sub-second. Either way, the next RPC
                # from this UI re-discovers the daemon.
                Clock.schedule_once(
                    lambda *_: _show(_tr(
                        'Sync service is restarting…'
                    )), 0)
                # The bottom version strip (``app.version_string``,
                # rendered ``client X · server Y``) is captured at
                # startup via ``_probe_server_version`` and never
                # refreshed. After a successful restart the
                # ``server Y`` half is stale. Re-probe so the user
                # sees the new daemon's version land. Delayed 2 s
                # so the new daemon has time to come up and answer.
                self._refresh_version_strip_after_restart()
                return
            if res.has_any(S.SERVER_UNAVAILABLE, S.SERVER_ERROR):
                # Cooperative path declined. Try the force-kill
                # fallback. Path of resort exists because users hit
                # this exact button when the daemon is too old to
                # honour /v1/admin/restart (the upgrade case) or is
                # otherwise unresponsive.
                killed, detail = self._force_kill_daemon_process()
                if killed:
                    Clock.schedule_once(
                        lambda *_: _show(_tr(
                            'Sync service is restarting…'
                        )), 0)
                    self._refresh_version_strip_after_restart()
                else:
                    Clock.schedule_once(
                        lambda *_: _show(_tr(
                            'Could not reach the sync service to '
                            'restart it.'
                        ) + (f'\n({detail})' if detail else '')), 0)
                return
            Clock.schedule_once(
                lambda *_: _show(_tr(
                    'Restart request returned an unexpected '
                    'response.'
                )), 0)

        # Run off the UI thread — restart_server() is a blocking RPC
        # that includes the daemon's 0.5 s pre-exit delay.
        threading.Thread(
            target=_do, daemon=True, name='ui-restart-server').start()

    def _refresh_version_strip_after_restart(self):
        """Re-probe the daemon's version after a restart so the
        ``client X · server Y`` strip at the bottom of the settings
        page reflects the new running daemon. Without this the user
        sees the toast "Sync service is restarting…" but the version
        string stays frozen at whatever was captured at app startup,
        so the restart looks like it did nothing visible.

        Two App classes host this SettingsScreen: ``CollabUIApp``
        (desktop, has ``_probe_server_version``) and ``PickerApp``
        (Android, also has ``_probe_server_version``). Both expose
        the same method on the running App, so call through
        ``App.get_running_app()``. Best-effort: any failure (App
        class without the probe method, scheduling exception) is
        silent — the strip just stays stale.

        Delay matches ``_post_install_continuation``: 2 s gives the
        new daemon time to come up and answer ``check_server_compat``.
        """
        from kivy.app import App as _App

        def _do_probe(_dt):
            try:
                app = _App.get_running_app()
                if app is not None and hasattr(
                        app, '_probe_server_version'):
                    import threading as _th
                    _th.Thread(target=app._probe_server_version,
                               daemon=True).start()
            except Exception as ex:
                print(f'[settings] version strip refresh failed: '
                      f'{ex}', flush=True)
        Clock.schedule_once(_do_probe, 2.0)

    def _force_kill_daemon_process(self):
        """Non-cooperative kill of the daemon process.

        Returns ``(killed: bool, detail: str)``. ``detail`` is a
        short diagnostic string for the toast on failure (empty on
        success). Same UID as the daemon → no Android permission
        and no desktop privilege escalation needed.

        On **Android** the daemon lives in the
        ``org.atoznback.aztcollab:provider`` process; this method
        runs in the PythonActivity main process. ``ActivityManager
        .getRunningAppProcesses()`` returns same-UID processes,
        ``Process.killProcess(pid)`` reaps non-self PIDs regardless
        of importance (the sticky-bound service pins ``:provider``
        at ``IMPORTANCE_SERVICE``, which
        ``killBackgroundProcesses`` can't touch — but per-PID kill
        bypasses that).

        On **desktop** the daemon is a sibling child process
        spawned by ``transports/loopback`` auto-spawn; its PID is
        in ``$AZT_HOME/server.json``. ``os.kill(pid, SIGTERM)``
        terminates it; the next client RPC re-discovers via the
        auto-spawn path.
        """
        try:
            on_android = ('ANDROID_ARGUMENT' in os.environ
                          or 'ANDROID_BOOTLOGO' in os.environ)
            if on_android:
                return self._force_kill_daemon_android()
            return self._force_kill_daemon_desktop()
        except Exception as ex:
            return False, f'force-kill error: {ex}'

    def _force_kill_daemon_android(self):
        """jnius path: enumerate same-UID processes, kill non-self
        pids. Mirrors ``SuiteSelfReplaceReceiver`` steps 1–2, minus
        the self-kill (we keep the UI alive)."""
        try:
            from jnius import autoclass
        except Exception as ex:
            return False, f'jnius unavailable: {ex}'
        try:
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            Context = autoclass('android.content.Context')
            Process = autoclass('android.os.Process')
            activity = PythonActivity.mActivity
            am = activity.getSystemService(Context.ACTIVITY_SERVICE)
            my_pid = Process.myPid()
            procs = am.getRunningAppProcesses()
            killed_any = False
            killed_names = []
            if procs is not None:
                # Iterate via Java List<RunningAppProcessInfo>;
                # jnius exposes .size()/.get(i) on java.util.List.
                for i in range(procs.size()):
                    info = procs.get(i)
                    pid = info.pid
                    if pid == my_pid:
                        continue
                    try:
                        Process.killProcess(pid)
                        killed_any = True
                        killed_names.append(
                            f'{info.processName}:{pid}')
                    except Exception:
                        pass
            # Fallback: killBackgroundProcesses for anything step 1
            # missed. Server APK has the permission injected; on
            # peer APKs this throws SecurityException, which is
            # fine — step 1 already handled the load-bearing case.
            try:
                am.killBackgroundProcesses(activity.getPackageName())
            except Exception:
                pass
            if killed_any:
                print(f'[settings] force-killed: {killed_names!r}',
                      flush=True)
                return True, ''
            return False, 'no sibling processes found to kill'
        except Exception as ex:
            return False, f'android kill failed: {ex}'

    def _force_kill_daemon_desktop(self):
        """Read pid from $AZT_HOME/server.json, send SIGTERM."""
        import json
        import signal
        from azt_collab_client.paths import azt_home
        try:
            info_path = os.path.join(azt_home(), 'server.json')
            with open(info_path) as f:
                info = json.load(f)
            pid = int(info.get('pid') or 0)
            if pid <= 0:
                return False, 'no pid in server.json'
            os.kill(pid, signal.SIGTERM)
            return True, ''
        except FileNotFoundError:
            return False, 'no server.json (daemon not running)'
        except ProcessLookupError:
            return False, 'pid stale (daemon already gone)'
        except Exception as ex:
            return False, f'desktop kill failed: {ex}'

    def _set_project_actions_msg(self, text):
        msg = self.ids.get('project_actions_msg')
        if msg is not None:
            msg.text = text or ''

    def _refresh_publish_row(self, status):
        """Show / hide / enable the "Publish <langcode> data" row.

        Picks the candidate project in this priority order:

        1. ``last_project()`` (suite-wide "last opened" key). The
           recorder updates it via ``set_last_project`` when a project
           is chosen through the picker.
        2. If that's empty or doesn't resolve to a registered project,
           fall back to scanning ``list_projects()`` and picking the
           one with the largest ``last_sync`` (most recently active).
           This covers users on older recorder versions that load a
           project without going through the picker — without this
           fallback the publish row would silently stay hidden even
           though the daemon clearly has a project on file.

        The row is visible only when a candidate is found AND that
        project has no remote yet. The button is enabled iff at least
        one host's credentials are confirmed."""
        row = self.ids.get('publish_row')
        btn = self.ids.get('publish_btn')
        msg = self.ids.get('publish_msg')
        if row is None or btn is None:
            return
        # Reset to hidden by default; the rest flips it back on only
        # if every condition holds. Detach children so the
        # publish_btn (a RecBtn with on_press) can't intercept
        # touches that should reach gl_action_btn just above it —
        # same hide-by-detach pattern used in
        # GitHubConnectScreen.
        row.height = 0
        row.opacity = 0
        if msg is not None:
            msg.text = ''
        self._detach_publish_children()
        project = self._pick_publish_candidate()
        if project is None:
            return
        # Authoritative remote_url comes from project_status, which
        # reads the live git config. The Project's remote_url is the
        # cached projects.json value — pre-0.20.1 daemons forgot to
        # write it back on a successful init_project, so existing
        # published projects can have an empty cached remote_url even
        # though their working_dir's `.git/config` lists origin
        # correctly. Trusting Project.remote_url alone would re-show
        # the publish button on those repos forever.
        live_remote_url = (project.remote_url or '').strip()
        try:
            ps = project_status(project.langcode)
        except Exception:
            ps = None
        if ps is not None and (ps.remote_url or '').strip():
            live_remote_url = ps.remote_url.strip()
        if live_remote_url:
            return
        langcode = project.langcode
        gh_confirmed = bool(status.get('github', {}).get('confirmed'))
        gl_confirmed = bool(status.get('gitlab', {}).get('confirmed'))
        n_confirmed = int(gh_confirmed) + int(gl_confirmed)
        btn.text = _tr('Publish {langcode} data').format(langcode=langcode)
        btn.disabled = (n_confirmed == 0)
        btn.normal_color = (
            theme.GREEN if n_confirmed else theme.BTN_INACTIVE)
        if msg is not None and n_confirmed == 0:
            msg.text = _tr(
                'Connect to GitHub or GitLab first to enable publish.')
        # Heights: button + message (BodyLabel is dp(20)) + spacing.
        row.height = dp(52) + dp(8) + dp(20)
        row.opacity = 1
        # Restore the children that ``_detach_publish_children`` may
        # have removed if the row was previously hidden during this
        # screen's lifetime. Idempotent if they're already attached.
        self._reattach_publish_children()

    # ── publish_row child detach (anti-touch-intercept) ──────────────
    #
    # ``publish_row`` defaults to ``height=0`` and sits directly under
    # ``gl_action_btn``. Even with the parent collapsed, BoxLayout's
    # ``_do_layout`` keeps positioning the inner ``publish_btn`` (a
    # RecBtn with on_press) at its explicit dp(52) height — far enough
    # below ``gl_action_btn`` not to overlap on paper, but Kivy's
    # touch dispatch loop visits every child regardless and the
    # combination has produced "GitLab button doesn't respond until
    # 10-12 clicks" reports. Same hide-by-detach pattern that fixed
    # the equivalent ``gh_primary_btn`` issue in
    # GitHubConnectScreen.

    _publish_detached = None  # list[Widget] | None — strong ref while detached

    def _detach_publish_children(self):
        row = self.ids.get('publish_row')
        if row is None:
            return
        if self._publish_detached is not None:
            return  # already detached; idempotent
        kids = list(row.children)  # reverse-add order
        # Restore order on reattach is "first-added first," so capture
        # via reversed().
        self._publish_detached = list(reversed(kids))
        for c in kids:
            row.remove_widget(c)

    def _reattach_publish_children(self):
        row = self.ids.get('publish_row')
        if row is None:
            return
        kids = self._publish_detached
        if not kids:
            return  # nothing to restore (already attached, or never detached)
        self._publish_detached = None
        for c in kids:
            row.add_widget(c)

    # ── project_actions_row child detach (anti-touch-intercept) ──────
    #
    # Same shape as publish_row's detach/reattach. ``project_actions_row``
    # defaults to ``height=0, opacity=0`` and sits below the GitHub +
    # GitLab Connect buttons + the publish_row. Even with the parent
    # collapsed, BoxLayout's ``_do_layout`` still positions the inner
    # RecBtns (grant_collab_btn, share_repo_btn, switch_project_btn)
    # at their explicit dp(52) heights — Kivy's touch dispatch loop
    # then visits them and an ``on_press: root.grant_collaborator()``
    # fires when the user thinks they tapped Connect to GitHub. Same
    # symptom across the three children: their on_press handlers
    # silently hijack taps meant for buttons higher in the screen.
    # Detaching the children removes them from the touch tree entirely.

    _project_actions_detached = None

    def _detach_project_actions_children(self):
        row = self.ids.get('project_actions_row')
        if row is None:
            return
        if self._project_actions_detached is not None:
            return  # already detached; idempotent
        kids = list(row.children)
        # ``children`` is in reverse-add order; flip so reattach
        # produces the same top-to-bottom layout as the KV rule.
        self._project_actions_detached = list(reversed(kids))
        for c in kids:
            row.remove_widget(c)

    def _reattach_project_actions_children(self):
        row = self.ids.get('project_actions_row')
        if row is None:
            return
        kids = self._project_actions_detached
        if not kids:
            return  # nothing to restore (already attached, or never detached)
        self._project_actions_detached = None
        for c in kids:
            row.add_widget(c)

    def _pending_adopt_origin_url(self, langcode):
        """Return the URL from a pending ``adopt_origin`` decision
        for *langcode*, or ``''`` if none exists. Lets Publish
        adopt a peer's existing github repo instead of inferring
        a new ``<user>/<langcode>`` path — see ``_do_publish``
        for context. 0.45.37."""
        try:
            from .. import lan_pending  # type: ignore
        except (ImportError, ValueError):
            try:
                from azt_collab_client import lan_pending
            except Exception:
                return ''
        try:
            decisions = lan_pending() or []
        except Exception:
            return ''
        for d in decisions:
            if not isinstance(d, dict):
                continue
            if d.get('kind') != 'adopt_origin':
                continue
            payload = d.get('payload') or {}
            if not isinstance(payload, dict):
                continue
            if str(payload.get('langcode') or '') != langcode:
                continue
            url = str(payload.get('url') or '').strip()
            if url:
                return url
        return ''


    def _pick_publish_candidate(self):
        """Return the Project the daemon last touched, or ``None`` if
        there isn't one (no project ever opened on this device, server
        unreachable, or stale registry).

        ``last_project()`` is server-tracked from azt_collabd 0.19+ /
        client 0.23+: every langcode-bound RPC auto-stamps via
        ``server._touch_project``, so the langcode that comes back is
        always the most recently active project regardless of which
        peer touched it. No fallback scanning is needed — if no
        project has ever been touched, hiding the publish row is the
        correct UX."""
        import sys
        langcode = (last_project() or '').strip()
        if not langcode:
            print('[settings] last_project: empty (no project '
                  'touched on this device yet)',
                  file=sys.stderr, flush=True)
            return None
        try:
            project = open_project(langcode)
        except Exception as ex:
            print(f'[settings] open_project({langcode!r}) raised: '
                  f'{ex}', file=sys.stderr, flush=True)
            return None
        if project is None or not project.lift_exists:
            print(f'[settings] last_project={langcode!r} no longer '
                  f'resolves to a live project',
                  file=sys.stderr, flush=True)
            return None
        print(f'[settings] publish candidate: {langcode!r} '
              f'(remote_url={project.remote_url!r})',
              file=sys.stderr, flush=True)
        return project

    def _focus_contributor_if_unset(self):
        """On screen entry, if the contributor name is empty, focus
        the input and surface an inline reason. Avoids the peer-side
        ``S.CONTRIBUTOR_UNSET`` toast getting blown away by the
        screen transition before the user can read it — the field
        coming up with the keyboard active makes the missing-value
        story obvious without an overlay.

        Defensive: silent on missing widget (UI not yet built), and
        only takes focus if the field is empty AND not already
        focused (don't yank focus from a user typing)."""
        inp = self.ids.get('contributor_input')
        msg = self.ids.get('contributor_msg')
        if inp is None:
            return
        if (inp.text or '').strip():
            return
        if inp.focus:
            return
        inp.focus = True
        if msg is not None:
            msg.text = _tr(
                'Required: your name is used to label your work on '
                'both Internet sync (GitHub commits) and local-network '
                'sync (peer label other phones see). Sync refuses '
                'until this is set.')
            msg.color = theme.RED

    def save_contributor(self):
        """Called on the contributor input losing focus. Persists the
        trimmed value to the server (config.json :: collab.contributor)
        and shows a transient confirmation.

        Empty input is NOT treated as a successful save: tapping
        outside an empty field would otherwise overwrite the
        ``Required: …`` red message with a green ``Saved.``, which
        looks like the empty value was accepted. Instead we leave
        whatever message was up (typically the on-entry
        ``Required: …`` from ``refresh_settings``) in place.

        Runs the RPC on a worker thread so a focus-loss triggered by
        tapping a sibling button (e.g. GitLab) doesn't block the UI
        thread mid-tap — that produced a "button resists pressing"
        symptom where the user had to tap several times because the
        first taps landed during the synchronous RPC window."""
        inp = self.ids.get('contributor_input')
        if inp is None:
            return
        name = (inp.text or '').strip()
        if not name:
            # Don't fire the RPC and don't claim "Saved." — the
            # field is still empty, the user hasn't actually set
            # anything, and the on-entry "Required:" message is
            # the right thing to keep showing.
            return
        threading.Thread(
            target=self._save_contributor_worker,
            args=(name,), daemon=True).start()

    def _save_contributor_worker(self, name):
        try:
            set_contributor(name)
            err = None
        except Exception as ex:
            err = str(ex)
        Clock.schedule_once(
            lambda dt: self._save_contributor_done(err), 0)

    def _save_contributor_done(self, err):
        msg = self.ids.get('contributor_msg')
        if msg is None:
            return
        if err is not None:
            msg.text = _tr('Error: {error}').format(error=err)
            msg.color = theme.RED
            return
        msg.text = _tr('Saved.')
        # Drop the on-entry "Required" red if it was up.
        msg.color = theme.TEXT_DIM
        Clock.schedule_once(lambda dt: setattr(msg, 'text', ''), 2.0)

    # ``gh_action`` / ``connect_github`` removed in 0.30.8: the KV
    # button now just calls ``app.go('github')`` directly. Disconnect
    # logic lives inside ``GitHubConnectScreen`` (and inside
    # ``GitLabFormScreen`` for GitLab); the settings screen is purely
    # a navigation hub now.

    def publish(self):
        """Create a remote repo on a confirmed git host for the
        most-recently-used project and push the local working tree up.

        Mirrors the recorder's ``do_publish`` flow (main.py:2884) but
        runs from the daemon's settings UI so any peer that hands the
        user into the gear can publish without owning the publish UI.
        Host pick: single-confirmed → use it; both → overlay."""
        # Re-pick rather than trusting a captured langcode — the user
        # may have changed projects in another peer between the row
        # rendering and this click.
        project = self._pick_publish_candidate()
        if project is None:
            return  # button shouldn't have been visible
        langcode = project.langcode
        status = get_credentials_status()
        confirmed = []
        if status.get('github', {}).get('confirmed'):
            confirmed.append('github')
        if status.get('gitlab', {}).get('confirmed'):
            confirmed.append('gitlab')
        if not confirmed:
            self._set_publish_msg(_tr(
                'Connect to GitHub or GitLab first to enable publish.'))
            return
        if len(confirmed) == 1:
            self._do_publish(langcode, confirmed[0], status)
            return
        self._show_host_picker(langcode, status)

    def _show_host_picker(self, langcode, status):
        """Modal with one button per confirmed host, plus Cancel."""
        from kivy.graphics import Color, RoundedRectangle
        from kivy.uix.modalview import ModalView
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.label import Label
        view = ModalView(size_hint=(0.85, None), height=dp(280),
                         auto_dismiss=False,
                         background_color=theme.OVERLAY_DARK)
        box = BoxLayout(orientation='vertical', padding=dp(16),
                        spacing=dp(12))
        with box.canvas.before:
            Color(*theme.SURFACE)
            box._bg = RoundedRectangle(pos=box.pos, size=box.size,
                                       radius=[dp(8)])
        box.bind(pos=lambda w, p: setattr(w._bg, 'pos', p),
                 size=lambda w, s: setattr(w._bg, 'size', s))
        prompt = Label(
            text=_tr('Publish to which service?'),
            color=theme.TEXT, font_size=sp(15),
            size_hint_y=None, height=dp(48),
            halign='center', valign='middle')
        prompt.bind(size=lambda w, s: setattr(w, 'text_size', s))
        box.add_widget(prompt)

        def _make_btn(label, fill, on_release):
            btn = Button(
                text=label, size_hint_y=None, height=dp(48),
                background_color=theme.TRANSPARENT, background_normal='',
                color=(1, 1, 1, 1), font_size=sp(16), bold=True)
            with btn.canvas.before:
                Color(*fill)
                btn._bg = RoundedRectangle(pos=btn.pos, size=btn.size,
                                           radius=[dp(8)])
            btn.bind(pos=lambda w, p: setattr(w._bg, 'pos', p),
                     size=lambda w, s: setattr(w._bg, 'size', s))
            btn.bind(on_release=on_release)
            return btn

        def _pick(host):
            view.dismiss()
            self._do_publish(langcode, host, status)

        box.add_widget(_make_btn(
            'GitHub', theme.GREEN, lambda *_: _pick('github')))
        box.add_widget(_make_btn(
            'GitLab', theme.GREEN, lambda *_: _pick('gitlab')))
        box.add_widget(_make_btn(
            _tr('Cancel'), theme.SURFACE, lambda *_: view.dismiss()))
        view.add_widget(box)
        view.open()

    def _do_publish(self, langcode, host, status):
        """Resolve project + URL, kick the worker thread."""
        block = status.get(host, {}) or {}
        user = (block.get('username', '') or '').strip()
        if not user:
            self._set_publish_msg(_tr(
                'No {host} username on file.').format(host=host))
            return
        project = open_project(langcode)
        if project is None or not project.working_dir:
            self._set_publish_msg(_tr(
                'Project {langcode} not found.').format(langcode=langcode))
            return
        # Defensive guard: ``_refresh_publish_row`` already hides
        # the row when ``project_status.remote_url`` is non-empty,
        # but a stale-bind to ``on_release`` could still land here
        # after a successful adopt-origin / publish elsewhere.
        # Refuse rather than create a duplicate URL — the
        # ``REMOTE_OWNER_MISMATCH_SKIP_CREATE`` orphan-repo guard
        # in ``_ensure_remote_repo`` would catch the worst case,
        # but a no-op refusal here is cheaper and gives a clearer
        # message. 0.50.27.
        existing_url = ''
        try:
            ps = project_status(langcode)
        except Exception:
            ps = None
        if ps is not None and (ps.remote_url or '').strip():
            existing_url = ps.remote_url.strip()
        elif (project.remote_url or '').strip():
            existing_url = project.remote_url.strip()
        if existing_url:
            self._set_publish_msg(_tr(
                'Project {langcode} is already published at '
                '{url}.').format(
                    langcode=langcode, url=existing_url))
            return
        domain = 'gitlab.com' if host == 'gitlab' else 'github.com'
        remote_url = f'https://{domain}/{user}/{langcode}.git'
        # If a ``LAN_ADOPT_ORIGIN_NEEDED`` pending decision exists
        # for this langcode (stashed by ``lan_clone`` when the peer
        # shared a project that already had a github origin), use
        # ITS URL instead of the inferred ``<user>/<langcode>``.
        # This is the recovery path for a user who missed (or
        # never saw) the in-flow adopt-origin popup at scan time
        # — Publish becomes the unified entry point: existing
        # peer-repo gets adopted; otherwise a new repo gets
        # created at the inferred path. 0.45.37.
        adopt_url = self._pending_adopt_origin_url(langcode)
        if adopt_url:
            remote_url = adopt_url
            print(f'[publish] using pending adopt_origin URL for '
                  f'{langcode!r}: {remote_url!r}',
                  file=sys.stderr, flush=True)
        # 0.40.0: contributor is daemon-owned; we don't pass it on the
        # wire any more. ``init_project`` reads from ``store.get_contributor()``
        # itself; if unset, it returns ``Result(CONTRIBUTOR_UNSET)`` and
        # the message routes the user back to the "Your name" field
        # above (which is on this same screen).
        btn = self.ids.get('publish_btn')
        if btn is not None:
            btn.disabled = True
        self._set_publish_msg(_tr('Publishing to {url}...').format(
            url=remote_url))
        threading.Thread(
            target=self._publish_worker,
            args=(project.working_dir, remote_url),
            daemon=True).start()

    def _publish_worker(self, working_dir, remote_url):
        import sys
        print(f'[publish] init_project working_dir={working_dir!r} '
              f'remote_url={remote_url!r}',
              file=sys.stderr, flush=True)
        try:
            result = init_project(working_dir, remote_url,
                                  branch='main')
            codes = result.codes()
            print(f'[publish] init_project done: codes={codes!r}',
                  file=sys.stderr, flush=True)
            text = translate_result(result) or _tr('Done.')
            # SERVER_UNAVAILABLE / SERVER_ERROR are wire-only codes
            # (no S.* alias); rpc-failure wrappers stamp them as
            # bare-string statuses, so check by string. The auth
            # codes do have S.* aliases.
            ok = not result.has_any(
                'SERVER_UNAVAILABLE', 'SERVER_ERROR',
                S.AUTH_REQUIRED, S.APP_NOT_INSTALLED,
                S.REPO_NOT_AUTHORIZED, S.ACCESS_DENIED,
                S.CONTRIBUTOR_UNSET)
        except Exception as ex:
            print(f'[publish] init_project raised: '
                  f'{type(ex).__name__}: {ex}',
                  file=sys.stderr, flush=True)
            text = _tr('Publish failed: {error}').format(error=str(ex))
            ok = False
        Clock.schedule_once(
            lambda dt: self._publish_done(text, ok), 0)

    def _publish_done(self, msg, ok):
        # Refresh first so a successful publish (which now leaves the
        # project with a populated remote_url) hides the row. Set the
        # outcome message AFTER refresh — _refresh_publish_row's first
        # act is to clear msg.text, so doing it the other way around
        # silently wipes the outcome the user needs to see.
        self.refresh()
        self._set_publish_msg(msg)
        if not ok:
            # Failure: row is still visible (remote_url stayed empty);
            # re-enable the button so the user can retry once they've
            # fixed whatever the message describes.
            btn = self.ids.get('publish_btn')
            if btn is not None:
                btn.disabled = False

    def _set_publish_msg(self, text):
        msg = self.ids.get('publish_msg')
        if msg is not None:
            msg.text = text or ''

    # ``disconnect_github`` / ``disconnect_gitlab`` were removed in
    # 0.30.8 along with their settings-screen entry points.
    # ``GitHubConnectScreen.disconnect`` and
    # ``GitLabFormScreen.disconnect`` are the canonical paths now —
    # both reachable from the corresponding "* Settings" entry on
    # SettingsScreen.

    # ── Debug helpers ───────────────────────────────────────────────────────

    def _debug_503_path(self):
        import os
        from azt_collabd.paths import azt_home
        return os.path.join(azt_home(), '_debug_force_503')

    def _refresh_debug_503_state(self):
        """Update the debug-section label to reflect whether the
        sentinel exists. Called from on_pre_enter / refresh / after
        toggle so the indicator stays in sync."""
        import os
        state_label = self.ids.get('debug_503_state')
        if state_label is None:
            return
        if os.path.exists(self._debug_503_path()):
            state_label.text = _tr(
                '/v1/health is forced to 503 (sentinel present).')
        else:
            state_label.text = _tr('/v1/health responds normally.')

    def toggle_debug_503(self):
        """Create or remove ``$AZT_HOME/_debug_force_503``. Daemon's
        ``_h_health`` checks this file per-request, so toggle takes
        effect on the next bootstrap probe — no daemon restart
        needed. Used to exercise the
        ``_prompt_server_unresponsive`` recovery popup that fires
        when bootstrap's daemon-warm-up retries exhaust."""
        import os
        path = self._debug_503_path()
        try:
            if os.path.exists(path):
                os.remove(path)
            else:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'w') as f:
                    f.write('')
        except OSError as ex:
            state_label = self.ids.get('debug_503_state')
            if state_label is not None:
                state_label.text = _tr(
                    'Error: {error}').format(error=ex)
            return
        self._refresh_debug_503_state()


# ── GitHub device flow ──────────────────────────────────────────────────────

class GitHubConnectScreen(Screen):
    """Step-by-step GitHub connect/manage screen.

    Pre-flight panel + a 3-step indicator (Authorize → Install App →
    Verify) gates progress; a single state-aware "primary" button
    presents only the next required action. ``on_pre_enter`` reads
    ``credentials_status`` and routes the user to the next step they
    haven't completed, so a partial setup (lost network, browser
    bail-out, app close) resumes from where it stopped — server state
    is the source of truth (``connected`` / ``app_installed`` /
    ``confirmed``). Nothing auto-fires; the user always taps Begin /
    Install GitHub App / Verify setup explicitly. The "no auto-fire"
    rule is the audit-doc #3 directive paired with the pre-flight
    explanation: the user reads the panel, then explicitly opts in.

    All show/hide goes through the Kivy hide/show pattern (height: 0,
    opacity: 0). Refer to ~/.claude-sil/CLAUDE.md for the cookbook —
    in particular the rule about not relying on ``minimum_height``
    when a BoxLayout starts at 0."""

    _user_code = ''
    # True between ``begin()`` and the worker's terminal callback.
    # While set, ``on_pre_enter`` skips re-rendering the message /
    # primary button so re-entering the screen mid-flow doesn't stomp
    # the visible code box and "Opening {uri}..." instructions.
    _device_flow_active = False
    # Set to the installation_id when the last Verify setup detected
    # a suspended install. Drives the "tap Install → Configure →
    # Unsuspend" path: the primary button at step 2 opens the
    # installation-specific configure page (settings/installations/
    # <id>) instead of the generic install URL, and the message
    # walks the user through the GitHub UI. Cleared on the next
    # Verify setup that detects a healthy install.
    _suspended_installation_id = None

    def on_pre_enter(self):
        # Defer to next frame so the KV rule has finished instantiating
        # nested children and ``self.ids`` is fully populated. Without
        # this, accessing ``self.ids.gh_user_code`` from a partial
        # paint pass would raise on Kivy 2.3, leaving the screen in
        # the KV-default "Begin"/visible state with no ``_action``
        # tagged on the primary button — i.e., taps would be silently
        # no-op'd by the dispatcher.
        Clock.schedule_once(lambda dt: self._refresh_state(), 0)

    def _refresh_state(self):
        status = self._safe_status()
        gh = (status or {}).get('github', {}) or {}
        step = self._current_step(gh)
        import sys
        print(f'[github-connect] _refresh_state: step={step} '
              f'device_flow_active={self._device_flow_active} '
              f'gh.connected={gh.get("connected")} '
              f'gh.app_installed={gh.get("app_installed")} '
              f'gh.confirmed={gh.get("confirmed")}',
              file=sys.stderr, flush=True)

        if not self._device_flow_active:
            self._user_code = ''
            user_code_label = self.ids.get('gh_user_code')
            if user_code_label is not None:
                user_code_label.text = ''
            self._hide_device_flow()
        # While re-auth is polling, force step indicator back to 1 —
        # the user explicitly chose to redo Authorize, even though the
        # old token still has app_installed/confirmed flags set.
        display_step = 1 if self._device_flow_active else step
        self._render_steps(display_step)
        self._render_primary(step, gh)
        self._render_manage(gh)
        self._render_message(step, gh)

    @staticmethod
    def _current_step(gh):
        """1=authorize, 2=install app, 3=verify, 4=done."""
        if not gh.get('connected'):
            return 1
        if not gh.get('app_installed'):
            return 2
        if not gh.get('confirmed'):
            return 3
        return 4

    def _render_steps(self, current):
        labels = (
            (1, _tr('1. Authorize this device')),
            (2, _tr('2. Install GitHub App')),
            (3, _tr('3. Verify setup')),
        )
        widget_ids = ('gh_step1', 'gh_step2', 'gh_step3')
        for n, text in labels:
            widget = self.ids.get(widget_ids[n - 1])
            if widget is None:
                continue
            widget.text = text
            if current > n:
                widget.color = theme.TEXT_DIM
                widget.bold = False
            elif current == n:
                widget.color = theme.ACCENT
                widget.bold = True
            else:
                widget.color = theme.TEXT
                widget.bold = False

    def _render_primary(self, step, gh):
        import sys
        btn = self.ids.get('gh_primary_btn')
        if btn is None:
            print('[github-connect] _render_primary: gh_primary_btn '
                  'NOT IN ids', file=sys.stderr, flush=True)
            return
        # Hide the primary button only while polling — the
        # device-flow box carries the active affordance there. Once
        # everything's verified (step 4) we keep "Verify setup"
        # available so the user can re-confirm without going through
        # device flow again. The test path is idempotent (hits
        # ``api.github.com/user``); a successful re-test stays at
        # step 4, a failure surfaces via the screen regressing to
        # step 2 / step 1 / "Token rejected" — all useful diagnostics
        # from a single tap.
        if self._device_flow_active:
            btn.height = 0
            btn.opacity = 0
            btn.disabled = True
            btn._action = None
            return
        if step == 1:
            btn.text = _tr('Begin')
            btn._action = 'begin'
        elif step == 2:
            btn.text = _tr('Install GitHub App')
            btn._action = 'install'
        else:
            # step 3 (initial verify) or step 4 (re-verify) both
            # land here — same label, same action.
            btn.text = _tr('Verify setup')
            btn._action = 'verify'
        btn.height = dp(52)
        btn.opacity = 1
        btn.disabled = False
        # Diagnostics. The state-change probe + Window touch tracer
        # together tell us whether the issue is event dispatch or
        # touch routing. If the Window sees touches at the button's
        # pos but ``state`` never flips, something between Window and
        # the button is eating the touch. Bound once per instance so
        # we don't pile up handlers across re-renders.
        if not getattr(btn, '_state_probe_bound', False):
            btn._state_probe_bound = True

            def _on_state(_b, value):
                print(f'[github-connect] gh_primary_btn state→{value} '
                      f'pos={btn.pos} size={btn.size} '
                      f'disabled={btn.disabled} opacity={btn.opacity}',
                      file=sys.stderr, flush=True)
            btn.bind(state=_on_state)

            def _on_touch_down(_b, touch):
                inside = btn.collide_point(*touch.pos)
                print(f'[github-connect] gh_primary_btn '
                      f'on_touch_down: touch.pos={touch.pos} '
                      f'btn.pos={btn.pos} btn.size={btn.size} '
                      f'inside={inside}',
                      file=sys.stderr, flush=True)
            btn.bind(on_touch_down=_on_touch_down)

            try:
                from kivy.core.window import Window

                def _window_touch(_w, touch):
                    inside = btn.collide_point(*touch.pos)
                    print(f'[github-connect] WINDOW touch_down: '
                          f'touch.pos={touch.pos} '
                          f'inside_primary_btn={inside}',
                          file=sys.stderr, flush=True)
                Window.bind(on_touch_down=_window_touch)
            except Exception as ex:
                print(f'[github-connect] window probe bind '
                      f'failed: {ex}', file=sys.stderr, flush=True)
        print(f'[github-connect] _render_primary: text={btn.text!r} '
              f'pos={btn.pos} size={btn.size} disabled={btn.disabled} '
              f'opacity={btn.opacity}',
              file=sys.stderr, flush=True)

    def _render_manage(self, gh):
        # Re-auth + Disconnect only when a token is on file — there's
        # nothing to re-authenticate against or disconnect from before
        # step 1 completes. ``_device_flow_active`` covers the re-auth
        # case where a token exists but we're mid-replacement; hide the
        # manage box then so the user can't disconnect mid-flow.
        if gh.get('connected') and not self._device_flow_active:
            self._show_manage_box()
        else:
            self._hide_manage()

    def _render_message(self, step, gh):
        if self._device_flow_active:
            return  # don't stomp the in-flight message
        msg = self.ids.get('gh_message')
        if msg is None:
            return
        username = gh.get('username', '') or '?'
        if step == 1:
            msg.text = _tr(
                "Tap Begin when you are ready. We'll open GitHub in "
                'your browser to authorize this device.')
        elif step == 2:
            # If the last test detected a suspended install on this
            # account, surface the unsuspend instructions instead of
            # the generic "now install" line.
            if self._suspended_installation_id:
                msg.text = self._suspended_message_text()
            else:
                msg.text = _tr(
                    'Authorized as {username}. Now install the '
                    'GitHub App so the daemon can push your project.'
                ).format(username=username)
        elif step == 3:
            msg.text = _tr(
                'GitHub App installed. Tap Verify setup to finish.')
        else:
            msg.text = _tr(
                'Setup complete. Connected as {username}. '
                'Tap Verify setup any time to re-test.'
            ).format(username=username)

    def _safe_status(self):
        try:
            return get_credentials_status()
        except Exception as ex:
            print(f'[github-connect] status fetch failed: {ex}')
            return {}

    # ── show / hide pattern ───────────────────────────────────────────

    # Hide-by-detach pattern. Setting only ``height=0`` on a
    # BoxLayout doesn't collapse its children's hit-test bounds —
    # ``BoxLayout._do_layout`` still positions them at their
    # explicit heights starting from ``self.top``, so a "hidden"
    # box with NavBtn children leaves the NavBtns at non-degenerate
    # y-ranges and they can swallow touches that *should* reach
    # ``gh_primary_btn``. ``disabled=True`` should make the swallow
    # safe via Widget.on_touch_down's "disabled+collide → return
    # True" short-circuit, but in this layout it didn't help —
    # touches in the Begin button's content y-range never reached
    # ``gh_primary_btn``. Detaching children entirely avoids the
    # layout-positioning interaction altogether: a parent with no
    # children cannot dispatch on_touch_down to anything.
    def _show_device_flow(self):
        box = self.ids.get('gh_device_flow_box')
        if box is None:
            return
        # SectionLabel(32) + dp(72) + 1×spacing(14) = 118.
        box.height = dp(118)
        box.opacity = 1
        box.disabled = False
        self._reattach_children('gh_device_flow_box')

    def _hide_device_flow(self):
        box = self.ids.get('gh_device_flow_box')
        if box is None:
            return
        box.height = 0
        box.opacity = 0
        box.disabled = True
        self._detach_children('gh_device_flow_box')

    def _hide_manage(self):
        box = self.ids.get('gh_manage_box')
        if box is None:
            return
        box.height = 0
        box.opacity = 0
        box.disabled = True
        self._detach_children('gh_manage_box')

    def _show_manage_box(self):
        box = self.ids.get('gh_manage_box')
        if box is None:
            return
        box.height = dp(48) + dp(48) + dp(10)
        box.opacity = 1
        box.disabled = False
        self._reattach_children('gh_manage_box')

    # ── detach / reattach helpers ────────────────────────────────────
    #
    # We snapshot the original children list once per box (lazily, on
    # first detach). On detach we ``remove_widget`` each child; on
    # reattach we ``add_widget`` them back in original order so the
    # KV-defined IDs / properties are preserved (Kivy holds strong
    # refs via the snapshot list, so the widgets aren't GC'd while
    # detached).

    _detached = None  # box_id -> list[Widget]

    def _detached_dict(self):
        if self._detached is None:
            self._detached = {}
        return self._detached

    def _detach_children(self, box_id):
        box = self.ids.get(box_id)
        if box is None:
            return
        store = self._detached_dict()
        if box_id in store:
            return  # already detached
        # Capture in display order (top → bottom) so reattach
        # restores the same stacking. Kivy's children list is
        # reverse-add; iterate over a copy because remove_widget
        # mutates it.
        kids = list(box.children)
        store[box_id] = list(reversed(kids))
        for c in kids:
            box.remove_widget(c)

    def _reattach_children(self, box_id):
        box = self.ids.get(box_id)
        if box is None:
            return
        store = self._detached_dict()
        kids = store.pop(box_id, None)
        if not kids:
            return
        for c in kids:
            box.add_widget(c)

    # ── primary dispatcher ───────────────────────────────────────────

    def primary_action(self):
        """Single tap target whose action depends on the current step.

        Dispatch off the button's ``_action`` attribute (stamped by
        ``_render_primary``) instead of re-fetching credentials_status
        — the freshly-rendered label is the one the user just tapped,
        so the stamped action is what they meant. Falls back to the
        button's ``text`` (and finally to ``begin()``) so a late /
        racy ``on_pre_enter`` that didn't tag ``_action`` doesn't
        leave the button silently no-opping."""
        import sys
        btn = self.ids.get('gh_primary_btn')
        action = getattr(btn, '_action', None) if btn else None
        label = btn.text if btn else ''
        print(f'[github-connect] primary_action: action={action!r} '
              f'label={label!r}', file=sys.stderr, flush=True)
        if action == 'verify':
            self.test()
        elif action == 'install':
            self.install_app()
        elif action == 'begin':
            self.begin()
        elif label == _tr('Verify setup'):
            self.test()
        elif label == _tr('Install GitHub App'):
            self.install_app()
        else:
            # Default — matches the KV-default "Begin" label and is
            # the right call when ``_render_primary`` hasn't run yet
            # (e.g., id-resolution race on first paint).
            self.begin()

    def open_signup(self):
        """Audit doc #5 — surface a path for users without a GitHub
        account. The button is always visible (free-account creation
        is a precondition for the whole flow); users who already have
        an account ignore it."""
        try:
            webbrowser.open('https://github.com/signup')
        except Exception:
            pass

    # ── device-flow path ─────────────────────────────────────────────

    def begin(self):
        """User pressed the primary button at step 1, or chose
        Re-authenticate from the manage box. Either way we kick the
        device-flow worker; the manage box is hidden by ``on_pre_enter``
        while ``_device_flow_active`` is True."""
        import sys
        print('[github-connect] begin: starting device flow',
              file=sys.stderr, flush=True)
        self._device_flow_active = True
        self._show_device_flow()
        self._hide_manage()
        # Hide primary button + reset step indicator to 1 active.
        self._render_primary(1, {})
        self._render_steps(1)
        msg = self.ids.get('gh_message')
        if msg is not None:
            msg.text = _tr('Starting device flow...')
        threading.Thread(target=self._worker, daemon=True).start()

    def copy_code(self):
        if not self._user_code:
            return
        try:
            from kivy.core.clipboard import Clipboard
            Clipboard.copy(self._user_code)
            self.ids.gh_message.text = (self.ids.gh_message.text + '\n'
                                        + _tr('(code copied)'))
        except Exception:
            pass

    def _worker(self):
        # Direct-import — UI process runs in the same package as the daemon
        import sys
        from azt_collabd.auth import (
            device_flow_start, device_flow_poll,
            get_github_username, check_app_installed,
        )
        print('[github-connect] worker: device_flow_start ...',
              file=sys.stderr, flush=True)
        try:
            resp = device_flow_start()
            user_code = resp['user_code']
            device_code = resp['device_code']
            print(f'[github-connect] worker: got user_code={user_code!r}, '
                  f'polling for token (interval={resp.get("interval", 5)}s, '
                  f'expires_in={resp.get("expires_in", 900)}s)',
                  file=sys.stderr, flush=True)
            # GitHub omits ``verification_uri_complete`` (RFC 8628
            # §3.2 marks it OPTIONAL; GitHub returns only the bare
            # ``verification_uri``). Constructing a prefilled URL
            # ourselves doesn't help: the ``/login/device`` page
            # silently ignores ``?user_code=...`` (verified against
            # docs.github.com, the cli/oauth Go reference impl, and
            # octokit/auth-oauth-device.js — none send a prefilled
            # form, and github.com makes no such handler available).
            # Plus a Jan-2024 account-confirmation step ("select
            # Continue on an account") sits in front of the code
            # form unconditionally, even for single-account users.
            # So we just open the bare URL; the user has to type
            # the code into GitHub's 8-field input. Best UX we can
            # offer in-app is the auto-clipboard copy below — but
            # GitHub's input doesn't accept clipboard paste either,
            # so it's mostly a fallback for scrollback / sharing.
            # If a future GitHub change starts returning
            # ``verification_uri_complete`` we'll use it.
            verify_uri = (resp.get('verification_uri_complete')
                          or resp.get('verification_uri')
                          or 'https://github.com/login/device')
            interval = resp.get('interval', 5)
            expires_in = resp.get('expires_in', 900)

            def _show(dt, _code=user_code, _uri=verify_uri):
                self._user_code = _code
                self.ids.gh_user_code.text = _code
                # Auto-copy the user_code so the user can paste it
                # straight into the GitHub device page without
                # needing to tap the Copy button. Best-effort: a
                # clipboard failure (Android headless test devices,
                # X11 missing on a CI box, etc.) is silent.
                copied_ok = False
                try:
                    from kivy.core.clipboard import Clipboard
                    Clipboard.copy(_code)
                    copied_ok = True
                except Exception:
                    pass
                msg = _tr(
                    'Opening {uri}\nIf the page asks for the code, '
                    'paste it from the box above.'
                ).format(uri=_uri)
                if copied_ok:
                    msg += '\n' + _tr('(code copied)')
                self.ids.gh_message.text = msg
            Clock.schedule_once(_show, 0)
            try:
                webbrowser.open(verify_uri)
            except Exception:
                pass

            token_data = device_flow_poll(device_code, interval, expires_in)
            print(f'[github-connect] worker: device_flow_poll '
                  f'returned, fetching username',
                  file=sys.stderr, flush=True)
            access_token = token_data['access_token']
            username = get_github_username(access_token) or 'unknown'
            print(f'[github-connect] worker: saving tokens for '
                  f'username={username!r}',
                  file=sys.stderr, flush=True)
            save_github_tokens(token_data, username)
            print('[github-connect] worker: tokens saved',
                  file=sys.stderr, flush=True)

            # Best-effort: read app-install state
            try:
                info = check_app_installed(access_token)
                installed = bool(info.get('installed'))
                print(f'[github-connect] worker: app_installed probe = '
                      f'{installed}', file=sys.stderr, flush=True)
                if installed:
                    mark_github_app_installed(True)
            except Exception as ex:
                print(f'[github-connect] worker: app_installed probe '
                      f'failed: {ex}', file=sys.stderr, flush=True)

            def _done(dt):
                # Token saved; set_github_tokens just reset
                # ``confirmed`` to False (and possibly ``app_installed``
                # to True if the probe above flipped it). Re-render the
                # screen so the step indicator advances and the primary
                # button label changes to whatever's next.
                print('[github-connect] worker: _done fired',
                      file=sys.stderr, flush=True)
                self._device_flow_active = False
                self.on_pre_enter()
            Clock.schedule_once(_done, 0)

        except AuthError as ex:
            print(f'[github-connect] worker: AuthError {ex.status!r}',
                  file=sys.stderr, flush=True)
            msg = translate_status(ex.status)

            def _err(dt, _m=msg):
                # Set the Failed message AFTER the deferred
                # _refresh_state runs, otherwise its step-1 default
                # message overwrites our error.
                self._device_flow_active = False
                self.on_pre_enter()  # schedules _refresh_state

                def _set_msg(_dt2):
                    msg_lbl = self.ids.get('gh_message')
                    if msg_lbl is not None:
                        msg_lbl.text = _tr(
                            'Failed: {error}').format(error=_m)
                Clock.schedule_once(_set_msg, 0)
            Clock.schedule_once(_err, 0)
        except Exception as ex:
            print(f'[github-connect] worker: Exception '
                  f'{type(ex).__name__}: {ex}',
                  file=sys.stderr, flush=True)

            def _err(dt, _e=str(ex)):
                self._device_flow_active = False
                self.on_pre_enter()

                def _set_msg(_dt2):
                    msg_lbl = self.ids.get('gh_message')
                    if msg_lbl is not None:
                        msg_lbl.text = _tr(
                            'Failed: {error}').format(error=_e)
                Clock.schedule_once(_set_msg, 0)
            Clock.schedule_once(_err, 0)

    # ── manage path ──────────────────────────────────────────────────

    def test(self):
        """Verify setup — the live test against
        ``api.github.com/user``. Daemon also refreshes
        ``app_installed`` while it has a valid token in hand, which
        lets a slightly-stale step-2-skipping flow self-correct."""
        self.ids.gh_primary_btn.disabled = True
        self.ids.gh_message.text = _tr('Testing...')
        threading.Thread(target=self._test_worker, daemon=True).start()

    def _test_worker(self):
        info = test_github_credentials()
        Clock.schedule_once(lambda dt: self._test_done(info), 0)

    def _test_done(self, info):
        # Re-render the screen against fresh status so a successful
        # test (which flips confirmed=True) advances to step 4.
        self.on_pre_enter()
        if not info.get('ok'):
            self.ids.gh_message.text = _tr(
                'Server unavailable: {error}').format(
                    error=info.get('error', '?'))
            return
        if info.get('valid'):
            # Token works against api.github.com/user. Two
            # sub-cases now matter:
            # - app_suspended=True: the App is installed but the
            #   user paused the install on GitHub. Push will 403.
            #   Stash the installation_id so ``install_app``
            #   opens the installation-specific configure page
            #   (and the message can give precise step-by-step
            #   instructions for the unsuspend path).
            # - otherwise: clear any stale suspended-id from a
            #   previous test, and let on_pre_enter's deferred
            #   ``_refresh_state`` write the right step line.
            if info.get('app_suspended'):
                self._suspended_installation_id = (
                    info.get('installation_id'))
                # ``on_pre_enter`` above scheduled
                # ``_refresh_state`` for the next frame, which
                # would overwrite ``gh_message`` with the step-2
                # default ("Now install the GitHub App..."). Run
                # our suspended message AFTER _refresh_state via a
                # second Clock.schedule_once so it survives the
                # render. ``_render_message`` also branches on
                # ``_suspended_installation_id`` so subsequent
                # re-renders (language change, screen re-entry)
                # surface the same suspended copy without a
                # re-test — but we still set it explicitly here
                # so the user sees the message immediately after
                # the verify they just tapped.
                def _set_suspended_msg(_dt):
                    msg = self.ids.get('gh_message')
                    if msg is not None:
                        msg.text = self._suspended_message_text()
                Clock.schedule_once(_set_suspended_msg, 0)
            else:
                self._suspended_installation_id = None
            return
        err = info.get('error', '') or 'unknown'
        if err == 'invalid_token':
            self.ids.gh_message.text = _tr(
                'Token rejected by GitHub. Tap Re-authenticate.')
            return
        if err.startswith('network_error'):
            self.ids.gh_message.text = _tr(
                'Network error — check your connection.')
            return
        self.ids.gh_message.text = _tr(
            'Test failed: {error}').format(error=err)

    def install_app(self):
        """Open the GitHub App install (or, when suspended, configure)
        page in the user's browser. After they grant access / unsuspend
        on GitHub they return here and tap the button — which we swap
        to "Verify setup" right after opening the browser, so the
        affordance the message promises actually exists.

        If the user returns without installing (cancelled, navigated
        away, etc.), tapping Verify setup runs the test, which
        reports ``app_installed=False`` and ``_test_done`` plus
        ``_render_primary`` regress the button back to
        "Install GitHub App" so the user can retry.

        For a known-suspended install, prefer the installation-specific
        page (``settings/installations/<id>``) over the generic install
        URL so the user lands directly on Configure with the Unsuspend
        button reachable from one scroll."""
        url = self._install_or_configure_url()
        if not url:
            self.ids.gh_message.text = _tr(
                'Could not determine the GitHub URL.')
            return
        try:
            webbrowser.open(url)
            if self._suspended_installation_id:
                self.ids.gh_message.text = _tr(
                    "Opening {uri}\nOn that page, scroll to the "
                    "bottom and tap 'Unsuspend'. Then come back "
                    "here and tap Verify setup."
                ).format(uri=url)
            else:
                self.ids.gh_message.text = _tr(
                    'Opening {uri}\nWhen you finish on GitHub, '
                    'return here and tap Verify setup.'
                ).format(uri=url)
            btn = self.ids.get('gh_primary_btn')
            if btn is not None:
                btn.text = _tr('Verify setup')
                btn._action = 'verify'
        except Exception as ex:
            self.ids.gh_message.text = _tr(
                'Could not open install page: {error}').format(error=ex)

    def _install_or_configure_url(self):
        """Pick the right GitHub URL for the user's situation:
        installation-specific (configure page) when we know about a
        suspended install on this account, generic install URL
        otherwise."""
        inst_id = self._suspended_installation_id
        if inst_id:
            return f'https://github.com/settings/installations/{inst_id}'
        try:
            return github_app_install_url() or ''
        except Exception:
            return ''

    def _suspended_message_text(self):
        """Step-by-step guidance for the Unsuspend path. The user
        complained that "Resume it at {url}" was unhelpful — they
        get a URL but no idea what to do on the page. Walk them
        through the actual GitHub UI."""
        return _tr(
            "GitHub App installation is suspended. Tap "
            "'Install GitHub App' below to open the install's "
            "configure page on GitHub, then scroll to the "
            "bottom and tap 'Unsuspend'.")

    def reauthenticate(self):
        """User explicitly asked to re-run the device flow (token
        expired / revoked / wrong account). ``begin()`` hides the
        manage box for the duration of the new device flow."""
        self.begin()

    def disconnect(self):
        """Wipe the GitHub credentials block. Same call shape as the
        settings screen's Disconnect, but lives here so the
        manage view is fully self-contained."""
        try:
            save_github_tokens({'access_token': '', 'refresh_token': ''},
                               username='')
            mark_github_app_installed(False)
        except Exception as ex:
            self.ids.gh_message.text = _tr(
                'Error: {error}').format(error=ex)
            return
        # After disconnect the screen state flips back to step 1 —
        # re-run on_pre_enter to render it.
        self.on_pre_enter()


# ── GitLab PAT form ─────────────────────────────────────────────────────────

class GitLabFormScreen(Screen):
    def on_pre_enter(self):
        try:
            status = get_credentials_status()
            gl = (status or {}).get('gitlab', {}) or {}
        except Exception:
            gl = {}
        user_inp = self.ids.get('gl_user')
        if user_inp is not None:
            user_inp.text = gl.get('username', '') or ''
        tok_inp = self.ids.get('gl_token')
        if tok_inp is not None:
            tok_inp.text = ''
        msg_lbl = self.ids.get('gl_msg')
        if msg_lbl is not None:
            msg_lbl.text = ''
        # Reveal the Disconnect block only once a token is on file.
        # Pre-disconnect, this is purely a connect form; surfacing
        # Disconnect there would be confusing (nothing to
        # disconnect from).
        if gl.get('connected'):
            self._show_manage()
        else:
            self._hide_manage()

    def _show_manage(self):
        box = self.ids.get('gl_manage_box')
        if box is None:
            return
        box.height = dp(48)  # one NavBtn
        box.opacity = 1
        box.disabled = False

    def _hide_manage(self):
        box = self.ids.get('gl_manage_box')
        if box is None:
            return
        box.height = 0
        box.opacity = 0
        box.disabled = True

    def disconnect(self):
        """Wipe the GitLab credentials block. On success, reload
        the screen state — the user lands on an empty connect form
        again with the Disconnect block hidden."""
        try:
            save_gitlab_credentials('', '')
        except Exception as ex:
            msg_lbl = self.ids.get('gl_msg')
            if msg_lbl is not None:
                msg_lbl.text = _tr(
                    'Error: {error}').format(error=ex)
            return
        self.on_pre_enter()

    def test(self):
        """Save the entered credentials and immediately validate them
        against gitlab.com. The single-button affordance avoids the
        "did I press Save before Test?" footgun — Test is the only
        operation, and on success the credentials are persisted."""
        u = self.ids.gl_user.text.strip()
        t = self.ids.gl_token.text.strip()
        if not u or not t:
            self.ids.gl_msg.text = _tr('Enter both username and token.')
            return
        self.ids.gl_test_btn.disabled = True
        self.ids.gl_msg.text = _tr('Testing...')
        threading.Thread(
            target=self._test_worker, args=(u, t), daemon=True).start()

    def _test_worker(self, username, token):
        # Server-side: a successful test persists credentials and sets
        # gitlab.confirmed=True in one shot. Failed tests neither save
        # nor mark confirmed, so the user can correct the PAT and retry
        # without first having to disconnect.
        info = test_gitlab_credentials(username, token)
        msg = self._format_test_result(info, username)
        Clock.schedule_once(lambda dt: self._test_done(msg), 0)

    def _format_test_result(self, info, username):
        if not info.get('ok'):
            return _tr('Server unavailable: {error}').format(
                error=info.get('error', '') or '?')
        if info.get('valid'):
            return (_tr('Connected as {username}. Credentials saved.')
                    .format(username=info.get('server_username', '')
                            or username))
        err = info.get('error', '') or 'unknown'
        if err == 'invalid_token':
            return _tr('Invalid token. Check your personal access token.')
        if err == 'username_mismatch':
            return _tr(
                'Token is valid, but belongs to {server_username}, '
                'not {username}.').format(
                    server_username=info.get('server_username', '?'),
                    username=username)
        if err.startswith('network_error'):
            return _tr('Network error — check your connection.')
        return _tr('Test failed: {error}').format(error=err)

    def _test_done(self, msg):
        self.ids.gl_msg.text = msg
        self.ids.gl_test_btn.disabled = False


# ── App ─────────────────────────────────────────────────────────────────────

class CollabUIApp(App):
    """Standalone collab settings UI. Credentials, host toggle, and
    GitHub/GitLab connect screens. Project picking lives in its own
    helper subprocess (`python -m azt_collabd projects`); see
    azt_collabd/ui/picker_app.py."""

    title = 'A-Z+T Collab'
    subtitle = StringProperty('Settings')
    icon = StringProperty(_AZT_ICON)
    # Initialized to ``client X · server ?`` and updated by
    # _probe_server_version() once the daemon answers /v1/health.
    # Showing ``azt_collabd.__version__`` here would be the
    # *compile-time* version of the UI subprocess, not the actual
    # daemon it's talking to — misleading whenever the user has
    # updated one half without the other.
    version_string = StringProperty(
        f'client {azt_collab_client.__version__}  ·  server ?'
    )

    def build(self):
        theme.set_theme('Ocean')
        self.font_name = register_charis()
        register_kv(self.font_name)
        self.sm = RootSM(transition=SlideTransition())
        return self.sm

    def on_start(self):
        """Bind Android's hardware back button so it pops sub-screens
        back to settings instead of closing the app. Without this,
        a back-press from GitHubConnectScreen / GitLabFormScreen
        falls through to the default ``App.stop`` path and the user
        loses the whole settings session — particularly painful
        from inside the connect flow where they're mid-setup."""
        from kivy.core.window import Window
        Window.bind(on_keyboard=self._on_back_button)
        # Probe the running daemon's version off the UI thread so the
        # bottom strip reflects what's actually answering, not the
        # version this UI subprocess was compiled at.
        import threading
        threading.Thread(target=self._probe_server_version,
                         daemon=True).start()

    def _probe_server_version(self):
        """Ask the daemon what version it is via /v1/health (the
        only endpoint that doesn't require auth) and render it into
        ``version_string``. Mirrors the picker-app probe so users
        see the same ``client X · server Y`` strip in both UIs."""
        try:
            compat = azt_collab_client.check_server_compat()
            err = ''
        except Exception as ex:
            compat = {}
            err = f'{type(ex).__name__}: {ex}'
        if isinstance(compat, dict):
            sv = compat.get('server_version') or ''
            ce = compat.get('error') or ''
            cd = compat.get('detail') or ''
        else:
            sv = ''
            ce = ''
            cd = ''
        if sv:
            label = f'server {sv}'
        elif err:
            label = f'server ? ({err[:60]})'
        elif ce:
            label = (f'server ? ({ce}: {cd[:60]})' if cd
                     else f'server ? ({ce})')
        else:
            label = 'server ?'
        Clock.schedule_once(
            lambda dt: setattr(
                self, 'version_string',
                f'client {azt_collab_client.__version__}  ·  {label}'),
            0)

    def _on_back_button(self, _window, key, *_args):
        """Android key 27 = hardware back. Returns True to consume.
        Settings-screen back closes the app (default Kivy behavior);
        any other screen pops to settings."""
        if key != 27:
            return False
        if not hasattr(self, 'sm'):
            return False
        if self.sm.current == 'settings':
            return False  # let the OS close the app
        self.sm.current = 'settings'
        return True

    def go(self, name):
        self.sm.current = name

    def share_apk(self):
        """Hand the running server APK to Android's share sheet so the
        user can send it to a teammate. No-op (with a translated
        error popup) on desktop — there's no APK to share."""
        share_running_apk(filename='aztcollab.apk',
                          on_error=self._show_error)

    def update_app(self):
        """Update this app. On Android: poll the configured GitHub repo
        for a newer server APK and, if found, download + trigger the
        system installer (repo / asset from ``azt_collabd.config``). On
        desktop the app is a git checkout, not an APK, so "update"
        fast-forwards that checkout from origin
        (``self_update.git_pull_self``, FF-only)."""
        try:
            from kivy.utils import platform
        except Exception:
            platform = ''
        if platform != 'android':
            self._desktop_git_update()
            return
        from azt_collabd.config import update_repo
        check_for_update(
            repo=update_repo(),
            current_version=azt_collabd.__version__,
            # asset_filename omitted — derived at runtime from the
            # running Activity's package name (= aztcollab.apk for
            # org.atoznback.aztcollab).
            on_status=self._set_update_msg,
            on_no_update=lambda: self._set_update_msg(_tr('Up to date.')),
            on_error=self._show_error,
        )

    def _desktop_git_update(self):
        """Desktop update = fast-forward the daemon's own git checkout.
        Runs in a worker so the network pull doesn't block the UI;
        result is translated here (``self_update`` returns codes so it
        owns no strings)."""
        import threading
        self._set_update_msg(_tr('Updating…'))

        def _work():
            try:
                from azt_collabd import self_update
                code, detail = self_update.git_pull_self()
            except Exception as ex:
                code, detail = 'FAILED', str(ex)
            msgs = {
                'UPDATED': _tr('Updated — restart to load the new '
                               'version.'),
                'UP_TO_DATE': _tr('Up to date.'),
                'NOT_A_CHECKOUT': _tr('This copy is not a git checkout, '
                                      'so it cannot self-update.'),
                'NO_GIT': _tr('git is not installed on this computer.'),
                'TIMEOUT': _tr('Update timed out.'),
            }
            if code in msgs:
                text = msgs[code]
            else:  # FAILED
                text = _tr('Update failed: {detail}').format(
                    detail=detail or '')
            Clock.schedule_once(
                lambda _dt: self._set_update_msg(text), 0)

        threading.Thread(target=_work, daemon=True).start()

    def _set_update_msg(self, text):
        try:
            sm = self.sm
            screen = sm.get_screen('settings') if sm.has_screen(
                'settings') else None
            msg = screen.ids.get('update_msg') if screen is not None \
                else None
        except Exception:
            msg = None
        if msg is not None:
            msg.text = text or ''

    def _show_error(self, msg):
        """Minimal error popup for share_apk. The settings screen has
        no inline status surface for app-level errors; a popup is loud
        enough to not be missed and dismissable in one tap."""
        from kivy.uix.popup import Popup
        from kivy.uix.label import Label
        Popup(
            title=_tr('Error'),
            content=Label(text=str(msg), color=theme.TEXT,
                          font_size=sp(14)),
            size_hint=(0.85, None), height=dp(220),
        ).open()


def main():
    azt_collabd.configure()
    CollabUIApp().run()


if __name__ == '__main__':
    main()
