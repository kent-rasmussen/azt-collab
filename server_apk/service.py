"""
Reserved.

Per cleanup-draft #3 q4: the server APK is allowed to be transient.
Android can stop the process when no peer is querying; the next
peer call wakes it back up via ``ContentResolver.call``. No
foreground-service notification ships today.

This file is kept (and declared in ``manifest_extras.xml``) only so
that turning the always-on path on later is a code-only change. If
you ever want it, populate this module and call into it from
``main.py`` after ``install_callbacks()``.
"""
