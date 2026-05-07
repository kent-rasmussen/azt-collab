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

Caller invariants
-----------------
For the workflow to actually work end-to-end, the peer must honor
four contracts. Failures here are silent at runtime — the helper
fires its callbacks normally; the install / lookup just produces
no useful effect — so they're worth checking up front.

1. **``peer_asset_filename`` matches the published asset name
   exactly.** No fuzzy match, no glob. If the peer starts publishing
   versioned filenames (``azt_recorder-1.6.0.apk``), the lookup
   breaks; stick with a stable name (the
   ``releases/latest/download/<name>`` GitHub redirect is brittle
   to filename changes for the same reason).

2. **The release ``tag_name`` is parseable as a version.**
   ``_version_tuple`` is forgiving — ``v1.2.3``, ``1.2.3``,
   ``2026-05-06`` all work — but a tag like ``latest`` or ``final``
   parses to ``(0, 0, 0)`` and the helper will treat the peer as
   "older than 0.0.0", firing a no-update.

3. **``prerelease=true`` releases are skipped.** The helper walks
   ``/releases?per_page=20`` for the first non-prerelease,
   non-draft entry. Beta releases pushed with ``--prerelease`` on
   ``gh release create`` won't auto-install via this workflow; to
   force one out, drop the prerelease flag or have users update
   manually.

4. **The peer's ``buildozer.spec`` lists ``REQUEST_INSTALL_PACKAGES``**
   in ``android.permissions``. Without it the install intent
   silently no-ops and the user is stuck on the "Installing…"
   status. Android 8+ additionally requires the user to flip the
   per-source "Install unknown apps" toggle the first time;
   ``check_for_update`` detects this and routes the user to
   ``Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES`` — a one-tap
   detour, not a code-side fix.

Typical peer integration
------------------------
Two methods on the peer's ``App`` subclass — a wrapper that supplies
identity, and a status sink that routes progress strings into the
peer's existing logging surface. ``App.on_start`` schedules the
wrapper via ``Clock.schedule_once`` so the UI is up before any
prompt fires. Direct copy-paste from ``azt_recorder/main.py``
(the canonical peer integration; substitute your own constants):

    # at the top of your App class
    def on_start(self):
        ...
        Clock.schedule_once(lambda dt: self._run_bootstrap(), 0.5)

    def _run_bootstrap(self):
        from azt_collab_client.ui import bootstrap
        from appinfo import APP_NAME
        bootstrap(
            peer_repo='owner/your-peer-repo',     # GitHub release feed
            peer_version=__version__,             # main.py __version__
            peer_asset_filename='your_peer.apk',  # asset name in release
            peer_display_name=APP_NAME,           # shown in "Update X?" popup
            on_status=self._log_bootstrap_status,
            on_error=self._log_bootstrap_status,
            font_name=_FONT_NAME,
        )

    def _log_bootstrap_status(self, message):
        '''Surface bootstrap progress / errors. Logs to whatever
        in-app status surface the peer has, and to stderr so
        desktop devs and Android logcat see it too.'''
        print(f'[bootstrap] {message}', file=sys.stderr)
        try:
            # adjust to your screen layout — recorder's collab screen
            # exposes _set_log; viewer would route to its equivalent.
            sm = self.root.ids.sm
            collab = sm.get_screen('collab')
            collab._set_log(message)
        except Exception:
            pass

The deferred ``import`` inside ``_run_bootstrap`` keeps the
bootstrap.py module (and its Kivy / jnius dependencies)
out of the import graph until the peer actually fires it; tests
or non-Kivy desktop tools that import ``main`` for its
``__version__`` aren't pulled into the Kivy world.

The status sink is just a callable accepting one ``str``. Routing
to a status label, a toast, ``Logger.info``, or all three is the
peer's call — bootstrap doesn't care.
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
# Asset filename on the GitHub release. Matches the Android package
# convention (``aztcollab`` — no underscore, see naming table in
# azt-collab/CLAUDE.md). Earlier code used ``azt_collab.apk`` by
# mistake, which 404'd against every published release.
_SERVER_ASSET_DEFAULT = 'aztcollab.apk'
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
    so the workflow is plain functions instead of nested closures.

    ``connecting_popup`` is the only mutable slot — set when the
    daemon-warm-up retry phase opens its "Connecting…" popup, cleared
    when that popup is dismissed (success or transition to the
    unresponsive popup). Lives on the ctx so the retry function
    can find it without a module-level dict."""

    __slots__ = ('peer_repo', 'peer_version', 'peer_asset_filename',
                 'peer_display_name', 'server_repo',
                 'server_asset_filename', 'server_display_name',
                 'on_status', 'on_done', 'on_error', 'font_name',
                 'connecting_popup')

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


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


def _release_running():
    """Release the idempotence guard so bootstrap can be re-entered
    later (rare; mostly tests + relaunch-after-install). Does NOT
    fire the host's on_done — used when the workflow ends in a
    user-blocking popup that's the terminal state. Firing on_done
    in that case would let the host continue its normal startup,
    which then fails for the obvious reason ("daemon isn't there
    yet") and may take the app down — exactly the flash-then-die
    bug the user reported on first launch."""
    global _running
    _running = False


def _on_done_and_release(ctx):
    """Release the guard *and* fire the host's on_done. Use this
    when bootstrap reaches a healthy terminal state — server is
    fine, peer is current, host can proceed with normal startup.
    The blocking-popup branches (no server / server too old) use
    ``_release_running`` instead so the host doesn't try to
    continue."""
    _release_running()
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


# ── connecting popup (daemon-warm-up retry feedback) ─────────────────────

def _show_connecting_popup(ctx):
    """Modal "Connecting to AZT Collaboration service…" popup shown
    during the daemon-warm-up retry phase. Idempotent — if a popup
    is already up, no-op. Without this, the user sits looking at
    a peer UI with no daemon backing for the 10s retry window
    (RPCs return ServerUnavailable; screens render empty); the
    popup makes that wait visible and bounded."""
    if ctx.connecting_popup is not None:
        return
    from kivy.uix.popup import Popup
    from kivy.uix.label import Label
    from kivy.uix.boxlayout import BoxLayout
    from . import theme
    content = BoxLayout(orientation='vertical', padding=dp(16),
                        spacing=dp(8))
    label = Label(
        text=_tr('Connecting to AZT Collaboration service…'),
        font_size=sp(14), color=theme.TEXT,
        font_name=ctx.font_name,
        halign='center', valign='middle',
    )
    label.bind(size=lambda w, _v: setattr(w, 'text_size', w.size))
    content.add_widget(label)
    popup = Popup(
        title=_tr('Connecting…'),
        content=content,
        size_hint=(0.85, None), height=dp(160),
        auto_dismiss=False,
    )
    popup.open()
    ctx.connecting_popup = popup


def _dismiss_connecting_popup(ctx):
    """Tear down the connecting popup if up. Called from every
    terminal branch of the retry loop (success → continue; exhaust
    → unresponsive popup; error → fall-through)."""
    if ctx.connecting_popup is None:
        return
    try:
        ctx.connecting_popup.dismiss()
    except Exception:
        pass
    ctx.connecting_popup = None


# ── step 1: server compat ──────────────────────────────────────────────────

# How many times to retry the compat probe when the server APK is
# installed but its daemon hasn't responded yet. The 503
# ``daemon_not_ready`` response comes from
# ``AZTCollabProvider.java`` when the Python dispatch callback
# isn't registered yet — i.e., the Python interpreter is still
# loading. Warm-cache cold starts settle in 1–3 seconds; cold
# starts after a freshly-installed APK or ``pm clear`` (where dex
# caches need rebuilding) have been observed to run > 30s on
# real devices (the 0.28.24 bump from 10s to 30s wasn't enough;
# user reported "next boot fails, the following one succeeds").
# Budget 30 × 2s = 60s to cover the worst observed cold start.
# Warm launches exit the loop on the first attempt and never
# feel the longer budget. Users on truly slow hardware can also
# tap "Try again" on the unresponsive popup to extend further.
_DAEMON_WARMUP_RETRIES = 30
_DAEMON_WARMUP_INTERVAL_S = 2.0


def _check_server(ctx, _warmup_attempt=0):
    try:
        compat = check_server_compat()
    except Exception as ex:
        _on_ui(_dismiss_connecting_popup, ctx)
        _ui_status(ctx, _tr(
            'Could not check service: {error}').format(error=ex))
        # Best-effort: still try the self-update probe so the peer
        # gets a chance to update even if the daemon's version
        # endpoint is misbehaving.
        _check_self(ctx)
        return

    if compat.get('ok'):
        _on_ui(_dismiss_connecting_popup, ctx)
        _check_self(ctx)
        return

    err = compat.get('error', '') or ''
    if err == 'server_unreachable':
        # Disambiguate: package absent vs. daemon-warming-up. If the
        # server APK is installed but the daemon happens to be down
        # (lazy-spawn in progress; OOM-killed mid-call; whatever),
        # prompting to *install* it is wrong — it's already installed.
        # Probe PackageManager before deciding.
        if _server_package_installed():
            # Package is there but daemon isn't responding. Most
            # common reason at startup: Android is still spinning up
            # the server APK's Python interpreter. Retry the compat
            # probe with backoff; the daemon usually settles within
            # a few seconds.
            if _warmup_attempt < _DAEMON_WARMUP_RETRIES:
                # Show the connecting popup on first retry so the
                # user knows we're waiting (instead of staring at
                # an empty-state peer UI for ~10s).
                _on_ui(_show_connecting_popup, ctx)
                _ui_status(ctx, _tr(
                    'Connecting to AZT Collaboration service…'))

                def _retry(_dt):
                    threading.Thread(
                        target=_check_server,
                        args=(ctx,),
                        kwargs={'_warmup_attempt':
                                _warmup_attempt + 1},
                        daemon=True).start()
                Clock.schedule_once(_retry, _DAEMON_WARMUP_INTERVAL_S)
                return
            # All retries exhausted: daemon is genuinely unreachable
            # despite being installed. Dismiss the connecting popup
            # and show the canonical install popup with an
            # "unresponsive" body so the user gets explicit
            # Reinstall / Open page / Quit options instead of being
            # silently bounced out of the app.
            _on_ui(_dismiss_connecting_popup, ctx)
            _on_ui(_prompt_server_unresponsive, ctx)
            return
        _on_ui(_dismiss_connecting_popup, ctx)
        _on_ui(_prompt_server_install, ctx)
        return
    if err == 'server_too_old':
        _on_ui(_dismiss_connecting_popup, ctx)
        _on_ui(_prompt_server_update, ctx,
               compat.get('server_version', '') or '')
        return
    if err == 'client_too_old':
        _on_ui(_dismiss_connecting_popup, ctx)
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

def _post_install_continuation(ctx):
    """Re-enter the bootstrap state machine after a successful
    server APK install. Wait briefly for the freshly-installed
    daemon to warm up (Android lazy-spawns the ContentProvider
    host on first call), then re-probe compat. From here the
    healthy paths take over: compat ok → ``_check_self`` → either
    a self-update prompt or the on_done flow that lets the host
    continue normal startup.

    The 2-second delay is a safety margin for daemon warm-up; the
    actual lazy-spawn is faster but variable. If compat still
    reports unreachable here, the user will see the install popup
    again — which is the right escape hatch for a botched install
    rather than getting stuck."""
    def _resume(_dt):
        # Re-run the server-side branch only. Bootstrap's worker
        # thread isn't appropriate here (we're already on the UI
        # thread post-install); _check_server runs the same
        # check_server_compat call and dispatches.
        threading.Thread(target=_check_server, args=(ctx,),
                         daemon=True).start()
    Clock.schedule_once(_resume, 2.0)


def _prompt_server_unresponsive(ctx):
    """Server APK is installed but didn't respond after the
    daemon-warm-up retries. Same canonical popup as the missing-
    server case (so the user gets one consistent install/recover
    UI), with a body explaining the situation. Reinstall is the
    primary recovery action — replaces the running install with
    a fresh download, which fixes corrupt / signature-mismatched
    / wedged daemons.

    Same dismiss semantics as the missing-server case: tapping
    Quit closes the peer (it can't function without the daemon,
    and the daemon isn't responding). Open install page opens
    the release page in the browser.

    Does not fire on_done — the popup is terminal. If the user
    reinstalls and the new daemon responds, the post-install
    continuation chain re-runs ``_check_server`` and the host's
    on_done fires from the healthy path."""
    from .popups import install_server_apk_popup
    body = _tr(
        'The AZT Collaboration service ({name}) is installed but '
        'did not respond. It may still be starting up; wait a '
        'moment, then tap Install to reinstall it, or Quit to '
        'close this app and try again later.'
    ).format(name=ctx.server_display_name)
    _release_running()
    install_server_apk_popup(
        on_status=ctx.on_status,
        font_name=ctx.font_name,
        body_message=body,
        current_server_version='0.0.0',
        title=_tr('AZT Collaboration not responding'),
        on_install_complete=lambda: _post_install_continuation(ctx),
        # User can keep waiting if 60s wasn't enough on their
        # device. "Try again" reruns the compat probe (same code
        # path as the post-install continuation, including the 2s
        # daemon-warm-up pause).
        on_retry=lambda: _post_install_continuation(ctx),
    )


def _prompt_server_install(ctx):
    """No server APK: show the canonical install popup. The popup
    itself owns the Quit / Open install page / Install affordances
    and surfaces download progress in its body. Bootstrap delegates
    completely so all "no server" code paths in the suite converge
    on a single visual + behavioural surface.

    **Does not fire on_done.** The popup is the terminal state for
    this branch — the host can't continue normal startup without
    the daemon. Firing on_done here would let the host's "continue
    startup" callback run, which then fails (no daemon) and may
    cascade into the app shutting down (= the user-reported
    flash-then-die bug). The host stays parked at whatever screen
    was up when ``bootstrap()`` was scheduled (typically a splash
    or loading screen). The popup's own ``on_install_complete``
    chain re-enters bootstrap once the install lands, so the host
    resumes normal startup automatically when the daemon is
    reachable — without a manual relaunch.

    If Android kills the peer process during the install (memory
    pressure, system-installer Activity dominating), the popup +
    its on_install_complete chain are gone too. Re-launch then
    triggers a fresh bootstrap, which finds the daemon reachable
    and flows through ``_check_self`` → ``on_done`` from the
    healthy path."""
    from .popups import install_server_apk_popup
    body = _tr(
        'This app needs the AZT Collaboration service ({name}) to '
        'sync your data. Tap Install to download and install it. '
        'Android will ask you to confirm before the install starts.'
    ).format(name=ctx.server_display_name)
    _release_running()
    install_server_apk_popup(
        on_status=ctx.on_status,
        font_name=ctx.font_name,
        body_message=body,
        current_server_version='0.0.0',
        title=_tr('Install AZT Collaboration?'),
        on_install_complete=lambda: _post_install_continuation(ctx),
    )


def _prompt_server_update(ctx, current_version):
    """Server present but too old. Same popup shape as the missing-
    server prompt, different body / title / Install-button label /
    current_version (so check_for_update doesn't redownload an
    identical release).

    **Also does not fire on_done.** Same reasoning as
    ``_prompt_server_install`` — the daemon at the existing
    version doesn't satisfy the peer's MIN_SERVER_VERSION, so any
    RPC the host attempts after on_done fires would fail. The
    only exception is the already-declined branch: there we DO
    fire on_done, because the user has explicitly chosen to
    proceed with an old server (the peer's first RPC will get
    ``client_too_old`` from the daemon's compat handshake; that's
    the host's problem to handle gracefully).

    On install completion the popup's continuation chain re-runs
    the compat check, same as the missing-server case."""
    from .popups import install_server_apk_popup
    if current_version and _declined_version(
            ctx.server_repo) == current_version:
        # User explicitly declined this version earlier in a
        # previous session. Let the host continue and surface the
        # daemon's compat error its own way.
        _on_done_and_release(ctx)
        return
    body = _tr(
        'A newer version of the AZT Collaboration service ({name}) '
        'is required. Tap Update to download and install it.'
    ).format(name=ctx.server_display_name)
    _release_running()
    install_server_apk_popup(
        on_status=ctx.on_status,
        font_name=ctx.font_name,
        body_message=body,
        current_server_version=current_version or '0.0.0',
        install_label=_tr('Update'),
        title=_tr('Update AZT Collaboration?'),
        on_install_complete=lambda: _post_install_continuation(ctx),
    )


def _prompt_self_update(ctx, latest_version):
    """Peer self-update prompt. Same popup as the server install /
    update cases (so users see one consistent install/update UI
    across the suite), parameterized for the peer's own APK:

    - ``direct_url`` → composed from peer_repo + peer_asset_filename.
    - ``open_page_url`` → peer's release page so the user can read
      release notes before tapping Update.
    - ``install_target_package=''`` → explicitly skip polling.
      Self-install replaces the running peer process; polling our
      own package would block forever and the popup would hang at
      "Installing…" until the user manually closes it.
    - ``dismiss_label='Not now'`` + ``dismiss_action='dismiss'`` →
      the dismiss button just closes the popup (peer keeps running
      at the current version) instead of quitting the app. Self-
      update decline is "stick with what I have", not "I can't
      use this app".

    Records the decline on the dismiss path via the same
    ``_record_decline`` machinery as before — wrapped via the
    popup's normal close handler is moot since
    ``install_server_apk_popup`` doesn't expose an on-decline
    hook; instead we record decline up front on the
    ``Not now`` path by wrapping the dismiss into a custom
    callback. (See the ``on_dismiss`` Popup property below.)"""
    from .popups import install_server_apk_popup
    body = _tr(
        'A newer version of this app ({version}) is available. '
        'Tap Update to download and install it.'
    ).format(version=latest_version)
    direct_url = (
        f'https://github.com/{ctx.peer_repo}/'
        f'releases/latest/download/{ctx.peer_asset_filename}'
    )
    open_page_url = (
        f'https://github.com/{ctx.peer_repo}/releases/latest'
    )

    _release_running()
    popup = install_server_apk_popup(
        on_status=ctx.on_status,
        font_name=ctx.font_name,
        body_message=body,
        title=_tr('Update {name}?').format(name=ctx.peer_display_name),
        install_label=_tr('Update'),
        direct_url=direct_url,
        asset_filename=ctx.peer_asset_filename,
        open_page_url=open_page_url,
        dismiss_label=_tr('Not now'),
        dismiss_action='dismiss',
        # Empty string == explicitly no polling. Self-install
        # kills the running peer process during install; polling
        # our own package would block forever.
        install_target_package='',
        # No on_install_complete: when self-install finishes, the
        # peer's process is dying anyway. Next-launch bootstrap
        # detects the new version naturally.
    )

    # Record decline on Not-now tap. Kivy's Popup fires
    # ``on_dismiss`` on any dismiss path (Not now button, or
    # auto_dismiss=False back-out — though we set False so back-
    # out doesn't fire here). We record only when the popup
    # dismisses without an install completion having fired; if
    # the user actually tapped Update, the install flow takes
    # over and the dismiss-without-install never happens.
    _state = {'install_started': False}

    def _record_on_dismiss(_p):
        if not _state['install_started']:
            _record_decline(ctx.peer_repo, latest_version)
        _on_done_and_release(ctx)

    # Best-effort hook into Install tap — the popup doesn't
    # currently expose this, so we monkey-patch by binding a
    # one-shot handler on its install_btn. Looking up the button
    # reaches into popup internals (children traversal), but it's
    # a one-line shortcut vs. plumbing an "on_install_started"
    # callback all the way through ``install_server_apk_popup``
    # for this single use case.
    try:
        from kivy.uix.button import Button
        def _walk(w):
            for child in w.children:
                if isinstance(child, Button) and child.text == _tr('Update'):
                    return child
                found = _walk(child)
                if found is not None:
                    return found
            return None
        install_btn = _walk(popup.content)
        if install_btn is not None:
            install_btn.fbind(
                'on_release',
                lambda *_: _state.__setitem__('install_started', True))
    except Exception:
        pass
    popup.bind(on_dismiss=_record_on_dismiss)


# ── popup primitives ───────────────────────────────────────────────────────

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
