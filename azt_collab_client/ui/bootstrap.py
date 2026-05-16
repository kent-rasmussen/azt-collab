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
     ``aztcollab.apk`` from the server's release feed and dispatch
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
import time as _time

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

# Idempotence guard for prewarm(). Issuing two compat probes in
# parallel is harmless but pointless — the second would race the
# first against the same daemon-lazy-spawn.
_prewarm_started = False

# Process-start monotonic clock. Anchored at module-load time so
# every ``_boot_trace`` line carries a consistent baseline. Both
# peers and the daemon (server_apk/service.py) have their own
# anchor; cross-process alignment is via logcat's wall-clock
# timestamps, which the parser in
# ``tests/integration/parse_boot_traces.py`` joins on.
_proc_start_monotonic = _time.monotonic()


def _installed_server_version():
    """Return the ``versionName`` of the server APK installed on disk,
    or '' if it can't be read (not Android, no jnius, server APK not
    installed, JNI failure).

    Used by ``_prompt_server_update`` to detect the
    "user just sideloaded a newer server APK but the old process is
    still serving the provider" transition. When the on-disk version
    is greater than what /v1/health reports, the install on disk is
    new but Android's package replace didn't kill the running
    daemon; ``_prompt_server_reboot_to_apply`` then substitutes the
    reboot-to-apply body for the usual download-and-install one.

    This is a transition helper for the pre-0.41.30 daemon → 0.41.30+
    upgrade where the receiver didn't yet auto-reap; once every
    field daemon is at 0.41.30 or newer the comparison should never
    fire (every install reaps the old daemon from the receiver,
    next bind = new code).
    """
    try:
        from jnius import autoclass
    except Exception:
        return ''
    try:
        PA = autoclass('org.kivy.android.PythonActivity')
        activity = PA.mActivity
        if activity is None:
            return ''
        pm = activity.getPackageManager()
        info = pm.getPackageInfo(_SERVER_PACKAGE_NAME, 0)
        return str(info.versionName or '')
    except Exception as ex:
        print(f'[bootstrap] _installed_server_version failed '
              f'({type(ex).__name__}: {ex})',
              file=sys.stderr, flush=True)
        return ''


def _boot_trace(phase, **fields):
    """Emit a boot-timing trace line. Format:

        [boot-trace-peer] phase=<phase> t=<elapsed-seconds> [k=v ...]

    Used by ``tests/integration/parse_boot_traces.py`` to compute
    timing tables for cold-start measurements (Q2 doze behaviour,
    Q3 prewarm value). Cheap (single ``time.monotonic`` + print);
    safe to leave on in shipping builds — the volume is tiny
    (≤ 10 lines per peer launch) and the stderr → logcat path is
    already established."""
    elapsed = _time.monotonic() - _proc_start_monotonic
    extras = ''
    if fields:
        extras = ' ' + ' '.join(f'{k}={v}' for k, v in fields.items())
    print(f'[boot-trace-peer] phase={phase} t={elapsed:.3f}{extras}',
          file=sys.stderr, flush=True)


def bootstrap(*, peer_repo, peer_version,
              peer_asset_filename=None,
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
    peer_asset_filename : str | None
        Exact name of the peer's APK in its GitHub release. When
        ``None`` (default), derived from the running Android
        package's last segment via
        ``azt_collab_client.ui.update.default_asset_filename`` —
        i.e. ``'aztrecorder.apk'`` for ``org.atoznback.aztrecorder``.
        Pass explicitly to override for a fork that publishes under
        a different naming scheme.
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
    _boot_trace('bootstrap_called')
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

    # Derive on the calling (UI) thread — the Activity reference
    # behind ``default_asset_filename`` is reliably reachable here,
    # and a blank result means we're in a configuration where the
    # auto-update path can't function anyway. Hard-fail rather than
    # silently 404 on a missing/wrong filename later.
    if peer_asset_filename is None:
        from .update import default_asset_filename
        peer_asset_filename = default_asset_filename()
    if not peer_asset_filename:
        msg = _tr(
            'Bootstrap failed: could not derive asset filename '
            '(running Activity unreachable). Pass '
            'peer_asset_filename= explicitly.')
        _safe(on_status, msg)
        _safe(on_error, msg)
        _running = False
        return

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
                 'connecting_popup',
                 # Warmup-loop telemetry. ``warmup_started_at`` is
                 # the monotonic clock when the first 503 fired so
                 # the connecting popup can show elapsed seconds.
                 # ``last_error_kind`` is the
                 # ``ServerUnavailable.kind`` from the most recent
                 # failed compat probe; surfaced verbatim in the
                 # connecting + unresponsive popups so a maintainer-
                 # email loop carries actionable detail without adb.
                 'warmup_started_at', 'last_error_kind',
                 'last_error_detail',
                 # Count of consecutive ``null_bundle`` failures
                 # observed in the current warmup cycle. Used to
                 # fail fast on signature-grant problems rather
                 # than waiting the full 60s budget. Reset whenever
                 # a 503 or any other kind shows up — those are
                 # progress signals.
                 'null_bundle_streak')

    def __init__(self, **kw):
        # Default to None for slots the caller doesn't pass.
        # Counters get a numeric default so the warmup loop can
        # increment without an isinstance check on first use.
        _numeric = {'null_bundle_streak'}
        for k in self.__slots__:
            if k in _numeric:
                setattr(self, k, kw.get(k, 0) or 0)
            else:
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
    _boot_trace('bootstrap_done')
    _release_running()
    _safe(ctx.on_done)


def prewarm():
    """Optional pre-warm hook for peers that want to overlap daemon
    lazy-spawn with their own Kivy initialisation. Caller invokes
    this from ``App.build()`` (or anywhere before ``bootstrap()``
    fires); it kicks off a single ``check_server_compat`` probe on
    a background thread and returns immediately. The probe causes
    Android to lazy-spawn the server APK's ``:provider`` process
    in parallel with whatever the peer is doing on the main thread.

    Idempotent within a process. No-op on non-Android (desktop
    transports auto-spawn synchronously; nothing to overlap).
    Safe to call before ``bootstrap()`` — the transport's
    per-process discovery cache means the second probe in
    ``bootstrap`` reuses the binding from this one.

    Best-effort: any failure is silent. The boot-trace
    instrumentation around it lets
    ``tests/integration/parse_boot_traces.py`` measure the
    overlap savings (Q3 in the daemon-boot plan).

    Recommended peer integration:

        class MyApp(App):
            def build(self):
                from azt_collab_client.ui.bootstrap import prewarm
                prewarm()
                return self._build_root_widget()

    Tradeoff: prewarm runs the transport's pyjnius initialisation
    earlier than the rest of the peer expects. If your peer's
    ``build()`` is the first place it touches Android Java
    surfaces, this is fine; otherwise it may shift the cost rather
    than reduce it. Measure with the boot-trace harness before
    enabling in production."""
    global _prewarm_started
    try:
        from kivy.utils import platform
    except Exception:
        platform = ''
    if platform != 'android':
        return
    # Opt-out for the measurement harness: a sentinel file at
    # ``$AZT_HOME/_no_prewarm`` (peer-private on Android) disables
    # the call without rebuilding the peer. Lets
    # ``tests/integration/measure_boot.sh`` toggle scenarios on
    # the same APK via ``adb shell run-as <peer-pkg> touch ...``.
    # The env-var equivalent (``AZT_BOOT_PREWARM=0``) also works
    # for desktop hosts and any path where the env is reachable
    # (e.g. CI).
    if os.environ.get('AZT_BOOT_PREWARM', '1') == '0':
        _boot_trace('prewarm_disabled_by_env')
        return
    try:
        from ..paths import azt_home
        if os.path.exists(os.path.join(azt_home(), '_no_prewarm')):
            _boot_trace('prewarm_disabled_by_sentinel')
            return
    except Exception:
        pass
    if _prewarm_started:
        return
    _prewarm_started = True
    _boot_trace('prewarm_called')

    # B2 main-thread bind. The Python worker we're about to spawn
    # runs on a thread JNI-attached with the system bootclassloader
    # (no app classes), so its ``autoclass(
    # 'org.atoznback.aztcollab.AZTServiceConnector')`` raises
    # ClassNotFoundException — verified empirically on the R500
    # tablet. Performing the bind here on the caller's thread
    # (typically the peer's ``App.build()`` main-thread context)
    # avoids the classloader scope mismatch and gets ``:provider``
    # bound at the earliest possible moment in cold start, which
    # is the whole point of prewarm. Best-effort: any failure is
    # silent; the worker still runs and the transport's later
    # ``discover()`` calls keep retrying via the same path.
    try:
        from jnius import autoclass
        _Connector = autoclass(
            'org.atoznback.aztcollab.AZTServiceConnector')
        _Activity = autoclass('org.kivy.android.PythonActivity')
        _Connector.ensureBound(_Activity.mActivity)
        _boot_trace('prewarm_bound_main_thread')
    except Exception as ex:
        _boot_trace('prewarm_bind_failed',
                    error=type(ex).__name__)

    def _worker():
        _boot_trace('prewarm_worker_start')
        try:
            from .. import check_server_compat
            check_server_compat()
        except Exception as ex:
            _boot_trace('prewarm_worker_error',
                        error=type(ex).__name__)
            return
        _boot_trace('prewarm_worker_done')

    threading.Thread(target=_worker, daemon=True).start()


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
    """Read-only peek at the persisted decline (or ``''``). Does
    NOT clear the entry. Use ``_consume_decline`` at the call site
    that actually decides whether to skip the prompt."""
    cfg = _load_config()
    block = (cfg.get(_CONFIG_NS) or {}).get(_DECLINED_KEY) or {}
    return block.get(repo, '') or ''


def _record_decline(repo, version):
    """Persist that the user declined ``version`` for ``repo``.

    The decline is **one-shot**: it suppresses exactly the next
    prompt for the same version, then clears itself the moment
    ``_consume_decline`` consults it. The intent is "skip the
    next ask", not "never ask again" — a stuck prompt is annoying
    once but a permanently-muted prompt is worse, because the
    user has no path to reconsider beyond waiting for the
    upstream tag to change.

    Cadence in practice: prompt → decline → skip → prompt →
    decline → skip → … . Each accept short-circuits the loop; a
    new upstream version invalidates the stored value via the
    exact-string comparison."""
    cfg = _load_config()
    ns = cfg.setdefault(_CONFIG_NS, {})
    block = ns.setdefault(_DECLINED_KEY, {})
    block[repo] = version
    try:
        _save_config(cfg)
    except OSError as ex:
        print(f'[bootstrap] could not persist decline: {ex}',
              file=sys.stderr, flush=True)


def _consume_decline(repo, version):
    """One-shot consume: returns True iff the persisted decline
    matches ``version`` for ``repo``, and clears the entry as a
    side effect.

    On a True return, the caller should skip the prompt for THIS
    launch only — the decline is already cleared on disk, so the
    NEXT launch (with the same upstream version still present)
    will prompt again. On a False return, either no decline was
    recorded, or the recorded version doesn't match upstream
    (a newer release; the stale entry is left in place since
    clearing it would mean we ask twice in a row if upstream
    rolls back).

    Why one-shot rather than permanent: a "never ask again for
    this version" rule paints users into a corner where they
    can only reconsider by waiting for the next release. One-
    shot gives the user breathing room (one launch's silence)
    without trapping them."""
    cfg = _load_config()
    ns = cfg.get(_CONFIG_NS) or {}
    block = ns.get(_DECLINED_KEY) or {}
    if block.get(repo, '') != version:
        return False
    # Match. Clear the entry and persist.
    del block[repo]
    if not block:
        ns.pop(_DECLINED_KEY, None)
    if not ns:
        cfg.pop(_CONFIG_NS, None)
    try:
        _save_config(cfg)
    except OSError as ex:
        # If we can't persist the clear, the next launch will
        # consume the same decline again — i.e., the user gets
        # an extra silent launch. Not ideal but not harmful;
        # the eventual write will land.
        print(f'[bootstrap] could not persist decline-consume: '
              f'{ex}', file=sys.stderr, flush=True)
    return True


# ── release-meets-minimum probe ───────────────────────────────────────────

def _release_version(repo):
    """Fetch the GitHub release-feed's ``latest`` tag and return it
    as a bare version string (``v`` / ``V`` prefix stripped). Returns
    ``''`` on any fetch failure — caller treats that as "couldn't
    check" rather than "too old"."""
    from .. import _version_tuple  # noqa: F401 — keep import warm
    from .update import _fetch_latest
    try:
        release = _fetch_latest(repo)
    except Exception as ex:
        print(f'[bootstrap] _release_version({repo!r}) fetch '
              f'failed: {ex}', file=sys.stderr, flush=True)
        return ''
    return (release.get('tag_name') or '').lstrip('vV')


def _release_meets_minimum(repo, required_min):
    """Return ``(ok, latest_version, error)`` for the GitHub release
    feed at ``repo``:

    - ``ok=True``: latest >= required_min — safe to offer download.
    - ``ok=False, error='too_old'``: GitHub's latest is older than
      the version this peer requires. Don't bother downloading; tell
      the user to wait or build from source.
    - ``ok=False, error='fetch_failed'``: couldn't reach GitHub /
      no tag in latest. Caller should fall through to the normal
      install path (the user might have a cache or working network
      by the time they tap Install)."""
    from .. import _version_tuple
    if not required_min:
        # No floor specified — every release is acceptable.
        return True, '', ''
    latest = _release_version(repo)
    if not latest:
        return False, '', 'fetch_failed'
    if _version_tuple(latest) < _version_tuple(required_min):
        return False, latest, 'too_old'
    return True, latest, ''


def _show_update_blocked_popup(ctx, body_text, mailto_subject,
                               mailto_body):
    """Shared "we can't proceed and it's not the user's fault" popup
    body. Two known callers:

    - ``_show_release_too_old`` — release feed exists but its latest
      tag is below the required floor.
    - ``_show_no_newer_release`` — release feed is at parity / above
      what we need but the daemon still won't let us through (rare
      edge case — version-namespace mismatch between peer-app
      version and client-library version, or the peer was rebuilt
      without bumping a tag).

    Identical UI shape: a markup body with a ``[ref=email]`` link
    routing to a pre-filled mailto: (subject + body parametrized by
    the caller), Check again to drop the release-cache and re-run
    ``_check_server``, Quit to stop the app via ``App.stop()``.
    Terminal — does **not** fire on_done."""
    import urllib.parse
    import webbrowser
    from kivy.metrics import dp, sp
    from kivy.uix.modalview import ModalView
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.label import Label
    from kivy.uix.button import Button
    from kivy.app import App as _App
    from .. import MAINTAINER_EMAIL

    def _open_mailto(*_):
        url = (
            f'mailto:{MAINTAINER_EMAIL}'
            f'?subject={urllib.parse.quote(mailto_subject)}'
            f'&body={urllib.parse.quote(mailto_body)}'
        )
        try:
            webbrowser.open(url)
        except Exception as ex:
            print(f'[bootstrap] mailto open failed: {ex}',
                  file=sys.stderr, flush=True)

    view = ModalView(size_hint=(0.85, None), height=dp(280),
                     auto_dismiss=False)
    box = BoxLayout(orientation='vertical', padding=dp(16),
                    spacing=dp(12))
    body = Label(text=body_text, markup=True, size_hint_y=1,
                 font_name=ctx.font_name, font_size=sp(14),
                 halign='left', valign='top')
    body.bind(size=lambda w, s: setattr(w, 'text_size', s))
    body.bind(on_ref_press=lambda _w, _ref: _open_mailto())
    box.add_widget(body)

    btn_row = BoxLayout(orientation='horizontal', size_hint_y=None,
                        height=dp(48), spacing=dp(8))
    check_btn = Button(text=_tr('Check again'), font_name=ctx.font_name,
                       font_size=sp(15))
    quit_btn = Button(text=_tr('Quit'), font_name=ctx.font_name,
                      font_size=sp(15))

    def _do_check_again(*_):
        # Dismiss the popup, kill any stale server background
        # process (curative if the user just sideloaded a newer
        # server APK; harmless if the server is already healthy —
        # the next call lazy-spawns from whichever APK is on disk
        # either way), drop the per-process release cache
        # (``_fetch_latest`` keeps results for 5 minutes; without
        # this drop, Check-again would just re-render against the
        # same stale release entry — real user-reported bug), and
        # re-enter the bootstrap state machine on a worker thread.
        # Trace lines bracket the path so a "Check again doesn't
        # work" report shows up in logcat with a clear before/
        # after pair (or the absence of "after" if something
        # raised silently in the worker).
        print('[bootstrap] Check again pressed — '
              'invalidating release cache + '
              're-entering _check_server',
              file=sys.stderr, flush=True)
        view.dismiss()
        from .update import invalidate_release_cache
        invalidate_release_cache(ctx.server_repo)
        invalidate_release_cache(ctx.peer_repo)

        def _retry():
            try:
                _check_server(ctx)
            except Exception as ex:
                print(f'[bootstrap] Check again _check_server '
                      f'raised: {type(ex).__name__}: {ex}',
                      file=sys.stderr, flush=True)
        threading.Thread(target=_retry, daemon=True).start()

    def _do_quit(*_):
        view.dismiss()
        try:
            app = _App.get_running_app()
            if app is not None:
                app.stop()
        except Exception:
            pass

    check_btn.bind(on_press=_do_check_again)
    quit_btn.bind(on_press=_do_quit)
    btn_row.add_widget(check_btn)
    btn_row.add_widget(quit_btn)
    box.add_widget(btn_row)
    view.add_widget(box)
    view.open()


def _prompt_server_reboot_to_apply(ctx, installed_version,
                                    running_version):
    """The user has installed a newer server APK but Android kept
    the old daemon process alive across the replace, so /v1/health
    still reports the old version. Tell them to reboot.

    Specifically a transition-period helper: daemon 0.41.30+
    ships ``SuiteSelfReplaceReceiver`` with the in-APK reap step,
    which kills the surviving old process during the install and
    makes this whole branch unreachable. Until every field daemon
    is at 0.41.30 or later, this is the cleanest way to nudge a
    user who installed the new APK out of the "I installed it but
    it's still saying I need to update" loop.

    Re-uses the ``_show_update_blocked_popup`` shape (Check again
    + Quit + maintainer email link) — same UI vocabulary as the
    other terminal popups in this module. Does NOT fire on_done."""
    body_text = _tr(
        'You have {name} {installed} installed, but the version '
        'currently running is {running}. Restart your device to '
        'switch to the newer version, then reopen this app.\n\n'
        'If reboot doesn\'t help, '
        '[ref=email][color=4ea1ff][u]send the developer an Email'
        '[/u][/color][/ref].'
    ).format(name=ctx.server_display_name,
             installed=installed_version,
             running=running_version)
    subject = (
        f'{ctx.server_display_name}: installed {installed_version} '
        f'but running process is {running_version}')
    msg_body = (
        f'{ctx.server_display_name} installed-on-disk: '
        f'{installed_version}\n'
        f'{ctx.server_display_name} running process:    '
        f'{running_version}\n\n'
        f'The new APK is on disk but the old daemon process is '
        f'still serving. Reboot should normally clear it.\n\n'
        f'(Sent from the in-app "send the developer an Email" '
        f'link.)'
    )
    _show_update_blocked_popup(ctx, body_text, subject, msg_body)


def _show_release_too_old(ctx, latest_seen, required_min,
                         display_name):
    """Release feed has a latest tag, but it's below the floor we
    need. Build a body explaining the version mismatch with a
    mailto link to the maintainer."""
    body_text = _tr(
        '{name} {required} or newer is required, but the latest '
        'available release is {latest}. Wait for an update or '
        '[ref=email][color=4ea1ff][u]send the developer an Email'
        '[/u][/color][/ref].'
    ).format(name=display_name, required=required_min,
             latest=latest_seen or '?')
    subject = (
        f'{display_name}: required version not yet released')
    msg_body = (
        f'{display_name} {required_min} or newer is required, '
        f'but the latest available release is '
        f'{latest_seen or "?"}.\n\n'
        f'(Sent from the in-app "send the developer an Email" '
        f'link.)'
    )
    _show_update_blocked_popup(ctx, body_text, subject, msg_body)


def _show_no_newer_release(ctx, display_name, peer_version,
                            required_client_lib='',
                            bundled_client_lib=''):
    """The ``client_too_old`` + ``force_prompt`` + no-newer-release
    branch. Daemon says we're too old, but ``_peer_update_with_confirm``
    found nothing newer on the peer's release feed.

    Body and email surface all four version anchors so the user
    and the maintainer can see the actual mismatch:

    - peer name + peer version (the recorder version the user can
      see in their device's app list),
    - bundled client library version (from ``azt_collab_client.
      __version__`` in this peer build),
    - required client library version (``min_required`` from the
      daemon's compat handshake).

    The peer-app version and the client-library version live in
    separate version namespaces (recorder bumps independently of
    client lib). The maintainer needs both to know where the gap
    is and what release to cut. Without these anchors the email
    is "the recorder is broken" with no actionable info.

    Shape mirrors ``_show_release_too_old`` (Check again + Quit +
    mailto link). Terminal — replaces the old single-button OK
    popup that silently dropped through to ``on_done`` and let
    the user load a project the daemon would refuse to serve."""
    body_text = _tr(
        '{name} {peer_v} is too old for the AZT Collaboration '
        'service.\nThis build bundles client library {bundled}; '
        'the service requires {required} or newer.\nNo newer '
        '{name} release is published yet. Check back later or '
        '[ref=email][color=4ea1ff][u]send the developer an Email'
        '[/u][/color][/ref].'
    ).format(name=display_name, peer_v=peer_version or '?',
             bundled=bundled_client_lib or '?',
             required=required_client_lib or '?')
    subject = (
        f'{display_name} {peer_version}: still too old for the '
        f'AZT Collaboration service')
    msg_body = (
        f'{display_name} version: {peer_version}\n'
        f'Bundled azt_collab_client: '
        f'{bundled_client_lib or "(unknown)"}\n'
        f'Required client library:  '
        f'{required_client_lib or "(unknown)"}\n\n'
        f'No newer {display_name} release is published yet.\n\n'
        f'(Sent from the in-app "send the developer an Email" '
        f'link.)'
    )
    _show_update_blocked_popup(ctx, body_text, subject, msg_body)


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
    popup makes that wait visible and bounded.

    The body label has its identity stashed at ``popup._body`` so
    ``_update_connecting_popup`` can refresh it on each retry
    without rebuilding the popup. The label carries a sub-line for
    retry count + elapsed seconds + last error kind so the user
    sees concrete progress (no longer a static black box)."""
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
    detail_label = Label(
        text='',
        font_size=sp(11), color=theme.TEXT_DIM,
        font_name=ctx.font_name,
        halign='center', valign='middle',
    )
    detail_label.bind(size=lambda w, _v: setattr(w, 'text_size', w.size))
    content.add_widget(detail_label)
    popup = Popup(
        title=_tr('Connecting…'),
        content=content,
        size_hint=(0.85, None), height=dp(190),
        auto_dismiss=False,
    )
    popup._body = label
    popup._detail = detail_label
    popup.open()
    ctx.connecting_popup = popup


def _update_connecting_popup(ctx, attempt_num, kind):
    """Refresh the connecting popup's detail line with the current
    retry count, elapsed seconds, and last-error kind. Best-effort:
    no-op if the popup isn't up or attributes are missing.

    Surfacing the kind in particular gives the user / maintainer
    actionable info — a 60s ``daemon_not_ready`` wait reads very
    differently from a 60s ``null_bundle`` wait (the latter is a
    signature-grant problem, not a slow boot)."""
    popup = ctx.connecting_popup
    if popup is None:
        return
    detail = getattr(popup, '_detail', None)
    if detail is None:
        return
    elapsed = 0.0
    if ctx.warmup_started_at is not None:
        import time as _t
        elapsed = _t.monotonic() - ctx.warmup_started_at
    parts = [_tr('Attempt {n} of {total}').format(
        n=attempt_num, total=_DAEMON_WARMUP_RETRIES)]
    if elapsed > 0:
        parts.append(_tr('{s}s elapsed').format(s=int(elapsed)))
    if kind:
        # Keep the kind machine-readable; the maintainer-email
        # link in the unresponsive popup picks it up verbatim, so
        # users on slow tablets reading "daemon_not_ready" know to
        # wait, while a "null_bundle" reading prompts a signature /
        # reinstall workflow.
        parts.append(kind)
    detail.text = '  ·  '.join(parts)


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

# Warmup retry budget when the server APK is installed but its
# daemon hasn't responded yet. The 503 ``daemon_not_ready``
# response comes from ``AZTCollabProvider.java`` when the Python
# dispatch callback isn't registered yet — Python interpreter
# still loading.
#
# Adaptive backoff (Phase A, 2026-05): the previous fixed 2s
# interval punished fast devices that were ready at attempt 2
# (always paid 2s). Now the schedule starts short and grows:
# 0.2s, 0.4s, 0.8s, 1.6s, then caps at 2.0s. Total budget
# unchanged: ~60s, covers the worst observed cold start on slow
# tablets (R500-class, 20–30s daemon Python boot) plus margin.
# Warm-cache launches now land in <1s instead of 2s+.
#
# ``null_bundle`` failures (ContentResolver.call returning null —
# usually signature-grant denial, structurally unrecoverable)
# fail fast at ``_NULL_BUNDLE_FAIL_FAST`` consecutive nulls.
# Waiting the full warmup on those is pointless; surface the
# signature/install issue early.
_DAEMON_WARMUP_RETRIES = 30
_DAEMON_WARMUP_BACKOFF_SCHEDULE_S = (0.2, 0.4, 0.8, 1.6)
_DAEMON_WARMUP_BACKOFF_CAP_S = 2.0
_NULL_BUNDLE_FAIL_FAST = 3


def _warmup_delay(attempt):
    """Return the seconds to wait before retry attempt ``attempt``
    (0-indexed: 0 means "first retry"). Schedule: ramp through
    ``_DAEMON_WARMUP_BACKOFF_SCHEDULE_S``, then plateau at
    ``_DAEMON_WARMUP_BACKOFF_CAP_S``."""
    schedule = _DAEMON_WARMUP_BACKOFF_SCHEDULE_S
    if attempt < len(schedule):
        return schedule[attempt]
    return _DAEMON_WARMUP_BACKOFF_CAP_S


def _check_server(ctx, _warmup_attempt=0):
    _boot_trace('compat_probe', attempt=_warmup_attempt)
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

    # Mirror the daemon's UI language locally so peer-side
    # popups (the ones that fire below) respect the language
    # picked in the server APK's settings UI. On Android,
    # ``$AZT_HOME/config.json`` is per-process private, so the
    # server's language pref doesn't reach the peer file-system
    # side. Daemon endpoint ``/v1/config/ui_language`` exposes
    # the canonical value; we apply it via ``i18n.set_language``
    # which both updates the active translator and persists to
    # the peer's own config.json so subsequent popups within
    # this same session keep the language even if the daemon
    # goes away. Best-effort: any failure is silent and
    # bootstrap continues in the local-default language.
    _sync_ui_language_with_daemon()

    if compat.get('ok'):
        _boot_trace('compat_ok')
        _on_ui(_dismiss_connecting_popup, ctx)
        _check_self(ctx)
        return

    err = compat.get('error', '') or ''
    if err == 'server_unreachable':
        # Capture the transport's coarse failure kind for adaptive
        # retry behavior + diagnostic surface. ``check_server_compat``
        # threads it from ``ServerUnavailable.kind``.
        kind = compat.get('kind', '') or ''
        detail = compat.get('detail', '') or ''
        ctx.last_error_kind = kind
        ctx.last_error_detail = detail
        # Disambiguate: package absent vs. daemon-warming-up. If the
        # server APK is installed but the daemon happens to be down
        # (lazy-spawn in progress; OOM-killed mid-call; whatever),
        # prompting to *install* it is wrong — it's already installed.
        # Probe PackageManager before deciding.
        if _server_package_installed():
            # ``null_bundle`` fast-fail. If ``ContentResolver.call``
            # returns null repeatedly, the daemon will never come
            # alive — most common cause is signature-grant denial,
            # which a 60-second wait won't fix. Bail to the
            # unresponsive popup early so the user can act on the
            # actual problem (reinstall, sign matching).
            if kind == 'null_bundle':
                ctx.null_bundle_streak += 1
            else:
                # Any other kind (especially ``daemon_not_ready``)
                # is a "boot in progress" signal — reset the streak.
                ctx.null_bundle_streak = 0
            if ctx.null_bundle_streak >= _NULL_BUNDLE_FAIL_FAST:
                print(f'[bootstrap] null-bundle fast-fail: streak='
                      f'{ctx.null_bundle_streak} attempt='
                      f'{_warmup_attempt}',
                      file=sys.stderr, flush=True)
                _on_ui(_dismiss_connecting_popup, ctx)
                _on_ui(_prompt_server_unresponsive, ctx)
                return
            # Package is there but daemon isn't responding. Most
            # common reason at startup: Android is still spinning up
            # the server APK's Python interpreter. Retry the compat
            # probe with adaptive backoff; the daemon usually
            # settles within a few seconds.
            if _warmup_attempt < _DAEMON_WARMUP_RETRIES:
                if ctx.warmup_started_at is None:
                    import time as _t
                    ctx.warmup_started_at = _t.monotonic()
                # Show the connecting popup with retry count +
                # elapsed seconds + last error kind so the user
                # has feedback during the wait. Refreshed on each
                # iteration via ``_update_connecting_popup``.
                _on_ui(_show_connecting_popup, ctx)
                _on_ui(_update_connecting_popup, ctx,
                       _warmup_attempt + 1, kind)
                _ui_status(ctx, _tr(
                    'Connecting to AZT Collaboration service…'))

                delay = _warmup_delay(_warmup_attempt)
                print(f'[bootstrap] warmup retry attempt='
                      f'{_warmup_attempt + 1}/'
                      f'{_DAEMON_WARMUP_RETRIES} '
                      f'kind={kind!r} delay={delay}s',
                      file=sys.stderr, flush=True)

                def _retry(_dt):
                    threading.Thread(
                        target=_check_server,
                        args=(ctx,),
                        kwargs={'_warmup_attempt':
                                _warmup_attempt + 1},
                        daemon=True).start()
                Clock.schedule_once(_retry, delay)
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
               compat.get('server_version', '') or '',
               compat.get('min_required', '') or '')
        return
    if err == 'client_too_old':
        _on_ui(_dismiss_connecting_popup, ctx)
        # The peer is the one out of date — jump straight to
        # self-update and skip the server prompt. Pass the
        # daemon's required min_client_version so _check_self can
        # confirm the latest peer release on GitHub actually
        # satisfies it before offering the user a download.
        _check_self(ctx, force_prompt=True,
                    required_min=compat.get('min_required', '') or '')
        return

    # Unknown error — log and try self-update anyway.
    _ui_status(ctx, _tr(
        'Service check returned: {error}').format(error=err))
    _check_self(ctx)


# ── step 2: self-update ────────────────────────────────────────────────────

def _check_self(ctx, *, force_prompt=False, required_min=''):
    """Probe the peer's own release feed. If a newer version exists,
    prompt the user; otherwise fire ``on_done``.

    ``force_prompt=True`` skips the no-update branch and always
    prompts even when versions match — used on the
    ``client_too_old`` path so the user sees actionable text rather
    than a silent ``on_done``.

    ``required_min`` (set on the ``client_too_old`` path) is the
    daemon's ``min_client_version`` — and that's the **client
    library** version, not the peer-app's version. For the
    recorder peer (or any cross-namespace peer) those numbers
    aren't comparable: recorder 1.34.0 vs client_lib 0.30.36 has
    no meaningful order. So we DON'T pre-flight ``required_min``
    against the peer's release feed here — the comparison would
    either trivially pass (peer major > client major) or
    trivially fail. Instead we rely on
    ``_peer_update_with_confirm`` to find a newer peer release
    if one exists; if none, ``_show_no_newer_release`` surfaces
    all four version anchors (peer name + peer version + bundled
    client lib + required client lib) so the user / maintainer
    can see exactly which way the mismatch goes.

    The pre-flight that DOES still apply lives in
    ``_prompt_server_update`` — server APK and client library
    share a versioning namespace (locked-step releases), so
    comparing release tags to ``MIN_SERVER_VERSION`` is
    meaningful there."""

    def _on_status(msg):
        _safe(ctx.on_status, msg)

    def _on_no_update():
        if force_prompt:
            # client_too_old + nothing newer to install. Don't
            # fall through to ``on_done`` — the daemon won't talk
            # to this peer, so the host loading a project after
            # this point would just hit RPC errors. Surface the
            # blocked popup with all four version anchors and
            # let the user pick Check again / Quit.
            import azt_collab_client as _client_pkg
            _on_ui(_show_no_newer_release, ctx,
                   ctx.peer_display_name, ctx.peer_version,
                   required_min,
                   getattr(_client_pkg, '__version__', ''))
            return
        _on_done_and_release(ctx)

    def _on_error(msg):
        _safe(ctx.on_error, msg)

    # check_for_update only triggers the install path when a newer
    # release is found. We wrap that with a Yes/No prompt so the user
    # gets to decide. To make that happen, we use a low-level
    # version probe path: pass the helper as-is; it reports progress
    # via on_status. The Yes/No is built into a wrapper.
    #
    # ``mandatory`` flips the prompt's body text + dismiss action.
    # Voluntary (force_prompt=False): "A newer version is available
    # — Update / Not now". Mandatory (force_prompt=True, i.e. the
    # client_too_old branch): "this version is required —
    # Update / Quit". Without this, declining a mandatory update
    # silently dropped the user into the peer with a daemon they
    # can't talk to.
    _peer_update_with_confirm(
        ctx,
        on_status=_on_status,
        on_no_update=_on_no_update,
        on_error=_on_error,
        required_min=required_min,
        mandatory=force_prompt,
    )


# ── language mirror (server pref → peer pref) ────────────────────────────

def _sync_ui_language_with_daemon():
    """Pull the daemon's persisted UI language and apply it
    locally so peer-side popups (bootstrap dialogs, status
    strings) honour the language picked in the server APK's
    settings UI. On Android, ``$AZT_HOME/config.json`` is
    per-process private; the file-system path doesn't propagate
    a preference across processes, so we have to ask the daemon.
    Best-effort: any failure is silent and the peer keeps using
    its local default."""
    try:
        import azt_collab_client as _client_pkg
    except Exception:
        return
    try:
        srv_lang = _client_pkg.get_server_ui_language()
    except Exception as ex:
        print(f'[bootstrap] _sync_ui_language_with_daemon RPC '
              f'failed: {ex}', file=sys.stderr, flush=True)
        return
    if not srv_lang:
        return
    try:
        from .. import i18n
        if srv_lang == i18n.language_pref():
            return
        i18n.set_language(srv_lang)
        print(f'[bootstrap] mirrored UI language from daemon: '
              f'{srv_lang!r}',
              file=sys.stderr, flush=True)
    except Exception as ex:
        print(f'[bootstrap] _sync_ui_language_with_daemon apply '
              f'failed: {ex}', file=sys.stderr, flush=True)


# ── re-upload detection (same-tag, different digest) ──────────────────────
#
# ``_peer_update_with_confirm._probe`` originally only compared
# ``latest_tag`` to ``peer_version`` via tuple ordering. That misses
# the case where a maintainer pushes a fix to the *same release tag*
# (re-uploads the asset on the existing release) — common in dev
# iteration and unavoidable when a tag is hot-fixed in place.
# GitHub's release-asset metadata has carried a ``digest``
# (``sha256:<hex>``) since 2025-06; we persist the last digest we saw
# for each peer repo and treat a change as "newer available" even
# when the tag is identical.
#
# Storage: ``peer_prefs.last_seen_digests`` dict, keyed by ``owner/
# repo``. ``peer_prefs`` writes to whatever ``$AZT_HOME/config.json``
# resolves to in the calling peer's process (peer-private on Android,
# shared on desktop), which is the right scope — each peer tracks
# its own repo independently.
#
# Baseline: on first run for a given repo (no entry) we record the
# current GitHub digest as a starting point. We can't reconstruct
# what was actually installed; what matters is detecting *change
# going forward*, not the absolute historical value. The single
# perverse case this misses — maintainer overwrites the asset
# *between* install and first launch — is rare enough to accept.

def _last_seen_digest(repo):
    """Return the last GitHub asset digest we recorded for
    ``repo``, or ``''`` if never recorded."""
    from .. import peer_pref
    state = (peer_pref('last_seen_digests', {}) or {})
    return state.get(repo, '')


def _record_last_seen_digest(repo, digest):
    """Persist ``digest`` as the last-seen GitHub asset digest for
    ``repo``. No-op for empty digest. Called from ``_probe`` when
    we observe a digest from GitHub (either as the first-run
    baseline or as the new value after a detected change)."""
    if not digest:
        return
    from .. import peer_pref, set_peer_pref
    state = dict(peer_pref('last_seen_digests', {}) or {})
    if state.get(repo) == digest:
        return  # idempotent — avoids needless config.json rewrites
    state[repo] = digest
    set_peer_pref('last_seen_digests', state)


def _peer_update_with_confirm(ctx, *, on_status, on_no_update, on_error,
                              required_min='', mandatory=False):
    """Two-stage self-update: probe latest release on a worker
    thread, prompt the user on the UI thread, then on Yes invoke
    ``check_for_update`` for the download+install. Splits the probe
    from the install so the user gets to confirm with the new
    version number in the message.

    Reuses ``update._fetch_latest`` so the prerelease-skipping policy
    stays in one place (rather than duplicating the listing walk
    here).

    ``required_min`` is forwarded into the confirm-popup body when
    set, so a ``client_too_old`` flow tells the user *which* version
    the daemon needs them to install (not just "newer is
    available").

    ``mandatory=True`` (set on the ``client_too_old`` path) flips
    the popup wording from "newer is available — Update / Not now"
    to "required — Update / Quit". Declining a mandatory update
    closes the app instead of dropping the user into a peer the
    daemon refuses to talk to."""
    from .update import _fetch_latest
    from .. import _version_tuple

    def _probe():
        from .update import _pick_asset
        try:
            release = _fetch_latest(ctx.peer_repo)
        except Exception as ex:
            _on_ui(on_error, _tr(
                'Update check failed: {error}').format(error=ex))
            _on_ui(on_no_update)
            return
        latest = (release.get('tag_name') or '').lstrip('vV')

        # Pull GitHub's authoritative ``asset.digest`` so we can
        # compare against the last-seen digest — same-tag re-uploads
        # (maintainer pushes a fix to the existing release without
        # bumping the version) flip this without flipping the tag,
        # and a tag-only check would silently skip the install.
        gh_digest = ''
        asset = _pick_asset(release, ctx.peer_asset_filename)
        if asset is not None:
            raw = asset.get('digest') or ''
            if raw.startswith('sha256:'):
                gh_digest = raw[len('sha256:'):].strip()
        last_seen = _last_seen_digest(ctx.peer_repo)

        version_newer = bool(
            latest and _version_tuple(latest)
            > _version_tuple(ctx.peer_version)
        )
        # Local-newer-than-release: the installed peer is at a higher
        # version than what GitHub's "latest" release advertises. The
        # typical cause is an adb-sideloaded dev build that hasn't
        # been published yet; we must not propose installing the
        # older online build over it. Gates the digest_changed
        # branch — same-tag re-upload semantics only make sense when
        # the user is on a version ≤ the published tag.
        local_newer = bool(
            latest and _version_tuple(latest)
            < _version_tuple(ctx.peer_version)
        )
        # Version-parity guard: if peer_version == latest, the user
        # is on the published release per the tag-vs-tag comparison,
        # and any digest discrepancy is necessarily an out-of-band
        # install artifact (adb install -r of a freshly published
        # build, in-place rebuild, etc.) rather than a real "newer
        # bytes available" signal. Bootstrap has no way to inspect
        # the bundled digest of the running APK to distinguish that
        # from a legitimate same-tag re-upload, so we treat parity
        # as authoritative and fold the digest mismatch into the
        # silent re-baseline branch below. The cost is missing a
        # real same-tag re-upload until either (a) the maintainer
        # bumps the tag or (b) the next dev-loop install picks up
        # the new bytes naturally. The benefit is no more "1.45.0
        # available" popups after the user already installed
        # 1.45.0. See NOTES_TO_DAEMON.md (2026-05-15, closed).
        at_version_parity = bool(
            latest and latest == ctx.peer_version)
        digest_changed = bool(
            gh_digest and last_seen and gh_digest != last_seen
            and not local_newer
            and not at_version_parity
        )
        # First-run case: no last_seen recorded yet. We can't
        # introspect the installed APK's bundled digest to baseline
        # against, so digest_changed alone can't tell us anything.
        unknown_baseline = bool((not last_seen) and gh_digest)
        # Parity-with-stale-baseline: peer_version == latest but
        # the recorded digest predates this version. Same shape as
        # unknown_baseline — silent re-baseline so the next probe
        # has a clean reference. Catches the adb install -r drift
        # documented in NOTES_TO_DAEMON.md (2026-05-15).
        stale_baseline_at_parity = bool(
            at_version_parity and gh_digest and last_seen
            and gh_digest != last_seen)
        # Mandatory-mode override. The daemon has already told us
        # the running client is too old; the probe's job here is
        # to find a target to install IF one exists.
        #
        # Previously "always prompt as long as the release feed
        # gave us *something*" — which looped when peer_version ==
        # latest, because installing the same version doesn't make
        # the daemon any happier. User installs 1.42.16, daemon
        # still says client_too_old, popup says "1.42.16 available,
        # install"; install, repeat. The peer's parity-with-latest
        # case has its own destination — _show_no_newer_release —
        # but the unconditional force_prompt hid it.
        #
        # Now force the prompt only when there's an actual reason
        # to install: a real version bump (version_newer), a
        # same-tag re-upload with new bytes (digest_changed), or a
        # first-run unknown baseline (so we record a digest to
        # baseline against on the next probe — the install at this
        # point is a no-op against same-version-latest, but the
        # recorded baseline means the NEXT probe falls cleanly
        # into _show_no_newer_release). The parity case otherwise
        # falls through to on_no_update → _show_no_newer_release,
        # which surfaces all four version anchors (peer name +
        # peer version + bundled client lib + required client lib)
        # plus a Check-again + mailto so the user understands
        # they're not at fault and the maintainer knows to cut a
        # fresh build.
        mandatory_force = bool(
            mandatory and latest
            and (version_newer or digest_changed or unknown_baseline))
        print(f'[bootstrap] _probe peer={ctx.peer_repo!r} '
              f'latest={latest!r} peer_v={ctx.peer_version!r} '
              f'gh_digest={gh_digest[:12]!r}… '
              f'last_seen={last_seen[:12]!r}… '
              f'version_newer={version_newer} '
              f'local_newer={local_newer} '
              f'at_version_parity={at_version_parity} '
              f'digest_changed={digest_changed} '
              f'stale_baseline_at_parity={stale_baseline_at_parity} '
              f'mandatory={mandatory} '
              f'mandatory_force={mandatory_force}',
              file=sys.stderr, flush=True)

        needs_update = (
            version_newer or digest_changed or mandatory_force)

        if not needs_update:
            # Quiet path. Take the baseline now so the NEXT probe
            # can detect a real change. Two cases:
            # - ``unknown_baseline``: first run for this repo,
            #   record the digest as the starting point.
            # - ``stale_baseline_at_parity``: an out-of-band
            #   install (adb install -r, dev rebuild) moved us
            #   to peer_version == latest without going through
            #   the in-app probe, so last_seen is from an earlier
            #   version's session. Re-baseline silently — the
            #   user is on the latest published bytes per the
            #   version field, no popup justified.
            # Only do this on the no-prompt branch — recording
            # before a prompt would let a "Quit" decline silently
            # re-baseline and mask the pending update on next
            # launch.
            if unknown_baseline or stale_baseline_at_parity:
                _record_last_seen_digest(ctx.peer_repo, gh_digest)
            _on_ui(on_no_update)
            return
        # Decline memory: if the user already said "Not now" for
        # this exact version, skip the prompt. A new version moves
        # us off the recorded value automatically.
        #
        # Two carve-outs to the silent skip:
        # - ``mandatory``: declines never apply on the
        #   ``client_too_old`` path. The popup's only dismiss
        #   action there is Quit (no decline is ever recorded
        #   anyway, but belt-and-braces).
        # - ``digest_changed``: same-tag re-upload. The "decline"
        #   was against the previous bytes — those bytes no
        #   longer exist on GitHub. Treat the new digest as a
        #   fresh release the user hasn't been asked about. This
        #   matches the user-reported case: "Check again doesn't
        #   currently show a new apk online, despite a different
        #   sha256."
        if (not mandatory and not digest_changed
                and _consume_decline(ctx.peer_repo, latest)):
            # One-shot decline consumed: silently skip THIS
            # launch's prompt. The stored decline is now cleared
            # so the next launch (still on the same upstream
            # version) will prompt again.
            _on_ui(on_no_update)
            return
        _on_ui(_prompt_self_update, ctx, latest, mandatory, gh_digest)

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


def _open_server_apk_launcher():
    """Fire an ACTION_MAIN/CATEGORY_LAUNCHER intent for the server
    APK package so its launcher activity moves to the foreground.
    Returns True on success, False otherwise. Android-15 process-
    freezer workaround: with the server APK foregrounded, its
    package's processes (including ``:provider``) un-freeze and
    the peer's next ContentResolver call lands."""
    try:
        from jnius import autoclass
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        Intent = autoclass('android.content.Intent')
        activity = PythonActivity.mActivity
        if activity is None:
            return False
        pm = activity.getPackageManager()
        intent = pm.getLaunchIntentForPackage(_SERVER_PACKAGE_NAME)
        if intent is None:
            return False
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        activity.startActivity(intent)
        return True
    except Exception as ex:
        print(f'[bootstrap] _open_server_apk_launcher failed: {ex}',
              file=sys.stderr, flush=True)
        return False


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

    Android 15 add-on: an "Open AZT Collaboration" button. The
    OS's app freezer can keep the server APK's ``:provider``
    process frozen across peer-driven lazy-spawn (verified on
    R500_V_US tablet), so peers time out before the daemon
    responds. Tapping the button launches the server APK's
    launcher activity, which un-freezes the whole package's
    process group; we then re-run ``_check_server`` from a fresh
    retry budget, and when the user switches back to the peer,
    bootstrap's compat probe lands and the host resumes normal
    startup. Cheaper recovery than reinstalling.

    Does not fire on_done — the popup is terminal. If the user
    reinstalls and the new daemon responds, the post-install
    continuation chain re-runs ``_check_server`` and the host's
    on_done fires from the healthy path."""
    from .popups import install_server_apk_popup

    # Surface diagnostic state so the user (or maintainer if they
    # tap an Email link) can act on the actual problem rather than
    # blindly retrying. ``last_error_kind`` distinguishes
    # ``daemon_not_ready`` (slow boot, retry / wake) from
    # ``null_bundle`` (signature-grant denial — reinstall with
    # matching keystore) from other transport failures. Server APK
    # versionName via PackageManager confirms which build is
    # actually installed (vs. what the user thinks they installed).
    diag_lines = []
    if ctx.last_error_kind:
        diag_lines.append(_tr('Last error: {kind}').format(
            kind=ctx.last_error_kind))
    if ctx.warmup_started_at is not None:
        import time as _t
        elapsed = int(_t.monotonic() - ctx.warmup_started_at)
        diag_lines.append(_tr('Waited {s}s before giving up.').format(
            s=elapsed))
    server_versionname = ''
    try:
        from .update import _installed_version_name
        server_versionname = _installed_version_name(
            _SERVER_PACKAGE_NAME)
    except Exception:
        pass
    if server_versionname:
        diag_lines.append(_tr(
            'Installed {name} version: {v}').format(
                name=ctx.server_display_name,
                v=server_versionname))

    body = _tr(
        'The AZT Collaboration service ({name}) is installed but '
        'did not respond. Tap Open {name} to wake the service, '
        'then come back here. Or tap Install to reinstall it, or '
        'Quit to close this app and try again later.'
    ).format(name=ctx.server_display_name)
    if diag_lines:
        body = body + '\n\n' + '\n'.join(diag_lines)

    def _wake_and_recheck():
        # Launch the server APK's launcher activity to un-freeze
        # its process group, then re-enter _check_server on a
        # short delay so the daemon has a window to register
        # callbacks. Bootstrap's existing retry loop + connecting
        # popup take over from there; when the user switches back
        # to the peer (Kivy Clock resumes), the compat probe lands
        # and the host's on_done fires from the healthy path.
        opened = _open_server_apk_launcher()
        if not opened:
            _ui_status(ctx, _tr(
                'Could not open {name}.').format(
                    name=ctx.server_display_name))
        # Show the connecting popup right away so when the user
        # switches back to the peer there's a visible "waiting"
        # state instead of a blank screen.
        _on_ui(_show_connecting_popup, ctx)

        def _resume(_dt):
            threading.Thread(target=_check_server, args=(ctx,),
                             daemon=True).start()
        Clock.schedule_once(_resume, 2.0)

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
        on_open_app=_wake_and_recheck,
        open_app_label=_tr('Open {name}').format(
            name=ctx.server_display_name),
        repo=ctx.server_repo,
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
        repo=ctx.server_repo,
    )


def _prompt_server_update(ctx, current_version, min_required=''):
    """Server present but too old. Same popup shape as the missing-
    server prompt, different body / title / Install-button label /
    current_version (so check_for_update doesn't redownload an
    identical release).

    **Pre-flight: installed-on-disk vs running-process.** Before
    deciding the user needs to download anything, check whether
    the server APK *on disk* is already newer than what /v1/health
    reports. That mismatch means the user has already installed
    the new APK but Android kept the old daemon process alive
    across the replace (the symptom this whole package-
    replacement work series exists to handle). In that case,
    redirect to ``_prompt_server_reboot_to_apply`` instead of
    asking the user to re-download — the bytes are already where
    they need to be; what's missing is a process restart.

    **Pre-flight version check.** If ``min_required`` is set (the
    daemon's compat response carries it on the ``server_too_old``
    branch), we fetch GitHub's latest server release and confirm
    it's >= min_required before opening the install popup. If the
    latest release is *also* too old to satisfy the peer, the
    install would land us right back at ``server_too_old`` — so
    instead we surface a one-button "Wait for an update or build
    from source" popup. Saves a useless download and a confused
    user.

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
    from .. import _version_tuple
    installed = _installed_server_version()
    if installed and current_version and (
            _version_tuple(installed) > _version_tuple(current_version)):
        # The bytes on disk are already newer than the running
        # daemon. Asking the user to download again is wrong; what
        # they need is a process restart. Reboot popup, no download.
        print(f'[bootstrap] server installed={installed!r} > '
              f'running={current_version!r}; offering reboot '
              f'instead of update',
              file=sys.stderr, flush=True)
        _prompt_server_reboot_to_apply(
            ctx, installed_version=installed,
            running_version=current_version)
        return
    if current_version and _consume_decline(
            ctx.server_repo, current_version):
        # One-shot decline consumed: let the host continue at the
        # old server version and surface the daemon's compat
        # error its own way. The next launch (still against the
        # same too-old server) will re-prompt.
        _on_done_and_release(ctx)
        return
    # Pre-flight: does the upstream release feed have a build that
    # would actually satisfy our floor?
    if min_required:
        ok, latest_seen, why = _release_meets_minimum(
            ctx.server_repo, min_required)
        if (not ok) and why == 'too_old':
            _show_release_too_old(ctx, latest_seen, min_required,
                                  ctx.server_display_name)
            return
        # ``fetch_failed`` falls through — user might have a
        # working connection by the time they tap Install, and
        # check_for_update inside the popup re-fetches anyway.
    body = (
        _tr(
            '{name} {required} or newer is required (you have '
            '{current}). Tap Update to download and install it.'
        ).format(name=ctx.server_display_name,
                 required=min_required, current=current_version)
        if min_required
        else _tr(
            'A newer version of {name} is required. '
            'Tap Update to download and install it.'
        ).format(name=ctx.server_display_name)
    )
    _release_running()
    install_server_apk_popup(
        on_status=ctx.on_status,
        font_name=ctx.font_name,
        body_message=body,
        current_server_version=current_version or '0.0.0',
        install_label=_tr('Update'),
        title=_tr('Update AZT Collaboration?'),
        on_install_complete=lambda: _post_install_continuation(ctx),
        repo=ctx.server_repo,
    )


def _prompt_self_update(ctx, latest_version, mandatory=False,
                        gh_digest=''):
    """Peer self-update prompt. Same popup as the server install /
    update cases (so users see one consistent install/update UI
    across the suite), parameterized for the peer's own APK.

    ``gh_digest`` is the SHA-256 of the GitHub asset that's about
    to be offered. When the user taps Update we record it as the
    new baseline via ``_record_last_seen_digest`` so the same-tag
    re-upload loop can break: without this, the next bootstrap
    probe still sees ``digest_changed=True`` (last_seen is stale)
    and re-prompts forever, even after a successful install.
    Recording at tap time (rather than at install-complete) is
    the practical compromise — for self-update we can't reliably
    detect install completion (versionName doesn't flip on a
    same-tag re-upload, and our process gets killed by Android
    during install so we lose the in-process poll). If the install
    ultimately fails the user is stuck at the old version with no
    further prompt, which they can recover from via the
    in-settings Update button or a manual reinstall.

    Two flavours, picked by ``mandatory``:

    - **Voluntary** (``mandatory=False``, the normal newer-release-
      detected path): body reads "A newer version of this app
      ({version}) is available." Dismiss button is "Not now",
      action is ``'dismiss'`` — peer keeps running at the current
      version, decline is recorded via ``_record_decline``. The
      decline is one-shot: it suppresses exactly the next prompt
      and clears (see ``_consume_decline``), so the cadence is
      prompt → decline → skip → prompt → decline → skip → …
      rather than the previous "never ask again for this version"
      shape. A new upstream version invalidates the stored value
      via exact-string compare.
    - **Mandatory** (``mandatory=True``, the ``client_too_old`` +
      newer-version-exists path): body reads
      "{name} {version} is required to use the AZT Collaboration
      service." Dismiss button is "Quit", action is ``'quit'`` —
      peer can't function without the daemon agreeing to talk to
      it, so declining means closing the app rather than dropping
      the user into a half-broken UI. No decline memory: we
      can't usefully remember a decline of a mandatory update;
      next launch just re-asks.

    Common bits in both:
    - ``direct_url`` → composed from peer_repo + peer_asset_filename.
    - ``open_page_url`` → peer's release page.
    - ``install_target_package=''`` → explicitly skip polling.
      Self-install replaces the running peer process; polling our
      own package would block forever."""
    from .popups import install_server_apk_popup
    if mandatory:
        # Two flavours of "mandatory update available":
        # - latest > peer: a genuinely newer peer release.
        # - latest == peer: same-tag re-upload (digest_changed
        #   was True in _probe). User is at the latest tag but
        #   bytes differ. Phrasing "{name} 0.8.2 is required"
        #   read confusingly when the user was already at 0.8.2;
        #   distinguish the two cases so the body actually
        #   describes what's about to happen.
        if str(latest_version) == str(ctx.peer_version):
            body = _tr(
                'A new build of {name} {version} is available. '
                'The current build is too old for the AZT '
                'Collaboration service. Tap Update to install '
                'the new build, or Quit to close this app.'
            ).format(name=ctx.peer_display_name,
                     version=latest_version)
        else:
            body = _tr(
                '{name} {peer_v} is too old for the AZT '
                'Collaboration service. Tap Update to install '
                '{name} {latest}, or Quit to close this app.'
            ).format(name=ctx.peer_display_name,
                     peer_v=ctx.peer_version,
                     latest=latest_version)
        dismiss_label = _tr('Quit')
        dismiss_action = 'quit'
    else:
        body = _tr(
            'A newer version of this app ({version}) is available. '
            'Tap Update to download and install it.'
        ).format(version=latest_version)
        dismiss_label = _tr('Not now')
        dismiss_action = 'dismiss'
    direct_url = (
        f'https://github.com/{ctx.peer_repo}/'
        f'releases/latest/download/{ctx.peer_asset_filename}'
    )
    open_page_url = (
        f'https://github.com/{ctx.peer_repo}/releases/latest'
    )

    # Self-install: poll our own package for a versionName flip, and
    # call ``App.stop()`` when it does. The pre-0.30.41 code skipped
    # polling on the assumption that Android would always kill our
    # process during the install — but on some devices /
    # configurations the running peer survives the install, comes
    # back to foreground, and re-renders the same popup. User
    # observed: "downloaded and installed fine, but then I found
    # myself back at the same popup." Polling + explicit App.stop()
    # closes us cleanly so the next launch picks up the new APK.
    # Safe in both cases:
    # - Android kills us during install → poll thread dies with us,
    #   no harm.
    # - Android doesn't kill us → poll detects the version change,
    #   we stop ourselves, user re-launches into the new code.
    peer_pkg = ''
    try:
        from jnius import autoclass
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        activity = PythonActivity.mActivity
        if activity is not None:
            peer_pkg = activity.getPackageName() or ''
    except Exception:
        pass

    def _on_self_install_complete():
        try:
            from kivy.app import App as _App
            app = _App.get_running_app()
            if app is not None:
                app.stop()
        except Exception:
            pass

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
        dismiss_label=dismiss_label,
        dismiss_action=dismiss_action,
        install_target_package=peer_pkg,
        on_install_complete=_on_self_install_complete,
        repo=ctx.peer_repo,
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
        # Decline memory only applies to the voluntary path.
        # Mandatory dismiss = quit (the popup's own dismiss_action
        # closes the app), so there's nothing to "remember" — and
        # ``_on_done_and_release`` would drop the user back into
        # the peer with a daemon that won't talk to them.
        if mandatory:
            return
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
            def _on_install_tap(*_):
                _state['install_started'] = True
                # Break the same-tag re-upload loop. Without this,
                # last_seen stays at the pre-install digest forever
                # and every bootstrap re-prompts because digest_changed
                # remains True even after install completes
                # (versionName doesn't flip on same-tag re-uploads, so
                # we can't condition on a successful install). User
                # report 0.30.45: "this window keeps coming up since
                # the digest is still different from gh."
                if gh_digest:
                    _record_last_seen_digest(ctx.peer_repo, gh_digest)
            install_btn.fbind('on_release', _on_install_tap)
    except Exception:
        pass
    popup.bind(on_dismiss=_record_on_dismiss)


# ``_show_info`` was deleted in 0.30.34 — the only caller (the
# client_too_old + no-newer-release branch) now uses
# ``_show_no_newer_release`` for parity with
# ``_show_release_too_old``: same Check-again + Quit + mailto-link
# UI. The old single-button OK popup silently dropped through to
# ``on_done`` after the user dismissed it, which was the cause of
# the user-reported "user lands in the client with a loaded
# project but the daemon won't talk to them" bug.
