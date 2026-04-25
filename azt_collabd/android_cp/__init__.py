"""
Android ContentProvider transport for the A-Z+T collab daemon.

When the recorder (or any sister-suite app) runs on Android, the daemon
runs in-process inside the host APK and is exposed cross-app via a
``ContentProvider`` declared in AndroidManifest.xml. Sibling AZT apps
discover the provider, call ``ContentResolver.call(method, arg, extras)``
or ``openFile(uri, mode)``, and reach the same ``dispatch()`` table the
HTTP transport uses.

Provider authority pattern: ``<package>.aztcollab`` (e.g.
``org.atoznback.azt_recorder.aztcollab``).

Auth is at the Android signature level — the provider is exported with
``protectionLevel="signature"`` so only APKs signed with the AZT suite
keystore can call it.

This package contains the Python side: a pyjnius shim that registers a
callback Java can invoke from its ContentProvider override. The Java
stub itself lives under ``android/`` at the project root and is
compiled into the APK via ``android.add_src``.

The SHA-256 fingerprint of the suite signing key is recorded in
``android/SUITE_FINGERPRINT`` for verification scripts.
"""
