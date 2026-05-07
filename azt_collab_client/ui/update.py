"""Self-update flow for suite APKs.

Reusable across the server APK and every peer (recorder, viewer, …).
Each host wires a small adapter that supplies three pieces of identity:

    repo             — 'owner/repo' on GitHub (releases endpoint)
    current_version  — caller's running ``__version__`` string
    asset_filename   — the release asset to fetch (e.g. ``azt_collab.apk``,
                       ``azt_recorder.apk``); each app names its own.

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


def _has_fresh_download(path):
    """True iff ``path`` exists AND its SHA-256 matches the digest
    stored in ``<path>.sha256``. Used by the install workers to
    skip a redundant re-download when the user comes back from
    Android's "Install unknown apps" settings and taps Install
    again — works regardless of how long the detour took.

    Definitive integrity check (replaces the mtime-window
    heuristic in 0.28.25): if the file on disk hashes to the
    same value we recorded right after download, it's the same
    file we'd have downloaded again. ``_download`` writes to
    ``<path>.part`` and renames on success, so a present
    ``<path>`` is always a complete download — no partial-file
    salvage logic needed."""
    sidecar = path + '.sha256'
    if not (os.path.exists(path) and os.path.exists(sidecar)):
        return False
    try:
        with open(sidecar) as f:
            stored = f.read().strip()
    except OSError:
        return False
    if not stored:
        return False
    return _sha256(path) == stored


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


def check_for_update(*, repo, current_version, asset_filename,
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
    asset_filename : str
        Exact name of the release asset to fetch. Each suite app names
        its own (``aztcollab.apk``, ``azt_recorder.apk``, …).
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

    def _ui_status(msg):
        _on_ui(_safe_call, on_status, msg)

    def _ui_error(msg):
        _on_ui(_safe_call, on_error, msg)

    def _ui_no_update():
        _on_ui(_safe_call, on_no_update)

    def _worker():
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

        asset = _pick_asset(release, asset_filename)
        if asset is None:
            _ui_error(_tr(
                'Update check failed: no {file} in release {tag}'
            ).format(file=asset_filename, tag=latest))
            return

        download_url = asset.get('browser_download_url') or ''
        size = int(asset.get('size') or 0)
        if not download_url:
            _ui_error(_tr('Update check failed: asset has no download URL'))
            return

        try:
            os.makedirs(download_dir, exist_ok=True)
        except OSError as ex:
            _ui_error(_tr('Could not create download dir: {error}')
                      .format(error=ex))
            return
        dest = os.path.join(download_dir, asset_filename)

        # Reuse a recent download (e.g. user came back from
        # granting "Install unknown apps") instead of paying for
        # the same bytes twice.
        if _has_fresh_download(dest):
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
                uri = _media_store_uri(dest, asset_filename)
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


def install_apk_from_url(*, url, asset_filename, on_status,
                         on_error=None, on_install_complete=None,
                         on_user_action_needed=None,
                         install_target_package=None,
                         install_label=None,
                         download_dir=None):
    """Direct-URL install path. Skips the GitHub API entirely —
    just GETs the URL, hands the bytes to Android's installer,
    optionally polls for install completion via change-detection.

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

    def _ui_status(msg):
        _on_ui(_safe_call, on_status, msg)

    def _ui_error(msg):
        _on_ui(_safe_call, on_error, msg)

    def _worker():
        try:
            os.makedirs(download_dir, exist_ok=True)
        except OSError as ex:
            _ui_error(_tr('Could not create download dir: {error}')
                      .format(error=ex))
            return
        dest = os.path.join(download_dir, asset_filename)

        # If we just downloaded this file (typically: user tapped
        # Install, finished download, granted "Install unknown
        # apps", came back, tapped Install again), skip the
        # download and go straight to the install Intent. Save the
        # user 10–30s of waiting for an identical re-download.
        if _has_fresh_download(dest):
            _ui_status(_tr('Using already-downloaded file…'))
        else:
            _ui_status(_tr('Downloading…'))

            def _on_progress(pct):
                _ui_status(_tr('Downloading {pct}%…').format(pct=pct))

            def _on_retry_status(msg):
                _ui_status(msg)

            try:
                _download(url, dest, 0, _on_progress,
                          on_status=_on_retry_status)
            except Exception as ex:
                _ui_error(
                    _tr('Download failed: {error}').format(error=ex)
                    + '\n' + _wrappable_url(url))
                return
            _save_download_sha(dest)

        _ui_status(_tr('Preparing install…'))

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
                uri = _media_store_uri(dest, asset_filename)
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
