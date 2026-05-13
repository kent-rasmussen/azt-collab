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


def share_log_file(log_path, prev_path=None, on_error=None,
                   display_name=None):
    """Share a log file via Android's share sheet. Bundles
    ``log_path`` and (optionally) ``prev_path`` into one
    ``text/plain`` blob, inserts into MediaStore Downloads to
    get a ``content://`` URI, and dispatches an
    ``Intent.ACTION_SEND`` with the URI attached.

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
    if not blob:
        if on_error is not None:
            on_error(_tr('Log file is empty.'))
        return False
    if not display_name:
        import datetime as _dt
        stamp = _dt.datetime.now().strftime('%Y%m%d_%H%M%S')
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
    text/plain blob. Empty string when neither file has content."""
    import os as _os
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
                f'=== current session ({log_path}) ===\n'
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
