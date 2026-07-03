"""Share helpers — used by every peer + the daemon UI.

Three entry points:

- ``share_running_apk`` — share the host APK's own .apk file via
  MediaStore + ``Intent.ACTION_SEND``.
- ``share_text`` — share a string (diagnostic log, status dump,
  shareable URL, etc.) via ``Intent.ACTION_SEND`` with
  ``EXTRA_TEXT``.
- ``email_text`` — open the user's email app pre-filled with
  recipient / subject / body via ``Intent.ACTION_SENDTO`` and a
  ``mailto:`` URI.

All three are Android-only — non-Android platforms invoke
``on_error`` with a translated message so the host can surface it
in its usual error channel. Lives in the client UI package so
every peer (recorder, viewer, future sister apps) gets these
without each one re-deriving the JNI dance and re-translating
the error strings.
"""

from ..translate import tr as _tr


def open_url(url, on_error=None):
    """Open *url* in the device's browser via ``ACTION_VIEW``.

    The fallback for the GitHub repo-invitation flow (0.52.24): when the
    daemon can't auto-accept an invitation (none pending yet, or the app
    token can't accept it) it emits ``REPO_NO_ACCESS`` carrying the repo
    ``url``; the peer offers this to send the user to the repo /
    invitations page to accept or request access. Returns True if the
    intent was dispatched. Non-Android → calls ``on_error`` with the link
    text so a desktop host can show it. Best-effort; never raises."""
    try:
        from kivy.utils import platform
    except Exception:
        platform = ''
    if not url:
        return False
    if platform != 'android':
        if on_error is not None:
            on_error(_tr('Open this link on the device: {url}').format(
                url=url))
        return False
    try:
        from jnius import autoclass
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        Intent = autoclass('android.content.Intent')
        Uri = autoclass('android.net.Uri')
        activity = PythonActivity.mActivity
        intent = Intent(Intent.ACTION_VIEW, Uri.parse(url))
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        activity.startActivity(intent)
        return True
    except Exception as ex:
        print(f'[open_url] failed: {ex!r}')
        if on_error is not None:
            on_error(_tr('Could not open the browser. Link: {url}').format(
                url=url))
        return False


def share_running_apk(filename=None, on_error=None):
    """Share the running APK via Android's share sheet.

    Parameters
    ----------
    filename : str | None
        MediaStore display name for the shared file. When ``None``
        (default), derived from the running Android package's last
        segment via
        ``azt_collab_client.ui.update.default_asset_filename`` —
        e.g. ``'aztrecorder.apk'`` for ``org.atoznback.aztrecorder``.
        Pass explicitly to override for a fork or test harness.
    on_error : callable(str) | None
        Invoked with a translated, user-visible message on any
        failure (non-Android, MediaStore insert refused, copy or
        intent dispatch raised). Hosts typically pass their popup /
        toast helper.
    """
    try:
        from kivy.utils import platform
    except Exception:
        platform = ''
    if platform != 'android':
        if on_error is not None:
            on_error(_tr('APK sharing is only available on Android.'))
        return

    if not filename:
        from .update import default_asset_filename
        filename = default_asset_filename() or 'app.apk'

    try:
        from jnius import autoclass, cast
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        Intent = autoclass('android.content.Intent')
        ContentValues = autoclass('android.content.ContentValues')
        MediaStoreDownloads = autoclass(
            'android.provider.MediaStore$Downloads')
        activity = PythonActivity.mActivity
        context = cast('android.content.Context', activity)
        pm = context.getPackageManager()
        app_info = pm.getApplicationInfo(context.getPackageName(), 0)
        apk_path = app_info.sourceDir

        values = ContentValues()
        values.put('_display_name', filename)
        values.put('mime_type',
                   'application/vnd.android.package-archive')
        resolver = context.getContentResolver()
        uri = resolver.insert(
            MediaStoreDownloads.EXTERNAL_CONTENT_URI, values)
        if not uri:
            if on_error is not None:
                on_error(_tr(
                    'Share failed: could not create MediaStore entry'))
            return

        fos = resolver.openOutputStream(uri)
        with open(apk_path, 'rb') as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                fos.write(chunk)
        fos.close()

        intent = Intent(Intent.ACTION_SEND)
        intent.setType('application/vnd.android.package-archive')
        intent.putExtra(Intent.EXTRA_STREAM,
                        cast('android.os.Parcelable', uri))
        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        chooser = Intent.createChooser(
            intent,
            autoclass('java.lang.String')(_tr('Share app')))
        activity.startActivity(chooser)
    except Exception as ex:
        print(f'Share APK error: {ex}')
        if on_error is not None:
            on_error(_tr('Could not share APK:\n{error}').format(error=ex))


def share_text(text, subject='', chooser_title='', on_error=None):
    """Share *text* through Android's share sheet via
    ``Intent.ACTION_SEND`` with ``EXTRA_TEXT``. Any share target
    that handles ``text/plain`` (email, messaging, file-saver,
    cloud-paste) will accept it.

    Parameters
    ----------
    text : str
        Body content. No size cap enforced here, but Android's
        Intent extras have a practical ~1 MB ceiling; callers
        sharing large payloads should truncate first.
    subject : str
        Pre-filled on share targets that support a subject
        (email, etc.); ignored by others.
    chooser_title : str
        Title shown above the share sheet. Defaults to a
        translated "Share".
    on_error : callable(str) | None
        Invoked with a translated message on failure (non-
        Android, JNI exception, intent dispatch refused).
    """
    try:
        from kivy.utils import platform
    except Exception:
        platform = ''
    if platform != 'android':
        if on_error is not None:
            on_error(_tr(
                'Sharing is only available on Android.'))
        return False
    try:
        from jnius import autoclass
        Intent = autoclass('android.content.Intent')
        String = autoclass('java.lang.String')
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        activity = PythonActivity.mActivity
        if activity is None:
            if on_error is not None:
                on_error(_tr(
                    'Sharing failed: no Activity context.'))
            return False
        intent = Intent(Intent.ACTION_SEND)
        intent.setType('text/plain')
        if subject:
            intent.putExtra(Intent.EXTRA_SUBJECT, String(subject))
        intent.putExtra(Intent.EXTRA_TEXT, String(text or ''))
        title = chooser_title or _tr('Share')
        chooser = Intent.createChooser(intent, String(title))
        activity.startActivity(chooser)
        return True
    except Exception as ex:
        print(f'Share text error: {ex}')
        if on_error is not None:
            on_error(_tr(
                'Could not share text:\n{error}').format(error=ex))
        return False


def share_files(items, on_error=None, chooser_title=None,
                mime_type='text/plain'):
    """Share multiple files in one Android share dispatch via
    ``ACTION_SEND_MULTIPLE`` (since 0.52.6). Inserts each item
    into MediaStore Downloads to get a ``content://`` URI, then
    dispatches an ``Intent.ACTION_SEND_MULTIPLE`` with an
    ``ArrayList<Uri>`` in ``EXTRA_STREAM``. Email + messaging
    apps that accept ``text/plain`` accept this cleanly; the
    receiver gets each item as a distinct attachment.

    Replaces the bespoke "two-file blob" path in
    ``share_log_file`` for callers that ship a known set of
    daily-rotated log files plus a snapshot. ``share_log_file``
    remains as a thin shim that builds the items list from its
    legacy ``log_path`` + ``prev_path`` + ``prefix_text``
    arguments — peers can migrate to ``share_files`` directly
    when they're ready.

    Parameters
    ----------
    items : list[dict]
        Each item is one attachment, shaped as either::

            {'path': '/abs/path/to.file',
             'display_name': 'human-name.ext'}

        or::

            {'content': <str or bytes>,
             'display_name': 'human-name.ext'}

        ``path`` reads from disk (streamed in 64 KB chunks);
        ``content`` writes the in-memory blob (str is
        UTF-8-encoded). ``display_name`` is the MediaStore
        ``_display_name`` and the filename the receiver sees.
        Items missing both ``path`` and ``content`` are skipped
        with a logged warning; items with ``path`` pointing at
        a nonexistent file are likewise skipped (a stale
        retention sweep racing the share is the common case).
    on_error : callable(str) | None
        Invoked with a translated, user-visible message on any
        failure (non-Android, all-items-skipped, JNI exception,
        intent dispatch refused).
    chooser_title : str | None
        Title shown above the share sheet. Defaults to a
        translated "Share".
    mime_type : str
        MIME type for both each MediaStore entry and the share
        intent. Default ``text/plain`` covers log bundles; pass
        e.g. ``'application/octet-stream'`` for binary payloads.

    Returns
    -------
    bool
        True if the intent was dispatched (at least one item
        landed in MediaStore and the chooser opened); False
        otherwise. Failure to insert one item is non-fatal as
        long as at least one item succeeds — diagnostic-bundle
        callers want partial coverage over nothing.
    """
    # Best-effort diagnostic logging on two channels (since
    # 0.52.12):
    #   1. ``print()`` to stderr — captured by logcat, so a
    #      developer on the device with adb sees the trace in
    #      real time as the share dispatches. Sub-ms cost.
    #   2. ``log_diagnostic`` RPC into the daemon's always-on
    #      log — visible in the *next* successful ``Share
    #      diagnostics`` bundle (the current attempt's bundle
    #      can't include traces from the current attempt
    #      itself, since the bundle is built BEFORE the trace
    #      lines are written). Sub-100ms cost per call.
    # Both channels are best-effort with swallowed exceptions
    # so a stalled write never derails the share path.
    try:
        from .. import log_diagnostic as _log
    except Exception:
        _log = lambda *a, **k: False

    def _dlog(line):
        try:
            print(f'[share_files] {line}', flush=True)
        except Exception:
            pass
        try:
            _log('share_files', line)
        except Exception:
            pass

    item_count = len(items or [])
    _dlog(f'entry: item_count={item_count} mime_type={mime_type!r} '
          f'chooser_title={chooser_title!r}')

    try:
        from kivy.utils import platform
    except Exception:
        platform = ''
    _dlog(f'platform check: platform={platform!r}')
    if platform != 'android':
        if on_error is not None:
            # Build a short description for desktop / test runs so
            # the operator can see what would have been shared.
            descs = []
            for it in items or []:
                dn = (it or {}).get('display_name') or '?'
                if 'path' in (it or {}):
                    descs.append(f'{dn} ({it["path"]})')
                else:
                    descs.append(dn)
            on_error(_tr(
                'Files: {names}').format(names=', '.join(descs)))
        return False
    import os as _os
    try:
        from jnius import autoclass, cast
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        Intent = autoclass('android.content.Intent')
        ContentValues = autoclass('android.content.ContentValues')
        MediaStoreDownloads = autoclass(
            'android.provider.MediaStore$Downloads')
        ArrayList = autoclass('java.util.ArrayList')
        ClipData = autoclass('android.content.ClipData')
        ClipDataItem = autoclass('android.content.ClipData$Item')
        PackageManager = autoclass('android.content.pm.PackageManager')
        Uri = autoclass('android.net.Uri')
        Integer = autoclass('java.lang.Integer')
        String = autoclass('java.lang.String')
        activity = PythonActivity.mActivity
        context = cast('android.content.Context', activity)
        resolver = context.getContentResolver()
        pkg_manager = context.getPackageManager()
        _dlog('jnius autoclasses loaded; activity + resolver + '
              'pkg_manager ready')

        uris = ArrayList()
        raw_uris = []  # Python-side handles for ClipData below
        landed = 0
        for idx, it in enumerate(items or []):
            it = it or {}
            display_name = it.get('display_name') or 'attachment'
            has_path = bool(it.get('path'))
            has_content = ('content' in it
                           and it.get('content') is not None)
            has_uri = bool(it.get('uri'))
            content_bytes = 0
            if has_content:
                body_probe = it.get('content')
                if isinstance(body_probe, str):
                    content_bytes = len(body_probe.encode(
                        'utf-8', errors='replace'))
                elif isinstance(body_probe, (bytes, bytearray)):
                    content_bytes = len(body_probe)
            if not has_path and not has_content and not has_uri:
                _dlog(f'item[{idx}] skip: no path/content/uri '
                      f'display={display_name!r}')
                continue
            # URI items: the caller already has a content:// URI
            # (typically from ``prepare_share_bundle`` —
            # files staged under our own ContentProvider
            # authority since 0.52.13 so receivers like Signal
            # accept them). Skip MediaStore entirely.
            if has_uri:
                _dlog(f'item[{idx}] using pre-staged uri={it["uri"]!r} '
                      f'display={display_name!r}')
                try:
                    uri = Uri.parse(it['uri'])
                except Exception as ex:
                    _dlog(f'item[{idx}] Uri.parse raised: {ex!r}')
                    continue
                # Add the native Uri without a Parcelable cast.
                # Signal's getParcelableArrayListExtraCompat on
                # Android 13+ does a runtime class check against
                # Uri; if the parcel records the items as
                # Parcelable (via our cast) the typed read filters
                # them out and Signal bails with
                # IntentError.SEND_MULTIPLE_STREAM. The underlying
                # Java object IS Uri either way — the cast was a
                # jnius dispatching hint, not a Java-type change —
                # but removing the cast makes the dispatch decide
                # the correct ArrayList<Uri> shape. 0.52.15.
                uris.add(uri)
                raw_uris.append(uri)
                landed += 1
                _dlog(f'item[{idx}] landed via pre-staged uri')
                continue
            if has_path and not _os.path.isfile(it['path']):
                _dlog(f'item[{idx}] skip: missing path '
                      f'{it["path"]!r} display={display_name!r}')
                continue
            _dlog(f'item[{idx}] inserting: display={display_name!r} '
                  f'has_path={has_path} content_bytes={content_bytes}')
            values = ContentValues()
            values.put('_display_name', display_name)
            values.put('mime_type', mime_type)
            uri = resolver.insert(
                MediaStoreDownloads.EXTERNAL_CONTENT_URI, values)
            if not uri:
                _dlog(f'item[{idx}] MediaStore insert returned null '
                      f'display={display_name!r}')
                continue
            _dlog(f'item[{idx}] MediaStore uri={str(uri.toString())}')
            try:
                fos = resolver.openOutputStream(uri)
            except Exception as ex:
                _dlog(f'item[{idx}] openOutputStream raised: {ex!r}')
                continue
            written = 0
            try:
                if has_path:
                    with open(it['path'], 'rb') as f:
                        while True:
                            chunk = f.read(65536)
                            if not chunk:
                                break
                            fos.write(chunk)
                            written += len(chunk)
                else:
                    body = it['content']
                    if isinstance(body, str):
                        body = body.encode('utf-8', errors='replace')
                    fos.write(body)
                    written = len(body)
            except Exception as ex:
                _dlog(f'item[{idx}] write raised after {written} '
                      f'bytes: {ex!r}')
            finally:
                try:
                    fos.close()
                except Exception:
                    pass
            # Clear IS_PENDING so other apps can read the URI.
            # On Android Q+ MediaStore.Downloads inserts default
            # to ``is_pending=1`` ("owned by inserter, invisible
            # to others"). The receiver — Signal, Gmail, whatever
            # the user picked — sees the URI in EXTRA_STREAM but
            # ContentResolver returns null/exception when it tries
            # to read, so the receiver flashes its compose screen
            # and bails. Field-diagnosed via 0.52.11 logcat
            # 2026-06-22. The canonical Android-docs fix is a
            # post-write ``update(uri, {is_pending: 0}, ...)``.
            try:
                update_values = ContentValues()
                # jnius needs explicit Java Integer wrapping —
                # Python int doesn't auto-box to
                # ``java.lang.Integer`` and put(String, int) isn't
                # one of the ContentValues overloads. 0.52.12
                # field log showed the silent failure:
                # ``No methods called put in ContentValues matching
                # your arguments, requested: ('is_pending', 0)``.
                update_values.put('is_pending', Integer(0))
                rows = resolver.update(uri, update_values,
                                       None, None)
                _dlog(f'item[{idx}] is_pending cleared (rows={rows})')
            except Exception as ex:
                _dlog(f'item[{idx}] is_pending clear raised: {ex!r}')
            # Same no-cast rule as the URI branch above (see
            # 0.52.15 comment). Uri stays Uri in the parcel, so
            # receivers' typed getParcelableArrayListExtra reads
            # don't filter it out.
            uris.add(uri)
            raw_uris.append(uri)
            landed += 1
            _dlog(f'item[{idx}] landed: written={written} bytes')

        _dlog(f'insert phase done: landed={landed} of {item_count}')

        if landed == 0:
            _dlog('bail: landed=0; on_error sent, returning False')
            if on_error is not None:
                on_error(_tr('No files to share.'))
            return False

        # Route by item count: a single-URI share uses
        # ACTION_SEND (broader receiver compatibility — Signal's
        # ACTION_SEND_MULTIPLE resolver filters URIs to
        # image/video MIMEs only, so text content can't go
        # multi-attachment regardless of the manifest's claim);
        # multi-URI shares use ACTION_SEND_MULTIPLE. Field-
        # diagnosed via Signal's ShareRepository.kt source on
        # 2026-06-22.
        if landed == 1:
            intent = Intent(Intent.ACTION_SEND)
            intent.setType(mime_type)
            intent.putExtra(
                Intent.EXTRA_STREAM,
                cast('android.os.Parcelable', raw_uris[0]))
            _dlog('intent: ACTION_SEND (single) built, type + '
                  'extras set')
        else:
            intent = Intent(Intent.ACTION_SEND_MULTIPLE)
            intent.setType(mime_type)
            intent.putParcelableArrayListExtra(
                Intent.EXTRA_STREAM, uris)
            _dlog('intent: ACTION_SEND_MULTIPLE built, type + '
                  'extras set')
        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        intent.putExtra(Intent.EXTRA_SUBJECT,
                        String(_tr('AZT diagnostics')))
        # ACTION_SEND_MULTIPLE + Intent.createChooser drops URI
        # grants for many receivers (Signal, modern Gmail) unless
        # the URIs are ALSO attached as ClipData AND the
        # FLAG_GRANT_READ_URI_PERMISSION is propagated to the
        # chooser wrapper. 0.52.9 set ClipData on the inner
        # intent only — Signal still rejected on the field
        # report. 0.52.10 belt-and-suspenders:
        #   (a) ClipData on inner intent (was already there).
        #   (b) ClipData on the chooser wrapper too.
        #   (c) FLAG_GRANT_READ_URI_PERMISSION on the chooser.
        #   (d) Explicit per-package ``grantUriPermission`` to
        #       every Activity matching the inner intent's
        #       intent-filter (the canonical workaround
        #       documented at developer.android.com for the
        #       chooser-+-multi-URI case).
        # The ClipData label is a chooser-side hint and doesn't
        # reach the receiver. MIME type stays as ``mime_type``
        # so per-file metadata is preserved.
        try:
            clip = ClipData.newUri(
                resolver, String('AZT diagnostics'), raw_uris[0])
            for extra_uri in raw_uris[1:]:
                clip.addItem(ClipDataItem(extra_uri))
            intent.setClipData(clip)
            _dlog(f'clipdata: built {len(raw_uris)}-item ClipData '
                  f'and attached to inner intent')
        except Exception as ex:
            _dlog(f'clipdata: build/attach raised: {ex!r}')
            clip = None
        # Pre-grant the URIs to every package that can handle
        # the intent. Bounded scope (only the URIs we created,
        # only read permission, only packages whose
        # intent-filter accepts ACTION_SEND_MULTIPLE for
        # ``mime_type``). Catches receivers that ignore the
        # chooser-forwarded ClipData grant — Signal on some
        # Android builds appears to be one.
        granted_pkgs = []
        try:
            match_default = PackageManager.MATCH_DEFAULT_ONLY
            res_list = pkg_manager.queryIntentActivities(
                intent, match_default)
            n_targets = res_list.size()
            _dlog(f'pre-grant: queryIntentActivities returned '
                  f'n_targets={n_targets}')
            for i in range(n_targets):
                res_info = res_list.get(i)
                pkg_name = res_info.activityInfo.packageName
                granted_pkgs.append(str(pkg_name))
                for raw_uri in raw_uris:
                    context.grantUriPermission(
                        pkg_name, raw_uri,
                        Intent.FLAG_GRANT_READ_URI_PERMISSION)
            # Limit log line size — long target lists get
            # truncated server-side anyway, but keep it tidy.
            pkg_summary = ','.join(granted_pkgs[:16])
            if len(granted_pkgs) > 16:
                pkg_summary += f',… +{len(granted_pkgs) - 16} more'
            _dlog(f'pre-grant: granted to {len(granted_pkgs)} '
                  f'packages: {pkg_summary}')
        except Exception as ex:
            # Non-fatal — the ClipData + flag path may still
            # work for cooperating receivers. Log so a later
            # diagnostic share captures the failure mode.
            _dlog(f'pre-grant raised: {ex!r}')
        title = chooser_title or _tr('Share')
        try:
            chooser = Intent.createChooser(intent, String(title))
            chooser.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            if clip is not None:
                chooser.setClipData(clip)
            _dlog('chooser: built; ClipData + grant flag attached')
            activity.startActivity(chooser)
            _dlog('startActivity returned (chooser dispatched)')
        except Exception as ex:
            _dlog(f'chooser/startActivity raised: {ex!r}')
            raise
        return True
    except Exception as ex:
        _dlog(f'OUTER catch: {ex!r}')
        if on_error is not None:
            on_error(_tr(
                'Could not share files:\n{error}').format(error=ex))
        return False


def share_diagnostics_action(on_error=None):
    """Canonical "share diagnostics" composition. One source of
    truth so the picker's ``Share diagnostics`` button and the
    daemon-settings ``Share diagnostics`` button dispatch
    identical bundles — same label, same underlying action,
    two affordances (one per UI surface).

    Sequence (since 0.52.13 — pre-0.52.13 was MediaStore-based,
    which Signal refused as a receiver-side security policy):

    1. Ask the daemon to stage the bundle via
       ``prepare_share_bundle``. The daemon writes snapshot +
       per-day daemon logs into ``$AZT_HOME/.shares/<token>/``,
       returns the URI paths.
    2. Build content URIs of the form
       ``content://org.atoznback.aztcollab/<uri_path>`` — our
       own ContentProvider authority, served via
       ``AZTCollabProvider.openFile``.
    3. Dispatch via ``share_files`` (``ACTION_SEND_MULTIPLE``).
       Each item is a URI item; share_files attaches them via
       ClipData and ``EXTRA_STREAM`` ArrayList without going
       through MediaStore.

    Two fallback paths:

    - **Daemon unreachable** (``prepare_share_bundle``
      returned None): send a ``share_text`` with an
      operator-actionable "confirm the AZT Collaboration app
      is installed" message.
    - **Empty bundle** (daemon reachable but nothing staged):
      send a ``share_text`` with "reproduce the issue first,
      then tap Share diagnostics again."

    Parameters
    ----------
    on_error : callable(str) | None
        Invoked with a translated, user-visible message on
        any failure. Both surfaces pass their own
        status-display callback — popup for the picker
        (it has no inline status area), label text for the
        daemon-settings UI (it has a status line below the
        button).

    Returns
    -------
    bool
        True if the share sheet (or the fallback share-text
        intent) was dispatched; False otherwise.
    """
    from .. import prepare_share_bundle
    from ..translate import tr as _tr
    from ..transports.android_cp import CANONICAL_AUTHORITY

    bundle = prepare_share_bundle()

    if bundle is None:
        return share_text(
            text=_tr(
                'Could not reach the AZT Collab daemon. '
                'Please confirm the AZT Collaboration app is '
                'installed and re-open this app.'),
            on_error=on_error)

    items = []
    for entry in bundle.get('items') or []:
        uri_path = entry.get('uri_path') or ''
        display_name = entry.get('display_name') or ''
        if not uri_path or not display_name:
            continue
        # Reuse the same authority the RPC transport uses.
        # That guarantees we point at the provider that's
        # actually serving these paths via openFile —
        # ``AZTCollabProvider`` declared at authority
        # ``org.atoznback.aztcollab``.
        items.append({
            'uri': f'content://{CANONICAL_AUTHORITY}/{uri_path}',
            'display_name': display_name,
        })

    if not items:
        return share_text(
            text=_tr(
                'No diagnostics available yet. Reproduce the '
                'issue first, then tap Share diagnostics '
                'again.'),
            on_error=on_error)

    # Diagnostic bundle is a gzipped tar (``.tar.gz``) since 0.52.22
    # (was ``.zip`` 0.52.19–0.52.21; a field email server silently
    # stripped ``.zip`` attachments — gzip's magic bytes dodge both
    # extension and content-sniffing filters). Intent-level MIME must
    # match (``application/gzip``) so Signal's single-attachment
    # ACTION_SEND filter accepts it (its ``application/*`` mimeType
    # entry in the manifest covers this) and other receivers' pickers
    # route the file correctly. The daemon builds the .tar.gz in
    # ``prepare_share_bundle``; the provider's ``getType`` maps the
    # ``gz`` extension to ``application/gzip`` for receivers that
    # consult it.
    from ..diagnostics import DIAGNOSTICS_MIME
    return share_files(items, on_error=on_error,
                       mime_type=DIAGNOSTICS_MIME)


def share_log_file(log_path, prev_path=None, on_error=None,
                   display_name=None, prefix_text=''):
    """Share a log file via Android's share sheet. Bundles
    ``log_path`` and (optionally) ``prev_path`` into one
    ``text/plain`` blob, inserts into MediaStore Downloads to
    get a ``content://`` URI, and dispatches an
    ``Intent.ACTION_SEND`` with the URI attached.

    ``prefix_text`` is prepended (with its own section break) — the
    picker's Share-diagnostics button uses this to ship a daemon-
    side registry snapshot alongside the log file even when the
    daemon-log-to-file toggle has never been enabled (the snapshot
    alone is the diagnostic payload in that case). Empty by default.

    Differences from ``share_text``: file-based source (reads
    from disk), bundles two files when ``prev_path`` is set, and
    attaches as a real file URI (``EXTRA_STREAM``) instead of
    inlining as ``EXTRA_TEXT``. Use this when the payload is
    big enough that the receiver wants to save it as a file
    rather than read it inline in their messaging app, and when
    a previous-session log is worth shipping alongside (rotate-
    on-launch patterns).

    Parameters
    ----------
    log_path : str
        Current-session log file path.
    prev_path : str | None
        Optional previous-session log path (typically
        ``log_path + '.prev'`` from a rotate-on-launch scheme).
        Prepended with a section break; silently ignored if the
        path is empty or the file doesn't exist.
    on_error : callable(str) | None
        Invoked with a translated message on any failure.
    display_name : str | None
        MediaStore display name. Defaults to
        ``f'azt_log_{stamp}.log'`` where ``stamp`` is the
        current local time as ``YYYYMMDD_HHMMSS``.

    Returns
    -------
    bool
        True if the share intent was dispatched; False on
        platform mismatch, missing log, or JNI failure.

    Bundled blob shape::

        === previous session (<prev_path>) ===
        <prev contents>

        === current session (<log_path>) ===
        <current contents>

    Section breaks let the receiver scroll directly to the
    relevant session. Filed by recorder 1.41.24
    (NOTES_TO_DAEMON.md 2026-05-13); shipped in 0.41.19.
    """
    try:
        from kivy.utils import platform
    except Exception:
        platform = ''
    if platform != 'android':
        if on_error is not None:
            on_error(_tr(
                'Log file: {path}').format(path=log_path or ''))
        return False
    # Read both files into a single blob. Empty / missing prev
    # is fine — we just skip the section.
    blob = _bundle_log_blob(log_path, prev_path)
    if prefix_text:
        # Prepend a diagnostic block ahead of the log sections so a
        # reader scrolling from the top hits state-of-the-world
        # before timeline. Section header matches the log-bundle
        # style for visual consistency.
        prefix_block = (f'=== diagnostic snapshot ===\n'
                        f'{prefix_text}\n')
        blob = prefix_block + (blob if blob else '')
    if not blob:
        if on_error is not None:
            on_error(_tr('Log file is empty.'))
        return False
    if not display_name:
        import datetime as _dt
        stamp = _dt.datetime.now().strftime('%Y%m%d_%H%M%S')
        # Splice the daemon's short peer-id tag into the display
        # name so a tester collecting two phones' logs into one
        # folder gets distinct filenames (e.g.
        # ``azt_log_20260522_140101_07c089f2.log`` vs ...
        # ``_a1b00d64.log``). Same 8-char tag the on-disk log file
        # carries (``daemon-07c089f2.log``) and that already shows
        # up in ``[lan-push] '07c089f2'`` lines. Falls back to the
        # un-tagged form if ``lan_peer_id`` is unavailable (peer
        # didn't ship cryptography, server transient, etc.) so the
        # share still works.
        tag = ''
        try:
            import azt_collab_client as _client
            info = _client.lan_peer_id() or {}
            hex_str = (info.get('peer_id') or '')
            tag = hex_str[:8]
        except Exception:
            tag = ''
        if tag:
            display_name = f'azt_log_{stamp}_{tag}.log'
        else:
            display_name = f'azt_log_{stamp}.log'
    try:
        from jnius import autoclass, cast
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        Intent = autoclass('android.content.Intent')
        ContentValues = autoclass('android.content.ContentValues')
        MediaStoreDownloads = autoclass(
            'android.provider.MediaStore$Downloads')
        String = autoclass('java.lang.String')
        activity = PythonActivity.mActivity
        context = cast('android.content.Context', activity)

        values = ContentValues()
        values.put('_display_name', display_name)
        values.put('mime_type', 'text/plain')
        resolver = context.getContentResolver()
        uri = resolver.insert(
            MediaStoreDownloads.EXTERNAL_CONTENT_URI, values)
        if not uri:
            if on_error is not None:
                on_error(_tr(
                    'Share failed: could not create MediaStore '
                    'entry'))
            return False
        fos = resolver.openOutputStream(uri)
        try:
            fos.write(blob.encode('utf-8', errors='replace'))
        finally:
            fos.close()

        intent = Intent(Intent.ACTION_SEND)
        intent.setType('text/plain')
        intent.putExtra(Intent.EXTRA_STREAM,
                        cast('android.os.Parcelable', uri))
        intent.putExtra(Intent.EXTRA_SUBJECT,
                        String(_tr('AZT log')))
        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        chooser = Intent.createChooser(
            intent, String(_tr('Share log')))
        activity.startActivity(chooser)
        return True
    except Exception as ex:
        print(f'Share log error: {ex}')
        if on_error is not None:
            on_error(_tr(
                'Could not share log:\n{error}').format(error=ex))
        return False


def _bundle_log_blob(log_path, prev_path):
    """Read current + previous log files and join into one
    text/plain blob. Empty string when neither file has content.

    Decorates the current-session header with the running daemon
    version (read from ``$AZT_HOME/server.json``) so a tester's
    emailed log carries an unambiguous "what produced this" tag
    without grepping the body. Silent on miss — the header drops
    back to the path-only form."""
    import os as _os
    import json as _json
    ver_tag = ''
    try:
        from .. import paths as _paths
        sjp = _os.path.join(_paths.azt_home(), 'server.json')
        with open(sjp, 'r', encoding='utf-8') as f:
            v = str(_json.load(f).get('version', '')).strip()
        if v:
            ver_tag = f' [daemon {v}]'
    except (OSError, ValueError, Exception):
        pass
    parts = []
    if prev_path and _os.path.isfile(prev_path):
        try:
            with open(prev_path, 'r', encoding='utf-8',
                      errors='replace') as f:
                prev = f.read()
        except OSError:
            prev = ''
        if prev:
            parts.append(
                f'=== previous session ({prev_path}) ===\n'
                f'{prev}\n')
    if log_path and _os.path.isfile(log_path):
        try:
            with open(log_path, 'r', encoding='utf-8',
                      errors='replace') as f:
                cur = f.read()
        except OSError:
            cur = ''
        if cur:
            parts.append(
                f'=== current session ({log_path}){ver_tag} ===\n'
                f'{cur}\n')
    return '\n'.join(parts)


def email_text(text, to='', subject='', on_error=None):
    """Open the user's email composer pre-filled with *to* /
    *subject* / *text* body, via ``Intent.ACTION_SENDTO`` and a
    ``mailto:`` URI.

    The ``SENDTO`` flavour (vs. ``SEND``) restricts the share
    sheet to apps that handle the ``mailto:`` scheme — i.e. email
    clients only. Better UX than ``ACTION_SEND`` when the user's
    intent specifically is "email this to someone": no messaging
    apps / cloud-paste targets clutter the picker.

    Parameters
    ----------
    text : str
        Email body. Goes into the URI's ``body`` query parameter
        per RFC 6068. Practical size limit on a ``mailto:`` URI
        is in the kilobytes; callers sharing larger payloads
        should use ``share_text`` instead (most email apps accept
        ``ACTION_SEND`` too).
    to : str
        Pre-filled recipient. Empty string lets the user pick.
    subject : str
        Pre-filled subject line.
    on_error : callable(str) | None
        Invoked with a translated message on failure.
    """
    try:
        from kivy.utils import platform
    except Exception:
        platform = ''
    if platform != 'android':
        if on_error is not None:
            on_error(_tr(
                'Emailing is only available on Android.'))
        return False
    try:
        from urllib.parse import quote as _q
        from jnius import autoclass
        Intent = autoclass('android.content.Intent')
        Uri = autoclass('android.net.Uri')
        String = autoclass('java.lang.String')
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        activity = PythonActivity.mActivity
        if activity is None:
            if on_error is not None:
                on_error(_tr(
                    'Emailing failed: no Activity context.'))
            return False
        # Build mailto: URI. ``urllib.parse.quote`` with default
        # safe='/' handles spaces, newlines, ampersands — anything
        # that'd break the URI grammar.
        params = []
        if subject:
            params.append(f'subject={_q(subject, safe="")}')
        if text:
            params.append(f'body={_q(text, safe="")}')
        mailto = 'mailto:' + _q(to or '', safe='@')
        if params:
            mailto += '?' + '&'.join(params)
        intent = Intent(Intent.ACTION_SENDTO)
        intent.setData(Uri.parse(mailto))
        chooser = Intent.createChooser(
            intent, String(_tr('Email')))
        activity.startActivity(chooser)
        return True
    except Exception as ex:
        print(f'Email error: {ex}')
        if on_error is not None:
            on_error(_tr(
                'Could not start email:\n{error}').format(error=ex))
        return False
