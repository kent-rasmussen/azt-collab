"""Startup install/update workflow for suite peers.

The user-facing rule for the suite: *the user installs one APK* — the
peer they actually opened (recorder, viewer, …). Everything else,
including the standalone server APK, is provisioned by the peer
itself on first run.

Each peer calls ``bootstrap(...)`` once, early in startup
(``App.on_start`` is the natural seam). The helper:

1. Asks the daemon for compat (``check_server_compat``):
   - ``ok=True``                    → continue to step 2.
   - ``error='server_unreachable'`` → likely the server APK isn't
     installed yet (or no network). Pop a Yes/No: "Install the
     AZT Collaboration service?" — on Yes, download the latest
     ``azt_collab.apk`` from the server's release feed and dispatch
     Android's system installer.
   - ``error='server_too_old'``     → Yes/No: "Update the AZT
     Collaboration service?" — same install path on Yes.
   - ``error='client_too_old'``     → the **peer** is the problem;
     jump to step 2's self-update path so the user only sees one
     prompt.

2. Check the peer's own latest published release on GitHub
   (``check_for_update`` against ``peer_repo``). If newer, pop a
   Yes/No: "Update <peer name>?" — on Yes, download+install. If
   not, call ``on_done`` so the host proceeds with its normal
   startup.

Android-only effects. Desktop hosts call ``on_done`` immediately
(no APKs to install). All popups marshal back to the Kivy UI
thread; the version probe runs on a worker thread so first paint
is unaffected.

**Peer build requirement.** The peer's ``buildozer.spec`` must list
``REQUEST_INSTALL_PACKAGES`` in ``android.permissions`` so the
helper can dispatch the install intent. Without it the install
silently no-ops and the user is stuck on the prompt. Android 8+
also requires the user to flip the per-source "Install unknown
apps" toggle the first time; ``check_for_update`` detects this and
routes the user to ``Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES``.
"""

import json
import os
import sys
import threading

from kivy.clock import Clock
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup

from .. import check_server_compat
from ..paths import azt_home
from ..translate import tr as _tr
from .update import check_for_update


_SERVER_REPO_DEFAULT = 'kent-rasmussen/azt-collab'
_SERVER_ASSET_DEFAULT = 'azt_collab.apk'
_SERVER_PACKAGE_NAME = 'org.atoznback.aztcollab'

# Module-level idempotence guard. A peer that calls bootstrap() twice
# in the same process — for instance, if its on_start fires twice
# during a Kivy hot reload, or two startup hooks both wire it — would
# otherwise launch two parallel workflows and prompt the user twice.
# The guard resets only on process exit, which is fine: legitimate
# re-runs (after install completes and peer relaunches) get a fresh
# process anyway.
_running = False


def bootstrap(*, peer_repo, peer_version, peer_asset_filename,
              peer_display_name='',
              server_repo=_SERVER_REPO_DEFAULT,
              server_asset_filename=_SERVER_ASSET_DEFAULT,
              server_display_name='',
              on_status=None, on_done=None, on_error=None,
              font_name='Roboto'):
    """Run the startup install/update workflow once.

    Parameters
    ----------
    peer_repo : str
        ``'owner/repo'`` for this peer (e.g.
        ``'kent-rasmussen/azt-recorder'``). Drives the self-update
        path.
    peer_version : str
        Caller's running ``__version__`` — compared as a semver
        tuple against the peer's latest release tag.
    peer_asset_filename : str
        Exact name of the peer's APK in its GitHub release
        (e.g. ``'azt_recorder.apk'``).
    peer_display_name : str
        Human-facing app name used in the self-update prompt
        ("Update <name>?"). Defaults to a safe fallback derived
        from the asset filename if empty.
    server_repo / server_asset_filename / server_display_name :
        Same shape as the peer arguments but for the standalone
        server APK. Defaults match the canonical
        ``kent-rasmussen/azt-collab`` feed; a fork can override.
    on_status : callable(str) | None
        State / progress strings ("Checking installation…",
        "Downloading 45%…", …). Hosts wire this to a status label or
        toast.
    on_done : callable() | None
        Fires when the workflow completes (every up-to-date branch,
        and after a declined or completed install).
    on_error : callable(str) | None
        Failure surface for non-recoverable problems (network down
        AND no cache, etc.). Most failures are routed through
        ``on_status`` instead so the host can keep going.
    font_name : str
        Font for the inline Yes/No popups. Pass the host's
        ``CharisSIL`` registration so prompts match the rest of the
        UI.
    """
    global _running
    try:
        from kivy.utils import platform
    except Exception:
        platform = ''
    if platform != 'android':
        # Desktop / non-Android hosts have nothing to install.
        # No guard manipulation needed — we never set _running on
        # this path.
        _safe(on_done)
        return

    if _running:
        # Second call within the same process — e.g. an on_start
        # hook fired twice during a Kivy reload or an explicit
        # caller invoked us before the first run completed.
        # Re-running would prompt the user twice; suppress instead.
        return
    _running = True

    if not peer_display_name:
        peer_display_name = _strip_apk(peer_asset_filename)
    if not server_display_name:
        server_display_name = _tr('AZT Collaboration')

    ctx = _Ctx(
        peer_repo=peer_repo,
        peer_version=peer_version,
        peer_asset_filename=peer_asset_filename,
        peer_display_name=peer_display_name,
        server_repo=server_repo,
        server_asset_filename=server_asset_filename,
        server_display_name=server_display_name,
        on_status=on_status,
        on_done=on_done,
        on_error=on_error,
        font_name=font_name,
    )
    _ui_status(ctx, _tr('Checking installation…'))
    threading.Thread(target=_check_server, args=(ctx,),
                     daemon=True).start()


# ── helpers ────────────────────────────────────────────────────────────────

class _Ctx:
    """Bag of immutable params + callbacks. Passed through every step
    so the workflow is plain functions instead of nested closures."""

    __slots__ = ('peer_repo', 'peer_version', 'peer_asset_filename',
                 'peer_display_name', 'server_repo',
                 'server_asset_filename', 'server_display_name',
                 'on_status', 'on_done', 'on_error', 'font_name')

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw[k])


def _strip_apk(name):
    if name.lower().endswith('.apk'):
        name = name[:-4]
    return name.replace('_', ' ').replace('-', ' ').strip() or name


def _safe(cb, *args):
    if cb is None:
        return
    try:
        cb(*args)
    except Exception as ex:
        print(f'[bootstrap] callback raised: {ex}',
              file=sys.stderr, flush=True)


def _on_done_and_release(ctx):
    """Wrap the host's ``on_done`` so the module-level idempotence
    guard releases when the workflow terminates. Without this, a
    bootstrap that completes (or is declined) would leave
    ``_running=True`` for the rest of the process and a host that
    legitimately re-fires bootstrap (rare; mostly for tests) couldn't.
    """
    global _running
    _running = False
    _safe(ctx.on_done)


# ── decline memory ────────────────────────────────────────────────────────

# Persisted at $AZT_HOME/config.json :: bootstrap.declined.<repo>=<version>
# so a user who taps "Not now" for a given peer or server release isn't
# re-prompted on every relaunch. Cleared automatically by storing a
# *version-pinned* value: when the upstream release moves to a newer
# version, the stored version is older and the prompt fires again.

_CONFIG_NS = 'bootstrap'
_DECLINED_KEY = 'declined'


def _config_path():
    return os.path.join(azt_home(), 'config.json')


def _load_config():
    try:
        with open(_config_path()) as f:
            return json.load(f) or {}
    except (FileNotFoundError, ValueError):
        return {}


def _save_config(cfg):
    p = _config_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
    os.replace(tmp, p)


def _declined_version(repo):
    cfg = _load_config()
    block = (cfg.get(_CONFIG_NS) or {}).get(_DECLINED_KEY) or {}
    return block.get(repo, '') or ''


def _record_decline(repo, version):
    """Persist that the user declined ``version`` for ``repo``.
    A later release with a different version string will re-prompt
    automatically — we compare exact strings, not semver, so a
    stuck prompt clears the moment the upstream tag changes."""
    cfg = _load_config()
    ns = cfg.setdefault(_CONFIG_NS, {})
    block = ns.setdefault(_DECLINED_KEY, {})
    block[repo] = version
    try:
        _save_config(cfg)
    except OSError as ex:
        print(f'[bootstrap] could not persist decline: {ex}',
              file=sys.stderr, flush=True)


# ── package-presence probe (Android) ──────────────────────────────────────

def _server_package_installed():
    """Return True iff the server APK is installed on this device.
    Used to disambiguate ``server_unreachable`` (which can mean either
    "no network" OR "server APK absent") so we don't prompt to install
    something that's already there but offline.

    Android-only; returns False on every other platform (callers gate
    on platform == 'android' first, so this is a defensive default)."""
    try:
        from jnius import autoclass
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        activity = PythonActivity.mActivity
        pm = activity.getPackageManager()
        # getPackageInfo throws NameNotFoundException when the package
        # isn't installed; with flags=0 we get the bare metadata which
        # is the cheapest probe.
        pm.getPackageInfo(_SERVER_PACKAGE_NAME, 0)
        return True
    except Exception:
        return False


def _on_ui(fn, *args):
    """Marshal to UI thread (no-op fallback if Clock isn't ready)."""
    try:
        Clock.schedule_once(lambda dt: fn(*args), 0)
    except Exception:
        try:
            fn(*args)
        except Exception:
            pass


def _ui_status(ctx, msg):
    _on_ui(_safe, ctx.on_status, msg)


# ── step 1: server compat ──────────────────────────────────────────────────

def _check_server(ctx):
    try:
        compat = check_server_compat()
    except Exception as ex:
        _ui_status(ctx, _tr(
            'Could not check service: {error}').format(error=ex))
        # Best-effort: still try the self-update probe so the peer
        # gets a chance to update even if the daemon's version
        # endpoint is misbehaving.
        _check_self(ctx)
        return

    if compat.get('ok'):
        _check_self(ctx)
        return

    err = compat.get('error', '') or ''
    if err == 'server_unreachable':
        # Disambiguate: package absent vs. network down. If the
        # server APK is installed but the daemon happens to be down
        # (no network reaching GitHub for the install probe; OOM-
        # killed mid-call; whatever), prompting to *install* it is
        # wrong — it's already installed. Probe PackageManager
        # before deciding.
        if _server_package_installed():
            # Package is there but unreachable. Skip the prompt;
            # the host's existing ServerUnavailable handling will
            # surface this when it next tries an RPC.
            _ui_status(ctx, _tr(
                'AZT Collaboration installed but unreachable. '
                'Continuing offline.'))
            _check_self(ctx)
            return
        _on_ui(_prompt_server_install, ctx)
        return
    if err == 'server_too_old':
        _on_ui(_prompt_server_update, ctx,
               compat.get('server_version', '') or '')
        return
    if err == 'client_too_old':
        # The peer is the one out of date — jump straight to
        # self-update and skip the server prompt.
        _check_self(ctx, force_prompt=True)
        return

    # Unknown error — log and try self-update anyway.
    _ui_status(ctx, _tr(
        'Service check returned: {error}').format(error=err))
    _check_self(ctx)


# ── step 2: self-update ────────────────────────────────────────────────────

def _check_self(ctx, *, force_prompt=False):
    """Probe the peer's own release feed. If a newer version exists,
    prompt the user; otherwise fire ``on_done``.

    ``force_prompt=True`` skips the no-update branch and always
    prompts even when versions match — used on the
    ``client_too_old`` path so the user sees actionable text rather
    than a silent ``on_done``."""

    def _on_status(msg):
        _safe(ctx.on_status, msg)

    def _on_no_update():
        if force_prompt:
            _on_ui(_show_info, ctx, _tr(
                'This app is too old for the AZT Collaboration '
                'service, but no newer version is published yet. '
                'Please check back later.'))
            _on_done_and_release(ctx)
            return
        _on_done_and_release(ctx)

    def _on_error(msg):
        _safe(ctx.on_error, msg)

    # check_for_update only triggers the install path when a newer
    # release is found. We wrap that with a Yes/No prompt so the user
    # gets to decide. To make that happen, we use a low-level
    # version probe path: pass the helper as-is; it reports progress
    # via on_status. The Yes/No is built into a wrapper.
    _peer_update_with_confirm(
        ctx,
        on_status=_on_status,
        on_no_update=_on_no_update,
        on_error=_on_error,
    )


def _peer_update_with_confirm(ctx, *, on_status, on_no_update, on_error):
    """Two-stage self-update: probe latest release on a worker
    thread, prompt the user on the UI thread, then on Yes invoke
    ``check_for_update`` for the download+install. Splits the probe
    from the install so the user gets to confirm with the new
    version number in the message.

    Reuses ``update._fetch_latest`` so the prerelease-skipping policy
    stays in one place (rather than duplicating the listing walk
    here)."""
    from .update import _fetch_latest
    from .. import _version_tuple

    def _probe():
        try:
            release = _fetch_latest(ctx.peer_repo)
        except Exception as ex:
            _on_ui(on_error, _tr(
                'Update check failed: {error}').format(error=ex))
            _on_ui(on_no_update)
            return
        latest = (release.get('tag_name') or '').lstrip('vV')
        if not latest or _version_tuple(latest) <= _version_tuple(
                ctx.peer_version):
            _on_ui(on_no_update)
            return
        # Decline memory: if the user already said "Not now" for
        # this exact version, skip the prompt. A new version moves
        # us off the recorded value automatically.
        if _declined_version(ctx.peer_repo) == latest:
            _on_ui(on_no_update)
            return
        _on_ui(_prompt_self_update, ctx, latest)

    threading.Thread(target=_probe, daemon=True).start()


# ── prompts ────────────────────────────────────────────────────────────────

def _prompt_server_install(ctx):
    title = _tr('Install AZT Collaboration?')
    body = _tr(
        'This app needs the AZT Collaboration service ({name}) to '
        'sync your data. Tap Install to download and install it.'
    ).format(name=ctx.server_display_name)

    def _decline():
        # Server-install decline doesn't get version-pinned (we
        # haven't probed the version yet, and a missing-package case
        # is unconditional). Just release and continue.
        _on_done_and_release(ctx)

    _yes_no(ctx, title, body,
            yes_label=_tr('Install'),
            on_yes=lambda: _do_server_install(ctx),
            on_no=_decline)


def _prompt_server_update(ctx, current_version):
    title = _tr('Update AZT Collaboration?')
    body = _tr(
        'A newer version of the AZT Collaboration service ({name}) '
        'is required. Tap Update to download and install it.'
    ).format(name=ctx.server_display_name)

    def _decline():
        # Pin the decline to the current server version so the
        # prompt stays suppressed until either the user retries
        # explicitly OR the upstream server release moves to a
        # different version (which would change the compat shape
        # the daemon reports next time).
        if current_version:
            _record_decline(ctx.server_repo, current_version)
        _on_done_and_release(ctx)

    _yes_no(ctx, title, body,
            yes_label=_tr('Update'),
            on_yes=lambda: _do_server_install(ctx, current_version),
            on_no=_decline)


def _prompt_self_update(ctx, latest_version):
    title = _tr('Update {name}?').format(name=ctx.peer_display_name)
    body = _tr(
        'A newer version of this app ({version}) is available. '
        'Tap Update to download and install it.'
    ).format(version=latest_version)

    def _decline():
        _record_decline(ctx.peer_repo, latest_version)
        _on_done_and_release(ctx)

    _yes_no(ctx, title, body,
            yes_label=_tr('Update'),
            on_yes=lambda: _do_self_install(ctx),
            on_no=_decline)


def _do_server_install(ctx, current_version='0.0.0'):
    """Drive the server APK install through ``check_for_update``.
    Pass ``current_version='0.0.0'`` when the server isn't installed
    so the version comparison always picks the latest as 'newer';
    when updating, pass the running server's version so a no-op
    release feed is reported as 'up to date' instead of double-
    installing."""
    _ui_status(ctx, _tr('Installing AZT Collaboration…'))

    def _on_status(msg):
        _safe(ctx.on_status, msg)

    def _on_no_update():
        # Latest release matched current; tell the user nothing
        # changed and continue to step 2 anyway.
        _ui_status(ctx, _tr(
            'AZT Collaboration is up to date.'))
        _check_self(ctx)

    def _on_error(msg):
        _safe(ctx.on_error, msg)
        # Don't block startup on a server-install error; the host
        # can still run with reduced functionality.
        _on_done_and_release(ctx)

    check_for_update(
        repo=ctx.server_repo,
        current_version=current_version,
        asset_filename=ctx.server_asset_filename,
        on_status=_on_status,
        on_no_update=_on_no_update,
        on_error=_on_error,
    )


def _do_self_install(ctx):
    _ui_status(ctx, _tr('Updating {name}…').format(
        name=ctx.peer_display_name))

    def _on_status(msg):
        _safe(ctx.on_status, msg)

    def _on_no_update():
        # Race: latest changed between probe and download. Treat as
        # done.
        _on_done_and_release(ctx)

    def _on_error(msg):
        _safe(ctx.on_error, msg)
        _on_done_and_release(ctx)

    check_for_update(
        repo=ctx.peer_repo,
        current_version=ctx.peer_version,
        asset_filename=ctx.peer_asset_filename,
        on_status=_on_status,
        on_no_update=_on_no_update,
        on_error=_on_error,
    )


# ── popup primitives ───────────────────────────────────────────────────────

def _yes_no(ctx, title, body, *, yes_label, on_yes, on_no):
    """Two-button modal. Same shape across server-install /
    server-update / self-update prompts."""
    from . import theme

    content = BoxLayout(orientation='vertical', spacing=dp(10),
                        padding=dp(12))
    msg = Label(text=body, halign='left', valign='top',
                color=theme.TEXT, font_size=sp(14),
                font_name=ctx.font_name)
    msg.bind(width=lambda w, val: setattr(w, 'text_size', (val, None)))
    content.add_widget(msg)

    row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(12))
    no_btn = Button(text=_tr('Not now'), font_size=sp(14),
                    font_name=ctx.font_name)
    yes_btn = Button(text=yes_label, font_size=sp(14),
                     font_name=ctx.font_name,
                     background_color=theme.ACCENT)
    row.add_widget(no_btn)
    row.add_widget(yes_btn)
    content.add_widget(row)

    popup = Popup(title=title, content=content,
                  size_hint=(0.9, None), height=dp(260),
                  auto_dismiss=False)
    no_btn.bind(on_release=lambda *_: (popup.dismiss(), on_no()))
    yes_btn.bind(on_release=lambda *_: (popup.dismiss(), on_yes()))
    popup.open()
    return popup


def _show_info(ctx, body):
    """Single-button info popup for the rare client_too_old +
    no-newer-release branch."""
    from . import theme

    content = BoxLayout(orientation='vertical', spacing=dp(10),
                        padding=dp(12))
    msg = Label(text=body, halign='left', valign='top',
                color=theme.TEXT, font_size=sp(14),
                font_name=ctx.font_name)
    msg.bind(width=lambda w, val: setattr(w, 'text_size', (val, None)))
    content.add_widget(msg)
    btn = Button(text=_tr('OK'), size_hint_y=None, height=dp(48),
                 font_size=sp(14), font_name=ctx.font_name,
                 background_color=theme.ACCENT)
    content.add_widget(btn)
    popup = Popup(title=_tr('Update needed'), content=content,
                  size_hint=(0.9, None), height=dp(240),
                  auto_dismiss=False)
    btn.bind(on_release=lambda *_: popup.dismiss())
    popup.open()
    return popup
