"""Tests for ``azt_collab_client.ui.qr_scan`` — the QR-scan
helper that wraps ZXing's IntentIntegrator on Android.

Limited to what we can verify without a real Android Activity:

- ``available()`` returns False on desktop (no jnius / no kivy
  platform == 'android'). This is the gate peers use to decide
  whether to show a "Scan QR" button at all; getting the gate
  wrong on the False side just means the button doesn't appear,
  but a False-positive would hand the user a non-functional
  button.
- ``scan_qr()`` on desktop fires ``on_cancel`` without launching
  anything. Peers that didn't check ``available()`` first get a
  no-op rather than a crash — defence-in-depth.
- The module imports cleanly without jnius installed (it
  defers the import inside ``scan_qr``).
"""

import pytest


def test_qr_scan_imports_without_jnius():
    """Module-level import must NOT touch jnius — the helper is
    consumed by Kivy peer code that may import it at module
    load time on desktop. Only ``scan_qr`` and ``available``
    should attempt the import lazily."""
    import azt_collab_client.ui.qr_scan  # noqa: F401


def test_available_false_on_desktop():
    """Desktop / non-android platforms must report unavailable so
    the calling UI hides the Scan QR button."""
    import azt_collab_client.ui.qr_scan as qr_scan
    # Conftest's ``desktop`` fixture isn't autouse here; the
    # default ``kivy.utils.platform`` (whatever it is in the
    # test env — typically 'linux' or 'macosx') is non-android,
    # which is the case ``available`` must handle.
    assert qr_scan.available() is False


def test_scan_qr_no_op_on_desktop_fires_on_cancel():
    """If a caller bypasses the ``available()`` gate and calls
    ``scan_qr`` on desktop anyway, the helper should call
    ``on_cancel`` rather than crash. Defence-in-depth — peers
    that didn't read § 13 cleanly are still safe."""
    import azt_collab_client.ui.qr_scan as qr_scan
    cancels = []
    qr_scan.scan_qr(
        on_result=lambda _t: pytest.fail(
            'on_result should NOT fire on desktop'),
        on_cancel=lambda: cancels.append(True),
    )
    assert cancels == [True]


def test_scan_qr_handles_missing_on_cancel():
    """``on_cancel`` is optional. Calling ``scan_qr`` without it
    on desktop should silently no-op."""
    import azt_collab_client.ui.qr_scan as qr_scan
    qr_scan.scan_qr(on_result=lambda _t: None)
    # No assertion; the absence of an exception is the test.
