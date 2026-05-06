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
import urllib.request

from kivy.clock import Clock

from .. import _version_tuple
from ..paths import azt_home
from ..translate import tr as _tr


_GITHUB_API = 'https://api.github.com'
_USER_AGENT = 'azt-collab-updater/1'
_DOWNLOAD_CHUNK = 65536


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
    default" — peers don't opt users into betas without intent."""
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
        return _fetch_latest_singleton(repo)
    for rel in releases or []:
        if not rel.get('prerelease') and not rel.get('draft'):
            return rel
    # Every recent release is a prerelease — fall back to the
    # canonical latest endpoint, which will give us *something* if
    # one exists, even if it's a prerelease (better to surface that
    # than silently install nothing).
    return _fetch_latest_singleton(repo)


def _fetch_latest_singleton(repo):
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


def _download(url, dest, total_bytes, on_progress):
    """Stream ``url`` into ``dest``. Calls ``on_progress(pct)`` from
    the worker thread roughly every 64 KB; caller marshals to UI."""
    req = urllib.request.Request(url, headers={'User-Agent': _USER_AGENT})
    tmp = dest + '.part'
    received = 0
    with urllib.request.urlopen(req, timeout=60) as resp, \
            open(tmp, 'wb') as out:
        while True:
            chunk = resp.read(_DOWNLOAD_CHUNK)
            if not chunk:
                break
            out.write(chunk)
            received += len(chunk)
            if total_bytes and on_progress is not None:
                on_progress(int(received * 100 / total_bytes))
    os.replace(tmp, dest)


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
                     download_dir=None):
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
        its own (``azt_collab.apk``, ``azt_recorder.apk``, …).
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

        def _on_progress(pct):
            _ui_status(_tr('Downloading {pct}%…').format(pct=pct))

        try:
            _download(download_url, dest, size, _on_progress)
        except Exception as ex:
            _ui_error(_tr('Download failed: {error}').format(error=ex))
            return

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
                    _safe_call(on_status, _tr(
                        'Allow "Install unknown apps" for this app, '
                        'then tap Update again.'))
                    _open_unknown_sources_settings(activity)
                    return
                uri = _media_store_uri(dest, asset_filename)
                _trigger_install(uri)
                _safe_call(on_status, _tr('Installing…'))
            except Exception as ex:
                _safe_call(on_error,
                           _tr('Install failed: {error}').format(error=ex))

        Clock.schedule_once(_install_on_ui, 0)

    threading.Thread(target=_worker, daemon=True).start()
