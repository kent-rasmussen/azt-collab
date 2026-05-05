"""Share the running APK via Android's share sheet.

Inserts the APK bytes into MediaStore Downloads to obtain a
``content://`` URI, then dispatches an ``ACTION_SEND`` intent. Lives
in the client UI package so every peer (recorder, viewer, future
sister apps) gets a "Share this app" button without each one
re-deriving the JNI dance and re-translating the error strings.

Android-only — non-Android platforms invoke ``on_error`` with a
translated "APK sharing is only available on Android." so the host
can surface it in its usual error channel.
"""

from ..translate import tr as _tr


def share_running_apk(filename='app.apk', on_error=None):
    """Share the running APK via Android's share sheet.

    Parameters
    ----------
    filename : str
        MediaStore display name for the shared file (e.g.
        ``'azt_recorder.apk'``). Each peer passes its own — the
        running APK's actual on-disk name is opaque.
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
