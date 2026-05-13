"""QR-code scanning via zxing-android-embedded.

Public API:

    from azt_collab_client.ui.qr_scan import scan_qr, available

    if available():
        scan_qr(on_result=lambda url: ...,
                on_cancel=lambda: ...)

``available()`` is a cheap probe that returns False on desktop /
on Android builds without the ``zxing-android-embedded`` Gradle
dependency in their APK. Use it to gate UI affordances (the
"Scan" button on the clone popup, etc.) so the button doesn't
appear on platforms that can't honour it.

Dependency: the consuming APK's ``buildozer.spec`` must list
``com.journeyapps:zxing-android-embedded:4.3.0`` under
``android.gradle_dependencies``. That pulls in:

- ``com.journeyapps.barcodescanner.CaptureActivity`` — the
  full-screen camera-preview Activity that ZXing dispatches
  via Intent.
- ``com.journeyapps.barcodescanner.IntentIntegrator`` — the
  helper that builds the Intent and parses the result.

The Activity inherits from AppCompatActivity and renders its own
UI (camera preview + targeting rect + "point at a code" prompt).
We don't author it — we just launch the Intent and read the
``SCAN_RESULT`` extra from ``onActivityResult``.

No automatic camera-permission handling on our side: ZXing's
CaptureActivity requests CAMERA at launch through Android's
runtime-permission flow, so the consuming APK only needs CAMERA
in its manifest (it does — the recorder already declares it).

Desktop has no equivalent. The contract is "QR scanning is an
Android-only convenience; desktop users paste URLs manually" —
documented in CLIENT_INTEGRATION.md § 10 (or wherever we land
the clone-flow section).
"""

import sys


# Used by ZXing's IntentIntegrator. Sentinel value defined in the
# library; we duplicate it here so we can match against
# ``request_code`` in ``on_activity_result`` without going
# through ``IntentIntegrator.parseActivityResult`` (which fights
# pyjnius's Java-side type unification on some Android versions).
_ZXING_REQUEST_CODE = 0x0000c0de


def available():
    """True if QR scanning is callable on this platform.

    Cheap; the heaviest thing it does is ``import jnius`` (already
    imported on Android, ImportError on desktop). Doesn't actually
    touch ZXing classes — those resolve lazily inside ``scan_qr``."""
    try:
        from kivy.utils import platform
    except Exception:
        return False
    if platform != 'android':
        return False
    try:
        import jnius  # noqa: F401
    except ImportError:
        return False
    return True


def scan_qr(on_result, on_cancel=None, prompt=''):
    """Launch the ZXing CaptureActivity and call ``on_result(text)``
    with the decoded payload on success.

    Parameters:

    ``on_result(text: str)`` — fires once with the decoded string
        on RESULT_OK. The caller decides whether to validate that
        the payload is a URL / repo slug / whatever the caller is
        expecting.
    ``on_cancel()`` — optional. Fires on RESULT_CANCELED (back
        button / dismiss / camera permission denied).
    ``prompt`` — optional text shown over the camera preview.
        Defaults empty; ZXing's default prompt is fine for most
        cases.

    Both callbacks fire on the Kivy main thread via
    ``Clock.schedule_once`` so callers can safely touch widget
    state inside them.

    No-op (logs to stderr, fires ``on_cancel``) when
    ``available()`` is False — callers should gate the UI on
    ``available()`` but the runtime check is defence-in-depth."""
    # Entry log so we can see in logcat that scan_qr was at
    # least called — closes the "silent button" diagnostic gap
    # from NOTES_TO_DAEMON.md 0.41.0 § 1.
    print('[qr_scan] scan_qr called', file=sys.stderr, flush=True)

    if not available():
        print('[qr_scan] not available on this platform; '
              'caller should have gated on available()',
              file=sys.stderr, flush=True)
        if on_cancel is not None:
            on_cancel()
        return

    from jnius import autoclass
    try:
        from android import activity as android_activity  # type: ignore
    except ImportError as ex:
        # The ``android`` Python module is shipped by p4a — if
        # this import fails on Android, the APK build is
        # missing something fundamental. Surface loudly.
        print(f'[qr_scan] cannot import android.activity: {ex}',
              file=sys.stderr, flush=True)
        if on_cancel is not None:
            on_cancel()
        return
    from kivy.clock import Clock

    try:
        IntentIntegrator = autoclass(
            'com.journeyapps.barcodescanner.IntentIntegrator')
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
    except Exception as ex:
        # zxing-android-embedded isn't in the APK. Tell the caller
        # we couldn't scan; they'll fall back to manual paste.
        print(f'[qr_scan] ZXing classes unresolvable '
              f'(zxing-android-embedded missing from APK?): '
              f'{type(ex).__name__}: {ex}',
              file=sys.stderr, flush=True)
        if on_cancel is not None:
            on_cancel()
        return

    activity = PythonActivity.mActivity
    if activity is None:
        print('[qr_scan] PythonActivity.mActivity is None — '
              'no Activity context to launch Intent from',
              file=sys.stderr, flush=True)
        if on_cancel is not None:
            on_cancel()
        return

    # Bind the result handler BEFORE we launch the scan Intent so
    # there's no race where the result fires before we've registered.
    # Single-shot semantics: we unbind ourselves on the first
    # matching request_code so a subsequent ``scan_qr`` call
    # doesn't double-fire the previous handler. Matches the
    # ``_unbind_handler`` discipline in ``pick_project``.
    bind_state = {'bound': False}

    def _unbind():
        if not bind_state['bound']:
            return
        bind_state['bound'] = False
        try:
            android_activity.unbind(on_activity_result=_on_result)
        except Exception:
            pass

    def _on_result(request_code, result_code, data):
        if request_code != _ZXING_REQUEST_CODE:
            return
        _unbind()
        # RESULT_OK = -1, RESULT_CANCELED = 0.
        if result_code != -1 or data is None:
            print(f'[qr_scan] cancelled / no data: '
                  f'result_code={result_code} data={data!r}',
                  file=sys.stderr, flush=True)
            if on_cancel is not None:
                Clock.schedule_once(lambda _dt: on_cancel(), 0)
            return
        try:
            text = data.getStringExtra('SCAN_RESULT') or ''
        except Exception as ex:
            print(f'[qr_scan] getStringExtra raised: {ex}',
                  file=sys.stderr, flush=True)
            text = ''
        if not text:
            if on_cancel is not None:
                Clock.schedule_once(lambda _dt: on_cancel(), 0)
            return
        Clock.schedule_once(lambda _dt: on_result(text), 0)

    android_activity.bind(on_activity_result=_on_result)
    bind_state['bound'] = True

    integrator = IntentIntegrator(activity)
    try:
        # Restrict to QR codes — barcodes would never carry a
        # clone URL and scanning them would just confuse the
        # user. ZXing's API takes a List<String> of format
        # constants.
        ArrayList = autoclass('java.util.ArrayList')
        formats = ArrayList()
        formats.add('QR_CODE')
        integrator.setDesiredBarcodeFormats(formats)
    except Exception as ex:
        print(f'[qr_scan] setDesiredBarcodeFormats failed '
              f'(non-fatal — scanner accepts any format): {ex}',
              file=sys.stderr, flush=True)
    if prompt:
        try:
            integrator.setPrompt(prompt)
        except Exception:
            pass
    try:
        integrator.setOrientationLocked(False)
    except Exception:
        pass
    print('[qr_scan] launching IntentIntegrator.initiateScan',
          file=sys.stderr, flush=True)
    try:
        integrator.initiateScan()
    except Exception as ex:
        _unbind()
        print(f'[qr_scan] initiateScan failed: '
              f'{type(ex).__name__}: {ex}',
              file=sys.stderr, flush=True)
        if on_cancel is not None:
            Clock.schedule_once(lambda _dt: on_cancel(), 0)
