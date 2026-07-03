"""Shared diagnostics-archive format for the AZT suite.

Single source of truth for the diagnostics bundle's **container format**
(gzipped tar), filename, and MIME — imported by the daemon
(``azt_collabd/server.py::_h_prepare_share_bundle``) and by every peer
that ships its own diagnostics (recorder, future viewer).

Collection + staging + dispatch stay per-process on purpose: each side
bundles files the other can't see, and on Android they live in separate
APK sandboxes (a peer's FileProvider vs. the daemon's ContentProvider
``_shares/<token>/``). Only the *format* is common — so it lives here,
where a format change is made once instead of drifting across builds.
(The 0.52.19→0.52.23 zip→tar.gz switch for the Dome mail server had to
be applied twice and the recorder copy shipped stale ``.zip`` for a
build; this module removes that foot-gun — see the NOTES_TO_DAEMON
"REFACTOR" item that requested it.)

Import direction: the daemon imports the client (allowed); the client
never imports the daemon. A client-hosted helper therefore serves the
daemon and every peer. Pure stdlib — no Kivy, no ``azt_collabd`` — safe
to import anywhere the client is.
"""

from __future__ import annotations

import io as _io
import re
import tarfile
import time

# MIME for the archive: intent type on ``share_files`` AND the
# ContentProvider ``getType`` must both match this. gzip (not zip)
# because a field mail server strips ``.zip`` — gzip's magic bytes dodge
# extension- and content-sniffing filters. Single attachment via
# ACTION_SEND, so Signal's SEND_MULTIPLE image/video filter is avoided.
DIAGNOSTICS_MIME = 'application/gzip'

# Mirror of the daemon's ``_SHARE_BUNDLE_FILENAME_RE`` charset so a
# composed name is always a valid, provider-servable basename.
_ARCHIVE_NAME_RE = re.compile(r'^[A-Za-z0-9._-]{1,128}$')


def diagnostics_archive_name(slug='', stamp=''):
    """Return the diagnostics archive basename.

    ``slug=''`` → ``azt_diagnostics_<stamp>.tar.gz`` (the daemon's own
    bundle). ``slug='recorder'`` → ``azt_recorder_diagnostics_<stamp>``
    ``.tar.gz``. Falls back to a stamp-less (then slug-less) safe name if
    the composed one fails the charset guard, so callers never produce an
    unservable filename."""
    slug = str(slug or '').strip()
    stamp = str(stamp or '').strip()
    mid = f'{slug}_' if slug else ''
    if stamp:
        name = f'azt_{mid}diagnostics_{stamp}.tar.gz'
        if _ARCHIVE_NAME_RE.match(name):
            return name
    name = f'azt_{mid}diagnostics.tar.gz'
    if _ARCHIVE_NAME_RE.match(name):
        return name
    return 'azt_diagnostics.tar.gz'


def build_diagnostics_targz(dest_path, *, file_items=(), content_items=(),
                            compresslevel=6):
    """Write a gzipped-tar diagnostics archive to *dest_path*.

    ``file_items``: iterable of ``(arcname, src_path)`` — real files on
    disk. A per-file ``OSError`` (unreadable / vanished mid-run) is
    swallowed and that entry skipped, so one bad file doesn't lose the
    whole bundle.

    ``content_items``: iterable of ``(arcname, str|bytes)`` — in-memory
    blobs (a snapshot, or daemon logs pulled via RPC), written with
    ``TarInfo`` + ``addfile`` at ``mtime=now``. ``None`` payloads are
    skipped.

    Content entries are written first, then files — matching the daemon's
    snapshot-first convention. Returns the number of entries actually
    written. Raises ``OSError`` only if the archive itself can't be
    opened/written; the caller maps that to its share-write-failed path.
    """
    written = 0
    now = int(time.time())
    with tarfile.open(dest_path, 'w:gz', compresslevel=compresslevel) as tf:
        for arcname, payload in (content_items or ()):
            if payload is None:
                continue
            data = (payload.encode('utf-8')
                    if isinstance(payload, str) else bytes(payload))
            info = tarfile.TarInfo(name=str(arcname))
            info.size = len(data)
            info.mtime = now
            tf.addfile(info, _io.BytesIO(data))
            written += 1
        for arcname, src_path in (file_items or ()):
            try:
                tf.add(src_path, arcname=str(arcname))
                written += 1
            except OSError:
                continue
    return written
