"""Self-update flow for suite APKs.

Reusable across the server APK and every peer (recorder, viewer, …).
Each host wires a small adapter that supplies three pieces of identity:

    repo             — 'owner/repo' on GitHub (releases endpoint)
    current_version  — caller's running ``__version__`` string
    asset_filename   — the release asset to fetch (e.g. ``aztcollab.apk``,
                       ``aztrecorder.apk``; convention is
                       ``<buildozer.spec package.name>.apk``). Omit to
                       derive at runtime via ``default_asset_filename``.

The helper spawns a worker thread, polls
``GET /repos/{repo}/releases/latest``, compares the tag name to
``current_version``, and on a newer release downloads the matching
asset to MediaStore Downloads and dispatches Android's
``ACTION_VIEW`` install intent. Progress / status reaches the host
through the ``on_status`` callback, marshaled back to the UI thread
via ``Clock.schedule_once`` so hosts can update labels directly.

Android-only — non-Android hosts get a translated
"APK install is only available on Android." through ``on_error``.

No SHA verification in v1 — TLS + GitHub trust + the suite-wide
keystore enforced by Android's signature-match install check are the
integrity layers. A future hardening pass can add a ``.sha256``
companion asset if a sister repo's release process publishes one.
"""

import json
import os
import sys
import threading
import time
import urllib.request

from kivy.clock import Clock

from .. import _version_tuple
from ..net import _ensure_ssl
from ..paths import azt_home
from ..translate import tr as _tr


_GITHUB_API = 'https://api.github.com'
_USER_AGENT = 'azt-collab-updater/1'
_DOWNLOAD_CHUNK = 65536

# Per-process cache of GitHub release lookups. Keyed by repo slug;
# each entry is ``(fetched_at_epoch, release_dict)``. A peer that
# launches with the bootstrap workflow plus a manual settings-screen
# update tap shouldn't pay two API calls; nor should multiple peers
# launching from behind one NAT in quick succession all chase the
# 60/hour anonymous rate limit. TTL is short enough that a fresh
# release within minutes of the last poll is still picked up before
# users typically retry.
_release_cache = {}
_RELEASE_CACHE_TTL_S = 300


def invalidate_release_cache(repo=None):
    """Drop the per-process cache for ``repo`` (or every entry when
    called with no arg) so the next ``_fetch_latest`` re-probes
    GitHub. Used by bootstrap's "Check again" button so a freshly-
    published release is visible immediately rather than waiting for
    the 5-minute TTL to expire."""
    if repo is None:
        _release_cache.clear()
        return
    _release_cache.pop(repo, None)

# Install-completion polling. After dispatching the install intent
# we don't get a callback when Android finishes (or the user backs
# out of the system installer). For installs whose target is a
# *different* package than ours (server-from-peer), we can poll
# PackageManager for the new versionName. Self-installs can't
# benefit — the install replaces our running process — but every
# peer-triggered server install is helped.
_INSTALL_POLL_INTERVAL_S = 5
_INSTALL_POLL_TIMEOUT_S = 300

# Reuse a previously-downloaded APK when the user is retrying the
# same install (e.g., they granted "Install unknown apps" and
# tapped Install again). Verified by SHA-256 against a sidecar
# file written after the original download — robust against
# arbitrary delays (the mtime-based heuristic that preceded this
# was time-bounded and not specific enough; SHA is the
# definitive integrity check).


def _on_ui(fn, *args):
    """Marshal a callback to the Kivy UI thread. No-op if Clock is not
    available (rare; pre-build smoke tests)."""
    try:
        Clock.schedule_once(lambda dt: fn(*args), 0)
    except Exception:
        try:
            fn(*args)
        except Exception:
            pass


def _safe_call(cb, *args):
    if cb is None:
        return
    try:
        cb(*args)
    except Exception as ex:
        print(f'[update] callback raised: {ex}', file=sys.stderr,
              flush=True)


def _fetch_latest(repo):
    """Latest **stable** release for ``repo``: walks
    ``/releases?per_page=20`` and returns the first entry whose
    ``prerelease`` flag is false. Falls back to the
    ``/releases/latest`` endpoint if every recent release is a
    prerelease (or if the listing endpoint refused). Raises on
    network / HTTP failure; caller wraps in try/except.

    Why not just `/releases/latest`: that endpoint excludes drafts
    but **includes prereleases**, so a project that pushes a v0.29-rc
    tag would silently get auto-installed onto every peer. Per-suite
    policy (research_notes_2026-05.md §4) is "stable channel only by
    default" — peers don't opt users into betas without intent.

    Per-process cache: results are kept for ``_RELEASE_CACHE_TTL_S``
    so a peer that hits this twice in quick succession (bootstrap on
    startup + a manual settings-screen update tap, or two peers
    behind one NAT) doesn't double-pay the GitHub API. Cache is
    process-local — a fresh launch always re-probes."""
    cached = _release_cache.get(repo)
    if cached and (time.time() - cached[0]) < _RELEASE_CACHE_TTL_S:
        return cached[1]
    _ensure_ssl()
    list_url = f'{_GITHUB_API}/repos/{repo}/releases?per_page=20'
    req = urllib.request.Request(list_url, headers={
        'Accept': 'application/vnd.github+json',
        'User-Agent': _USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            releases = json.load(resp)
    except Exception:
        # Listing endpoint refused or returned junk; fall back to
        # the latest singleton so we still have a chance.
        result = _fetch_latest_singleton(repo)
        _release_cache[repo] = (time.time(), result)
        return result
    for rel in releases or []:
        if not rel.get('prerelease') and not rel.get('draft'):
            _release_cache[repo] = (time.time(), rel)
            return rel
    # Every recent release is a prerelease — fall back to the
    # canonical latest endpoint, which will give us *something* if
    # one exists, even if it's a prerelease (better to surface that
    # than silently install nothing).
    result = _fetch_latest_singleton(repo)
    _release_cache[repo] = (time.time(), result)
    return result


def _fetch_latest_singleton(repo):
    _ensure_ssl()
    url = f'{_GITHUB_API}/repos/{repo}/releases/latest'
    req = urllib.request.Request(url, headers={
        'Accept': 'application/vnd.github+json',
        'User-Agent': _USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def _pick_asset(release, asset_filename):
    for asset in release.get('assets') or []:
        if asset.get('name') == asset_filename:
            return asset
    return None


_DOWNLOAD_RETRY_ATTEMPTS = 3
_DOWNLOAD_RETRY_BACKOFF_S = 5
def _sha256(path):
    """Compute the SHA-256 of ``path`` and return the hex digest,
    or ``''`` on any error. Streams through 64 KB blocks so a
    multi-megabyte APK doesn't pin memory."""
    import hashlib
    try:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(_DOWNLOAD_CHUNK), b''):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ''


def _save_download_sha(path):
    """Compute the SHA-256 of ``path`` and write it to
    ``<path>.sha256``. Called right after a successful download
    so the sidecar captures the freshly-downloaded content; later
    reuse checks recompute the SHA and verify against this
    value. Failures are non-fatal — a missing sidecar just means
    the next reuse check returns False and we redownload."""
    digest = _sha256(path)
    if not digest:
        return
    try:
        with open(path + '.sha256', 'w') as f:
            f.write(digest)
    except OSError as ex:
        print(f'[update] sidecar write failed: {ex}')


def _has_fresh_download(path, expected_sha256=''):
    """True iff ``path`` exists AND hashes to a known-good value.

    Two modes, depending on whether ``expected_sha256`` is provided:

    - **Authoritative mode** (``expected_sha256`` non-empty): the
      file's SHA-256 must equal the supplied digest. Used when the
      GitHub release JSON carries an ``asset.digest`` field
      (added 2025-06; format ``sha256:<hex>``). This is the strong
      check — matches mean the cached bytes are exactly what GitHub
      serves *right now*, so reusing is safe even if the asset was
      re-uploaded since our cache was filled. A re-uploaded asset
      with the same name but different bytes (e.g. a fix release
      reusing the old tag, or a same-version-different-rebuild
      situation) flips the digest and the cache gets re-fetched.
    - **Self-consistency mode** (``expected_sha256`` empty / not
      provided): falls back to the sidecar that ``_save_download_sha``
      writes after a successful download — i.e. confirms the cached
      bytes haven't been corrupted on disk since we wrote them, but
      can't tell us if GitHub is now serving different bytes. Used
      when the release asset doesn't expose a digest (legacy assets
      uploaded pre-rollout) or when the caller doesn't have one at
      hand.

    ``_download`` writes to ``<path>.part`` and renames on success,
    so a present ``<path>`` is always a complete download — no
    partial-file salvage logic needed."""
    if not os.path.exists(path):
        print(f'[update] _has_fresh_download: {path} not present',
              file=sys.stderr, flush=True)
        return False
    file_sha = _sha256(path)
    if not file_sha:
        print(f'[update] _has_fresh_download: could not hash {path}',
              file=sys.stderr, flush=True)
        return False
    if expected_sha256:
        match = file_sha == expected_sha256
        print(f'[update] _has_fresh_download: digest mode '
              f'file={file_sha[:16]}… expected={expected_sha256[:16]}… '
              f'match={match}',
              file=sys.stderr, flush=True)
        return match
    sidecar = path + '.sha256'
    if not os.path.exists(sidecar):
        print(f'[update] _has_fresh_download: no sidecar at {sidecar}',
              file=sys.stderr, flush=True)
        return False
    try:
        with open(sidecar) as f:
            stored = f.read().strip()
    except OSError as ex:
        print(f'[update] _has_fresh_download: sidecar read failed: '
              f'{ex}', file=sys.stderr, flush=True)
        return False
    if not stored:
        print('[update] _has_fresh_download: sidecar empty',
              file=sys.stderr, flush=True)
        return False
    match = file_sha == stored
    print(f'[update] _has_fresh_download: sidecar mode '
          f'file={file_sha[:16]}… stored={stored[:16]}… '
          f'match={match}',
          file=sys.stderr, flush=True)
    return match


def _wrappable_url(url):
    """Insert soft line-breaks at path separators so a URL fits a
    narrow popup body. Kivy Labels wrap at whitespace; URLs have no
    whitespace, so they overflow / clip without help. We use real
    newlines (rather than zero-width spaces) because Kivy's text
    measurement doesn't honour zero-width characters consistently."""
    if not url or len(url) < 50:
        return url
    # Break after each '/' but keep the slash on the previous line.
    # Resulting display:
    #     https://
    #     github.com/
    #     kent-rasmussen/
    #     azt-collab/
    #     releases/...
    return url.replace('/', '/\n')


# HTTP statuses we treat as transient on the asset download. 404 is
# load-bearing here: during a release upload GitHub publishes the
# release JSON (so /releases/latest returns it and ``browser_download_url``
# is populated) before the binary finishes uploading, so the asset
# URL briefly 404s. Same window when a maintainer deletes + re-uploads
# an asset on a published release. The classic 5xx + 429 are obviously
# transient too.
_DOWNLOAD_RETRY_STATUSES = (404, 429, 500, 502, 503, 504)


def _download(url, dest, total_bytes, on_progress, on_status=None,
              attempts=_DOWNLOAD_RETRY_ATTEMPTS):
    """Stream ``url`` into ``dest``. Calls ``on_progress(pct)`` from
    the worker thread roughly every 64 KB; caller marshals to UI.

    Retries on transient HTTP statuses
    (``_DOWNLOAD_RETRY_STATUSES``) and on network errors. Between
    attempts we wait ``_DOWNLOAD_RETRY_BACKOFF_S * attempt`` seconds
    and (if ``on_status`` is given) surface a translated
    "Release in progress — retrying in {s}s…" so the user sees what
    we're doing instead of a hung worker thread. Final-attempt
    failure raises; caller turns it into a translated error message."""
    import urllib.error
    import sys
    _ensure_ssl()
    last_exc = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={
                # GitHub's CDN serves 404 to some bot-pattern UAs;
                # mimic curl which definitely works for the same URL.
                'User-Agent': 'azt-collab-updater/1 (+curl-compat)',
                'Accept': '*/*',
            })
            tmp = dest + '.part'
            received = 0
            print(f'[update] GET {url}', file=sys.stderr, flush=True)
            with urllib.request.urlopen(req, timeout=60) as resp:
                # Log redirect chain so 404s on the CDN side are
                # distinguishable from 404s on github.com.
                final_url = resp.geturl()
                if final_url != url:
                    print(f'[update] redirected to {final_url}',
                          file=sys.stderr, flush=True)
                print(f'[update] {resp.status} {resp.reason}',
                      file=sys.stderr, flush=True)
                # If the caller didn't pre-compute total_bytes (the
                # direct-download path that bypasses the GitHub API
                # has no asset metadata), read it from the response
                # headers so progress percentages still work.
                if not total_bytes:
                    try:
                        total_bytes = int(resp.headers.get(
                            'Content-Length') or 0)
                    except (TypeError, ValueError):
                        total_bytes = 0
                with open(tmp, 'wb') as out:
                    while True:
                        chunk = resp.read(_DOWNLOAD_CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
                        received += len(chunk)
                        if total_bytes and on_progress is not None:
                            on_progress(int(received * 100 / total_bytes))
            os.replace(tmp, dest)
            return
        except urllib.error.HTTPError as e:
            last_exc = e
            # Log enough to disambiguate "github.com 404" from "CDN
            # redirect-target 404" — they have very different
            # diagnoses (asset truly missing vs. bot-pattern
            # rejection on the CDN edge).
            try:
                hit_url = e.url
            except Exception:
                hit_url = url
            print(f'[update] HTTP {e.code} from {hit_url}',
                  file=sys.stderr, flush=True)
            if e.code not in _DOWNLOAD_RETRY_STATUSES \
                    or i == attempts - 1:
                raise
        except (urllib.error.URLError, OSError) as e:
            last_exc = e
            print(f'[update] transport error on {url}: '
                  f'{type(e).__name__}: {e}',
                  file=sys.stderr, flush=True)
            if i == attempts - 1:
                raise
        wait = _DOWNLOAD_RETRY_BACKOFF_S * (i + 1)
        if on_status is not None:
            try:
                on_status(_tr(
                    'Release in progress — retrying in {s}s…'
                ).format(s=wait))
            except Exception:
                pass
        time.sleep(wait)
    if last_exc:
        raise last_exc


def _media_store_uri(apk_path, asset_filename):
    """Insert ``apk_path`` into MediaStore Downloads and return the
    resulting ``content://`` URI. Mirrors the share.py code path so the
    install intent can grant a per-URI read to the system installer
    without configuring a separate FileProvider."""
    from jnius import autoclass, cast
    PythonActivity = autoclass('org.kivy.android.PythonActivity')
    ContentValues = autoclass('android.content.ContentValues')
    MediaStoreDownloads = autoclass('android.provider.MediaStore$Downloads')
    activity = PythonActivity.mActivity
    context = cast('android.content.Context', activity)
    resolver = context.getContentResolver()
    values = ContentValues()
    values.put('_display_name', asset_filename)
    values.put('mime_type', 'application/vnd.android.package-archive')
    uri = resolver.insert(MediaStoreDownloads.EXTERNAL_CONTENT_URI, values)
    if not uri:
        raise RuntimeError('MediaStore insert refused')
    fos = resolver.openOutputStream(uri)
    try:
        with open(apk_path, 'rb') as f:
            while True:
                chunk = f.read(_DOWNLOAD_CHUNK)
                if not chunk:
                    break
                fos.write(chunk)
    finally:
        fos.close()
    return uri


def _can_install_packages(activity):
    """Android 8+: ``REQUEST_INSTALL_PACKAGES`` is install-time but each
    source app must also be allowlisted by the user under
    Settings → "Install unknown apps". Returns True iff the toggle is
    on for our package."""
    try:
        pm = activity.getPackageManager()
        return bool(pm.canRequestPackageInstalls())
    except Exception:
        # Pre-Oreo (API < 26) — the permission alone is sufficient.
        return True


def _open_unknown_sources_settings(activity):
    """Send the user to the "Install unknown apps" settings page,
    pre-scoped to our package. They flip the toggle, hit back, and
    re-tap Update."""
    from jnius import autoclass
    Intent = autoclass('android.content.Intent')
    Uri = autoclass('android.net.Uri')
    Settings = autoclass('android.provider.Settings')
    intent = Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES)
    intent.setData(Uri.parse(f'package:{activity.getPackageName()}'))
    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    activity.startActivity(intent)


def _trigger_install(uri):
    """Dispatch ACTION_VIEW with the APK MIME type so the system
    installer takes over. Caller has already verified
    ``canRequestPackageInstalls()``."""
    from jnius import autoclass
    Intent = autoclass('android.content.Intent')
    PythonActivity = autoclass('org.kivy.android.PythonActivity')
    activity = PythonActivity.mActivity
    intent = Intent(Intent.ACTION_VIEW)
    intent.setDataAndType(uri, 'application/vnd.android.package-archive')
    intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
                    | Intent.FLAG_ACTIVITY_NEW_TASK)
    activity.startActivity(intent)


# ── Pre-install validation: APK parse + signature compare ─────────────────
#
# When the install fails with "App not installed as package appears to be
# invalid" or "You can't install this app on your device", Android's
# message is unhelpful. The two most common causes we can detect from the
# Python side BEFORE firing the Intent:
#
# 1. The downloaded APK is corrupted or malformed —
#    ``getPackageArchiveInfo`` returns null.
# 2. The downloaded APK is signed with a different keystore than the
#    currently-installed app of the same package — Android refuses to
#    overwrite, surfacing the "package appears to be invalid" message.
#    Common in dev workflows where the upstream release uses one key and
#    a local rebuild uses another (or in fork situations).
#
# Both checks use ``PackageManager.GET_SIGNATURES`` (deprecated since API
# 28 in favor of ``GET_SIGNING_CERTIFICATES``) because GET_SIGNATURES is
# universally available on our minSdk 26+ floor; GET_SIGNING_CERTIFICATES
# would be cleaner but adds a branch we don't need yet.

_PM_GET_SIGNATURES = 64  # PackageManager.GET_SIGNATURES


def _android_package_manager():
    """Return ``(pm, ctx)`` or ``(None, None)`` if pyjnius / Activity
    aren't available."""
    try:
        from jnius import autoclass
    except ImportError:
        return None, None
    try:
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        ctx = PythonActivity.mActivity
        if ctx is None:
            try:
                PythonService = autoclass('org.kivy.android.PythonService')
                ctx = PythonService.mService
            except Exception:
                return None, None
        if ctx is None:
            return None, None
        return ctx.getPackageManager(), ctx
    except Exception:
        return None, None


def _installed_version_name(package_name):
    """Return the device-installed APK's ``versionName`` for
    ``package_name``, or ``''`` if not installed / not Android.

    Distinct from the running process's ``__version__``: the running
    code is whatever Python loaded from disk at process start. If the
    APK on disk has been replaced since (rare; only happens during a
    self-update install) or the Python source was hot-patched without
    a rebuild (dev workflow), they can diverge — so logging both lets
    the next "why is Update checking against the wrong version?"
    report be answered from the trace alone."""
    pm, _ctx = _android_package_manager()
    if pm is None:
        return ''
    try:
        info = pm.getPackageInfo(package_name, 0)
        return info.versionName or ''
    except Exception:
        return ''


def _apk_parse_info(apk_path):
    """Return ``PackageInfo`` for the APK at ``apk_path`` with
    GET_SIGNATURES requested, or ``None`` if the APK can't be
    parsed (corrupted / truncated / wrong magic) or we're off
    Android."""
    pm, _ctx = _android_package_manager()
    if pm is None:
        return None
    try:
        return pm.getPackageArchiveInfo(apk_path, _PM_GET_SIGNATURES)
    except Exception:
        return None


def default_asset_filename():
    """Derive the release-asset filename from the running peer's own
    Android package name. Returns ``'<package-segment>.apk'`` (e.g.
    ``'aztrecorder.apk'`` for ``org.atoznback.aztrecorder``), or
    ``''`` off Android.

    Suite convention is that every APK's release asset is named after
    its ``buildozer.spec → package.name`` (= the Android package's
    last segment = the short-form name in the suite naming table).
    Hardcoding the name in each peer drifted (the recorder shipped
    with ``'azt_recorder.apk'`` for several releases while the
    actual published asset was ``'aztrecorder.apk'`` — Python-pkg
    underscore vs. Android-segment no-underscore — and self-update
    404'd until the user reinstalled manually). Derive at runtime
    instead: ``activity.getPackageName().rsplit('.', 1)[-1] +
    '.apk'`` is what every suite peer wants, with no per-peer
    boilerplate. Forks that publish under a different scheme pass
    ``asset_filename=`` explicitly to override.
    """
    _pm, ctx = _android_package_manager()
    if ctx is None:
        return ''
    try:
        pkg = ctx.getPackageName() or ''
    except Exception:
        return ''
    if not pkg:
        return ''
    return f'{pkg.rsplit(".", 1)[-1]}.apk'


def _signature_matches_installed(apk_path, package_name):
    """Compare the signing certificate of ``apk_path`` against the
    installed app's certificate for ``package_name``. Returns:

    - ``True`` if signatures match (install will succeed signature-wise).
    - ``False`` if signatures differ (install will fail with the
      "App not installed as package appears to be invalid" Android
      error).
    - ``None`` if we can't determine — APK unparseable, no current
      install (so signature mismatch isn't a concern; first-install
      path), pyjnius unavailable, or any unexpected exception."""
    pm, _ctx = _android_package_manager()
    if pm is None:
        return None
    try:
        archive_info = pm.getPackageArchiveInfo(
            apk_path, _PM_GET_SIGNATURES)
        if archive_info is None:
            return None  # APK unparseable
        try:
            installed_info = pm.getPackageInfo(
                package_name, _PM_GET_SIGNATURES)
        except Exception:
            return None  # not installed → fresh install, no clash
        archive_sigs = archive_info.signatures or []
        installed_sigs = installed_info.signatures or []
        if not archive_sigs or not installed_sigs:
            return None
        # Apps typically have one signature; compare the canonical
        # toCharsString() for an exact byte-for-byte match.
        return (archive_sigs[0].toCharsString()
                == installed_sigs[0].toCharsString())
    except Exception:
        return None


def check_for_update(*, repo, current_version, asset_filename=None,
                     on_status, on_no_update=None, on_error=None,
                     download_dir=None, install_target_package=None,
                     on_install_complete=None,
                     on_user_action_needed=None,
                     install_label=None):
    """Check GitHub for a newer release of ``asset_filename`` in
    ``repo``; download and trigger Android's installer if found.

    Spawns a worker thread and returns immediately. All callbacks are
    marshaled to the Kivy UI thread, so hosts may update labels /
    progress bars directly without their own threading.

    Parameters
    ----------
    repo : str
        ``'owner/repo'`` on GitHub (e.g. ``'kent-rasmussen/azt-collab'``).
    current_version : str
        The caller's running ``__version__`` (compared as a semver tuple
        against the release's ``tag_name``).
    asset_filename : str | None
        Exact name of the release asset to fetch. When ``None``
        (default), derived from the running Android package's last
        segment via ``default_asset_filename()`` — i.e.
        ``'aztrecorder.apk'`` for ``org.atoznback.aztrecorder``.
        Pass explicitly to override (e.g. a fork that publishes
        under a different naming scheme).
    on_status : callable(str)
        Called repeatedly with translated, user-visible state strings:
        "Checking for updates…", "Downloading {pct}%…", "Installing…".
    on_no_update : callable() | None
        Called when ``current_version >= latest`` so the host can show
        "Up to date." or similar.
    on_error : callable(str) | None
        Failure surface: network errors, missing asset, install refused,
        non-Android host.
    download_dir : str | None
        Where to stage the downloaded APK before handing it to
        MediaStore. Defaults to ``$AZT_HOME/updates``.
    install_target_package : str | None
        Android package name to poll for install completion (e.g.
        ``'org.atoznback.aztcollab'`` when a peer is installing the
        server APK). If set, after dispatching the install intent we
        poll ``PackageManager.getPackageInfo`` every
        ``_INSTALL_POLL_INTERVAL_S`` for up to
        ``_INSTALL_POLL_TIMEOUT_S``, firing ``on_status('Installed.')``
        when the version flips to the freshly-downloaded one and
        ``on_status('Install pending. Reopen this app when finished.')``
        on timeout. **Set this only for cross-package installs** —
        a self-update kills the running peer process during install,
        so polling its own package would block forever. Pass ``None``
        (default) for self-installs.
    on_install_complete : callable() | None
        Optional callback fired by the polling watchdog when the
        target package's version flips to the just-downloaded
        version (i.e. install actually succeeded). Doesn't fire on
        the timeout branch — only on confirmed completion. Used by
        ``install_server_apk_popup`` to chain into bootstrap's
        compat-recheck flow so the host can resume normal startup
        without a manual relaunch.
    """
    try:
        from kivy.utils import platform
    except Exception:
        platform = ''
    if platform != 'android':
        _safe_call(on_error,
                   _tr('APK install is only available on Android.'))
        return

    if download_dir is None:
        download_dir = os.path.join(azt_home(), 'updates')

    # Resolve asset_filename now, on the calling (UI) thread — needs
    # an Android Activity, which is available here but might not be
    # later. Pre-platform-gate this fallback is a no-op (returns '');
    # we're already inside the ``platform == 'android'`` branch, so a
    # blank result from default_asset_filename means the Activity
    # genuinely couldn't be reached, which we surface to the caller.
    resolved_asset = asset_filename or default_asset_filename()
    if not resolved_asset:
        _safe_call(on_error, _tr(
            'Update check failed: could not derive asset filename '
            '(running Activity unreachable). Pass asset_filename='
            ' explicitly.'))
        return

    def _ui_status(msg):
        _on_ui(_safe_call, on_status, msg)

    def _ui_error(msg):
        _on_ui(_safe_call, on_error, msg)

    def _ui_no_update():
        _on_ui(_safe_call, on_no_update)

    def _worker():
        # Allow the resilient-asset-name fallback below to rebind
        # ``resolved_asset`` so the cache path + MediaStore display
        # name use the actually-found filename.
        nonlocal resolved_asset
        _ui_status(_tr('Checking for updates…'))
        try:
            release = _fetch_latest(repo)
        except Exception as ex:
            _ui_error(_tr('Update check failed: {error}').format(error=ex))
            return

        latest = (release.get('tag_name') or '').lstrip('vV')
        if not latest:
            _ui_error(_tr('Update check failed: no tag in latest release'))
            return
        if _version_tuple(latest) <= _version_tuple(current_version):
            _ui_no_update()
            return

        asset = _pick_asset(release, resolved_asset)
        if asset is None:
            # Resilient fallback: if the caller passed an explicit
            # ``asset_filename`` literal that doesn't match the
            # actually-published asset (real bug: peer ``main.py``
            # pinned Python-pkg ``azt_recorder.apk`` while the
            # release shipped Android-segment ``aztrecorder.apk``),
            # try the runtime-derived name. Survives peer-side
            # literal drift without requiring a peer rebuild.
            derived = default_asset_filename()
            if derived and derived != resolved_asset:
                asset = _pick_asset(release, derived)
                if asset is not None:
                    print(f'[update] explicit asset {resolved_asset!r} '
                          f'not in release {latest!r}; falling back '
                          f'to derived {derived!r}',
                          file=sys.stderr, flush=True)
                    resolved_asset = derived
        if asset is None:
            _ui_error(_tr(
                'Update check failed: no {file} in release {tag}'
            ).format(file=resolved_asset, tag=latest))
            return

        download_url = asset.get('browser_download_url') or ''
        size = int(asset.get('size') or 0)
        # GitHub's REST API exposes an authoritative ``digest`` on
        # release assets since 2025-06. Format: ``sha256:<hex>``.
        # Older assets (pre-rollout) may have ``null``; we fall back
        # to the sidecar self-consistency check in that case.
        digest_field = asset.get('digest') or ''
        expected_sha = ''
        if digest_field.startswith('sha256:'):
            expected_sha = digest_field[len('sha256:'):].strip()
        if not download_url:
            _ui_error(_tr('Update check failed: asset has no download URL'))
            return

        try:
            os.makedirs(download_dir, exist_ok=True)
        except OSError as ex:
            _ui_error(_tr('Could not create download dir: {error}')
                      .format(error=ex))
            return
        dest = os.path.join(download_dir, resolved_asset)

        # Cache-staleness check. Two layers:
        #
        # 1. SHA against GitHub's authoritative digest (when the
        #    asset has one). Catches re-uploaded assets, corrupted
        #    cache, and the "previous Update cycle left an older
        #    version's APK behind" scenario in one go — if the
        #    cached bytes don't hash to what GitHub serves *right
        #    now*, we re-download. Subsumes most of what the
        #    versionName check below catches when the digest is
        #    available.
        # 2. versionName fallback. For legacy assets without a
        #    digest, ``_has_fresh_download`` falls back to the
        #    sidecar (self-consistency only — bytes match what we
        #    saved, but can't tell if GitHub is now serving
        #    something different). The versionName comparison
        #    covers the "stale cache, no digest available" case so
        #    we don't accidentally re-install an older version.
        if _has_fresh_download(dest, expected_sha256=expected_sha):
            cached_info = _apk_parse_info(dest)
            cached_version = ''
            if cached_info is not None:
                try:
                    cached_version = cached_info.versionName or ''
                except Exception:
                    cached_version = ''
            if cached_version and cached_version != latest:
                print(f'[update] cache stale: cached_version='
                      f'{cached_version!r} != latest={latest!r}; '
                      f'discarding {dest}',
                      file=sys.stderr, flush=True)
                try:
                    os.remove(dest)
                except OSError:
                    pass
                try:
                    os.remove(dest + '.sha256')
                except OSError:
                    pass

        if _has_fresh_download(dest, expected_sha256=expected_sha):
            _ui_status(_tr('Using already-downloaded file…'))
        else:
            def _on_progress(pct):
                _ui_status(_tr('Downloading {pct}%…').format(pct=pct))

            def _on_retry_status(msg):
                # _download surfaces "retrying in Ns…" between attempts;
                # marshal back to the UI thread the same way regular
                # status updates do.
                _ui_status(msg)

            try:
                _download(download_url, dest, size, _on_progress,
                          on_status=_on_retry_status)
            except Exception as ex:
                _ui_error(
                    _tr('Download failed: {error}').format(error=ex)
                    + '\n' + _wrappable_url(download_url))
                return
            _save_download_sha(dest)

        _ui_status(_tr('Preparing install…'))

        # Pre-install validation. ``check_pkg`` is the package whose
        # signature must match — install_target_package for cross-
        # package installs (peer pushing the server APK), our own
        # for self-updates. Diagnostic line lets us see in logcat
        # whether the device's installed version diverges from the
        # running code's ``current_version`` (rare but useful when
        # diagnosing "Update keeps checking against the wrong
        # version").
        check_pkg = install_target_package or ''
        if not check_pkg:
            try:
                from jnius import autoclass
                PythonActivity = autoclass(
                    'org.kivy.android.PythonActivity')
                _act = PythonActivity.mActivity
                check_pkg = _act.getPackageName() if _act else ''
            except Exception:
                check_pkg = ''
        installed_v = _installed_version_name(check_pkg) if check_pkg else ''
        print(f'[update] pre-install check: pkg={check_pkg!r} '
              f'installed_version={installed_v!r} '
              f'running_version={current_version!r} '
              f'latest={latest!r}',
              file=sys.stderr, flush=True)

        archive_info = _apk_parse_info(dest)
        if archive_info is None:
            print(f'[update] parse_info: None path={dest}',
                  file=sys.stderr, flush=True)
        else:
            try:
                _pkg = archive_info.packageName
                _ver = archive_info.versionName
            except Exception:
                _pkg = '?'
                _ver = '?'
            print(f'[update] parse_info: ok pkg={_pkg!r} '
                  f'versionName={_ver!r} path={dest}',
                  file=sys.stderr, flush=True)
        if archive_info is None:
            _ui_error(_tr(
                'Downloaded APK could not be parsed. Try Update '
                'again to re-download.'))
            try:
                # Toss the cached file + sidecar so the next attempt
                # actually re-downloads instead of reusing the same
                # broken bytes.
                os.remove(dest)
            except OSError:
                pass
            sidecar = dest + '.sha256'
            try:
                os.remove(sidecar)
            except OSError:
                pass
            return
        if check_pkg:
            sig_ok = _signature_matches_installed(dest, check_pkg)
            print(f'[update] signature_matches_installed: {sig_ok!r} '
                  f'(True=match / False=mismatch / None=unknown)',
                  file=sys.stderr, flush=True)
            if sig_ok is False:
                _ui_error(_tr(
                    "Downloaded APK is signed with a different key "
                    "than the installed app. Android won't replace "
                    "the install. Uninstall the current version "
                    "first, then tap Update again — or rebuild from "
                    "source with the matching keystore."))
                return

        # MediaStore insert + install intent must run on the UI thread:
        # PythonActivity.mActivity touches the main Looper, and starting
        # an Activity from a worker thread is rejected by Android.
        def _install_on_ui(_dt):
            try:
                from jnius import autoclass
                PythonActivity = autoclass(
                    'org.kivy.android.PythonActivity')
                activity = PythonActivity.mActivity
                if not _can_install_packages(activity):
                    label = install_label or _tr('Update')
                    _safe_call(on_status, _tr(
                        'Allow "Install unknown apps" for this app, '
                        'then tap {label} again.'
                    ).format(label=label))
                    _open_unknown_sources_settings(activity)
                    _safe_call(on_user_action_needed)
                    return
                uri = _media_store_uri(dest, resolved_asset)
                _trigger_install(uri)
                _safe_call(on_status, _tr('Installing…'))
                if install_target_package:
                    _start_install_poll(install_target_package, latest,
                                        on_status, on_install_complete)
            except Exception as ex:
                _safe_call(on_error,
                           _tr('Install failed: {error}').format(error=ex))

        Clock.schedule_once(_install_on_ui, 0)

    threading.Thread(target=_worker, daemon=True).start()


def install_apk_from_url(*, url, asset_filename=None, on_status,
                         on_error=None, on_install_complete=None,
                         on_user_action_needed=None,
                         install_target_package=None,
                         install_label=None,
                         download_dir=None,
                         repo=None):
    """Direct-URL install path. Skips the GitHub API for *download* —
    just GETs the URL, hands the bytes to Android's installer,
    optionally polls for install completion via change-detection.

    ``repo`` (optional, ``'owner/repo'``) opts in to a one-call
    GitHub release lookup for **cache freshness** only — the
    download itself still uses the supplied direct URL. Without
    it, ``_has_fresh_download`` runs in sidecar mode (cached SHA
    matches what we recorded post-download), which can't tell
    "the bytes are intact" from "the bytes are intact but two
    versions stale." With it, we fetch the release JSON, find the
    asset's ``digest`` (sha256:hex, populated since 2025-06), and
    compare against the cached file's SHA. Mismatch → drop cache
    and re-download. Caller gets fresh bytes against the URL it
    already trusted to be the canonical download.

    Use for stable redirect URLs like
    ``https://github.com/<owner>/<repo>/releases/latest/download/<asset>``
    where you don't need version comparison and the API path's
    "fetch JSON, walk releases, pick asset by name, retry on 404"
    machinery is overkill. ``check_for_update`` is for the
    self-update case where version comparison matters; this is for
    the popup's "install the missing service" case where the user
    just wants the latest stable APK installed.

    Same callback shape as ``check_for_update`` minus
    ``on_no_update`` (this path always installs) and minus the
    ``current_version`` parameter (no comparison).

    Spawns a worker thread; callbacks marshal back to the Kivy UI
    thread so hosts may update labels directly. Android-only — non-
    Android hosts get a translated "Android only" through
    ``on_error``."""
    try:
        from kivy.utils import platform
    except Exception:
        platform = ''
    if platform != 'android':
        _safe_call(on_error,
                   _tr('APK install is only available on Android.'))
        return

    if download_dir is None:
        download_dir = os.path.join(azt_home(), 'updates')

    # See check_for_update for rationale: derive on the UI thread so
    # the Activity reference is reliably reachable, error out fast if
    # the caller didn't pass a name and we couldn't resolve one.
    resolved_asset = asset_filename or default_asset_filename()
    if not resolved_asset:
        _safe_call(on_error, _tr(
            'Install failed: could not derive asset filename '
            '(running Activity unreachable). Pass asset_filename='
            ' explicitly.'))
        return

    def _ui_status(msg):
        _on_ui(_safe_call, on_status, msg)

    def _ui_error(msg):
        _on_ui(_safe_call, on_error, msg)

    def _worker():
        # ``nonlocal`` lets the resilient-asset-name fallback below
        # rebind both names — the resolved filename (used for cache
        # path + MediaStore display) and the download URL (which may
        # need to be replaced by the release JSON's authoritative
        # browser_download_url when the caller-baked URL points at
        # the wrong asset name).
        nonlocal resolved_asset
        download_url = url
        try:
            os.makedirs(download_dir, exist_ok=True)
        except OSError as ex:
            _ui_error(_tr('Could not create download dir: {error}')
                      .format(error=ex))
            return

        # When ``repo`` is supplied, the release JSON is the
        # authoritative source for BOTH the digest (cache freshness)
        # AND the download URL (resilience). Resilience matters
        # because the caller-supplied ``url`` is typically
        # ``releases/latest/download/<name>`` constructed by
        # ``bootstrap.py`` from a peer-baked literal — and that
        # literal historically drifted from the published asset
        # name (Python-pkg ``azt_recorder.apk`` vs. published
        # ``aztrecorder.apk``). If the explicit name isn't found in
        # the release, fall back to the runtime-derived name; the
        # client is the right place to know the convention since the
        # peer's only knowledge is its own
        # ``activity.getPackageName()``.
        expected_sha = ''
        if repo:
            try:
                rel = _fetch_latest(repo)
                asset = _pick_asset(rel, resolved_asset)
                if asset is None:
                    derived = default_asset_filename()
                    if derived and derived != resolved_asset:
                        asset = _pick_asset(rel, derived)
                        if asset is not None:
                            print(f'[update] install: explicit asset '
                                  f'{resolved_asset!r} not in release; '
                                  f'falling back to derived '
                                  f'{derived!r}', file=sys.stderr,
                                  flush=True)
                            resolved_asset = derived
                if asset is not None:
                    # Use the API-provided URL — survives wrong-name
                    # literals in the caller-baked
                    # ``releases/latest/download/<name>`` URL.
                    api_url = asset.get('browser_download_url') or ''
                    if api_url:
                        download_url = api_url
                    digest = asset.get('digest') or ''
                    if digest.startswith('sha256:'):
                        expected_sha = digest[len('sha256:'):].strip()
            except Exception as ex:
                # Don't fail the install just because the metadata
                # call hiccupped — fall through to sidecar mode.
                # User experience is "cache might be stale" vs
                # "Install button doesn't work at all", and the
                # signature / parse checks still defend the install
                # against a corrupted cache.
                print(f'[update] install_apk_from_url: release JSON '
                      f'fetch failed for {repo!r}: {ex}',
                      file=sys.stderr, flush=True)

        dest = os.path.join(download_dir, resolved_asset)

        # If we just downloaded this file (typically: user tapped
        # Install, finished download, granted "Install unknown
        # apps", came back, tapped Install again), skip the
        # download and go straight to the install Intent. Save the
        # user 10–30s of waiting for an identical re-download.
        if _has_fresh_download(dest, expected_sha256=expected_sha):
            _ui_status(_tr('Using already-downloaded file…'))
        else:
            _ui_status(_tr('Downloading…'))

            def _on_progress(pct):
                _ui_status(_tr('Downloading {pct}%…').format(pct=pct))

            def _on_retry_status(msg):
                _ui_status(msg)

            try:
                _download(download_url, dest, 0, _on_progress,
                          on_status=_on_retry_status)
            except Exception as ex:
                _ui_error(
                    _tr('Download failed: {error}').format(error=ex)
                    + '\n' + _wrappable_url(download_url))
                return
            _save_download_sha(dest)

        _ui_status(_tr('Preparing install…'))

        # Pre-install validation. Same parse + signature check as
        # check_for_update, see helper docstrings for rationale.
        check_pkg = install_target_package or ''
        archive_info = _apk_parse_info(dest)
        if archive_info is None:
            _ui_error(_tr(
                'Downloaded APK could not be parsed. Try Install '
                'again to re-download.'))
            try:
                os.remove(dest)
            except OSError:
                pass
            sidecar = dest + '.sha256'
            try:
                os.remove(sidecar)
            except OSError:
                pass
            return
        if check_pkg:
            sig_ok = _signature_matches_installed(dest, check_pkg)
            if sig_ok is False:
                _ui_error(_tr(
                    "Downloaded APK is signed with a different key "
                    "than the installed app. Android won't replace "
                    "the install. Uninstall the current version "
                    "first, then tap Install again — or rebuild "
                    "from source with the matching keystore."))
                return

        def _install_on_ui(_dt):
            try:
                from jnius import autoclass
                PythonActivity = autoclass(
                    'org.kivy.android.PythonActivity')
                activity = PythonActivity.mActivity
                if not _can_install_packages(activity):
                    label = install_label or _tr('Install')
                    _safe_call(on_status, _tr(
                        'Allow "Install unknown apps" for this app, '
                        'then tap {label} again.'
                    ).format(label=label))
                    _open_unknown_sources_settings(activity)
                    # Tell the caller the install path stalled on
                    # user action — popup uses this to re-enable
                    # buttons so the user can retry after granting
                    # the permission. Without this the caller is
                    # left with disabled buttons forever.
                    _safe_call(on_user_action_needed)
                    return
                uri = _media_store_uri(dest, resolved_asset)
                _trigger_install(uri)
                _safe_call(on_status, _tr('Installing…'))
                if install_target_package:
                    # Change-detection mode: we don't know the
                    # version we just downloaded.
                    _start_install_poll(install_target_package, '',
                                        on_status, on_install_complete)
            except Exception as ex:
                _safe_call(on_error,
                           _tr('Install failed: {error}').format(error=ex))

        Clock.schedule_once(_install_on_ui, 0)

    threading.Thread(target=_worker, daemon=True).start()


def _start_install_poll(package_name, expected_version, on_status,
                        on_install_complete=None):
    """Poll Android's PackageManager for ``package_name``'s
    ``versionName`` to flip. Two modes, picked by
    ``expected_version``:

    - **Pinned-version mode** (``expected_version`` truthy): poll
      for ``installed >= expected_version``. Used by
      ``check_for_update`` when the GitHub API path knows what
      version it just downloaded.
    - **Change-detection mode** (``expected_version`` empty / None):
      snapshot the current installed versionName at start, then
      poll for any change. Used by ``install_apk_from_url`` where
      we downloaded a stable redirect URL and don't have version
      metadata. Trivially detects "package became installed" when
      the snapshot is None (uninstalled → installed).

    Cancels itself on match or timeout; status updates flow
    through ``on_status``. Best-effort observation: while the peer
    is in the background (system installer Activity in foreground),
    Kivy's Clock pauses and polls don't fire. They resume on
    foreground — which is fine, since the peer can also detect
    completion on its next bootstrap() invocation in a fresh
    process. This watchdog mostly catches the case where the user
    stayed on the peer through a quick install or canceled and we
    want to surface that the install never landed.

    On confirmed completion (versionName flipped) we fire
    ``on_install_complete`` if given — letting the popup auto-
    dismiss and the host re-run its compat check without requiring
    the user to relaunch."""

    def _read_installed():
        try:
            from jnius import autoclass
            PythonActivity = autoclass(
                'org.kivy.android.PythonActivity')
            pm = PythonActivity.mActivity.getPackageManager()
            info = pm.getPackageInfo(package_name, 0)
            return info.versionName or ''
        except Exception:
            # Package not installed yet or PM transient.
            return None

    initial = _read_installed() if not expected_version else None
    started_at = time.time()
    last = {'tick': 0}

    def _tick(_dt):
        if (time.time() - started_at) >= _INSTALL_POLL_TIMEOUT_S:
            _safe_call(on_status, _tr(
                'Install pending. Reopen this app when finished.'))
            return False  # unschedule
        installed = _read_installed()
        if expected_version:
            # Pinned-version mode.
            done = (installed is not None and
                    installed.lstrip('vV') and
                    _version_tuple(installed.lstrip('vV'))
                    >= _version_tuple(expected_version))
        else:
            # Change-detection mode.
            done = installed is not None and installed != initial
        if done:
            _safe_call(on_status, _tr('Installed.'))
            _safe_call(on_install_complete)
            return False  # unschedule
        last['tick'] = time.time()
        return True  # keep going

    Clock.schedule_interval(_tick, _INSTALL_POLL_INTERVAL_S)
