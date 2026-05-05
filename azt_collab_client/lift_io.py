"""Cross-package LIFT-file access for peer apps.

The daemon owns a single canonical copy of every LIFT file under
``$AZT_HOME/projects/<lang>/<file>.lift``. On desktop, peers share
the filesystem with the daemon and a plain ``open()`` works. On the
new Android model the daemon lives in the standalone server APK
(``org.atoznback.aztcollab``) and its ``filesDir`` is sandboxed away
from peer packages — peers must go through ``AZTCollabProvider``
(``ContentResolver.openFileDescriptor`` against a ``content://`` URI
the picker emits) to reach the canonical copy.

``LiftHandle`` papers over the difference: ``pick_project()`` may
return a filesystem path (desktop, or open-file flow on any
platform) or a ``content://`` URI (Android clone / template flow on
the new model). Peer code uses the same shape regardless::

    from azt_collab_client import LiftHandle
    from xml.etree import ElementTree

    handle = LiftHandle(path_or_uri_from_picker)
    with handle.open_read() as f:
        tree = ElementTree.parse(f)
    ...
    with handle.open_write() as f:
        tree.write(f, encoding='utf-8', xml_declaration=True)

There is **no caching layer**. Every read / write hits the canonical
copy through the provider (or the local file). Lost-update
protection relies on the daemon's serialization — peers must not
keep their own edited copy in their private ``filesDir`` and write
it back later, because two peers (or the same peer across two
sessions) reading at T0 and writing at T1 / T2 would race and the
later writer would clobber the earlier writer's changes.

Audio and other binary files served by the same provider can use
the same handle shape; ``LiftHandle`` is named for the LIFT case but
nothing in it is LIFT-specific.
"""

import os


_CONTENT_PREFIX = 'content://'


def is_content_uri(s):
    """True iff ``s`` is a ``content://`` URI string. Useful for
    code paths that need to branch before instantiating a handle
    (e.g. registering a project: working_dir on a URI is the URI
    itself, on a path it's ``os.path.dirname``)."""
    return isinstance(s, str) and s.startswith(_CONTENT_PREFIX)


class LiftHandle:
    """Read/write wrapper around either a filesystem path or a
    ``content://`` URI. Returns binary file-like objects suitable
    for ``ElementTree.parse`` / ``ElementTree.write`` and similar
    APIs that accept either a path or a file-like."""

    def __init__(self, path_or_uri):
        if not isinstance(path_or_uri, str) or not path_or_uri:
            raise ValueError(
                f'LiftHandle needs a non-empty str, got {path_or_uri!r}')
        self.path_or_uri = path_or_uri
        self.is_uri = path_or_uri.startswith(_CONTENT_PREFIX)

    def open_read(self):
        """Open for binary read. Returns a file-like usable as a
        context manager. Closing closes the underlying descriptor /
        file."""
        if self.is_uri:
            return _open_content_uri(self.path_or_uri, 'r')
        return open(self.path_or_uri, 'rb')

    def open_write(self):
        """Open for binary write (truncating). Returns a file-like
        usable as a context manager. The provider serializes
        concurrent writers; peers should still complete their write
        promptly so the daemon can pick up the change for the next
        sync."""
        if self.is_uri:
            return _open_content_uri(self.path_or_uri, 'w')
        return open(self.path_or_uri, 'wb')

    def display_path(self):
        """Short, human-readable string for log lines / error
        messages. URIs render as themselves; filesystem paths render
        as basenames so logs don't carry full sandbox paths."""
        if self.is_uri:
            return self.path_or_uri
        return os.path.basename(self.path_or_uri)

    def __repr__(self):
        kind = 'uri' if self.is_uri else 'path'
        return f'<LiftHandle {kind}={self.path_or_uri!r}>'


def _sibling_uri_or_path(lift_path_or_uri, subdir, basename):
    """Compose the URI / filesystem path for a sibling resource of
    a project's LIFT file. ``subdir`` is one of ``'audio'`` or
    ``'images'``; ``basename`` is the leaf filename only.

    On a ``content://`` URI, returns a same-authority URI with the
    project's ``<lang>`` segment preserved::

        content://<auth>/<lang>/<file>.lift   →
        content://<auth>/<lang>/<subdir>/<basename>

    On a filesystem path, returns
    ``<dirname-of-lift>/<subdir>/<basename>``. Mirrors what desktop
    code does with ``os.path.join(os.path.dirname(lift_path), …)``,
    so callers can stay agnostic about path vs URI."""
    if not basename or '/' in basename or basename in ('..', '.'):
        raise ValueError(f'invalid basename: {basename!r}')
    if subdir not in ('audio', 'images'):
        raise ValueError(f'subdir must be audio or images, got {subdir!r}')
    if is_content_uri(lift_path_or_uri):
        # Drop the scheme, split on '/', take authority + lang prefix
        # before the .lift segment, swap subdir + basename in.
        rest = lift_path_or_uri[len(_CONTENT_PREFIX):]
        parts = rest.split('/')
        # Expect [<authority>, <lang>, <file>.lift]; tolerate trailing
        # slashes / empty terminal segments.
        clean = [p for p in parts if p]
        if len(clean) < 2:
            raise ValueError(
                f'malformed lift URI (need authority and at least '
                f'<lang> segment): {lift_path_or_uri!r}')
        authority, lang = clean[0], clean[1]
        return f'{_CONTENT_PREFIX}{authority}/{lang}/{subdir}/{basename}'
    return os.path.join(os.path.dirname(lift_path_or_uri),
                        subdir, basename)


def audio_uri_for(lift_path_or_uri, basename):
    """Sibling audio URI / path. See ``_sibling_uri_or_path``."""
    return _sibling_uri_or_path(lift_path_or_uri, 'audio', basename)


def image_uri_for(lift_path_or_uri, basename):
    """Sibling image URI / path. See ``_sibling_uri_or_path``."""
    return _sibling_uri_or_path(lift_path_or_uri, 'images', basename)


class MediaHandle(LiftHandle):
    """Read/write wrapper for sibling audio / image files served by
    the same provider that serves the LIFT file. Same shape as
    ``LiftHandle`` plus a ``kind`` for log lines and a write-mode
    gate (images are read-only from peers — the daemon owns image
    additions; peers only display).

    ``MediaHandle('content://.../audio/foo.wav', 'audio')`` →
    read+write. ``MediaHandle('content://.../images/foo.png',
    'image')`` → read-only.

    Compose the path/URI with ``audio_uri_for`` / ``image_uri_for``
    given the picker-emitted LIFT path/URI plus a basename."""

    def __init__(self, path_or_uri, kind):
        if kind not in ('audio', 'image'):
            raise ValueError(f'kind must be audio or image, got {kind!r}')
        super().__init__(path_or_uri)
        self.kind = kind

    def open_write(self):
        if self.kind == 'image':
            raise PermissionError(
                'images are read-only from peers; the daemon owns '
                'image additions. If you really need this, expose a '
                'server endpoint.')
        return super().open_write()

    def __repr__(self):
        kind = 'uri' if self.is_uri else 'path'
        return (f'<MediaHandle kind={self.kind!r} {kind}='
                f'{self.path_or_uri!r}>')


def _open_content_uri(uri, mode):
    """Open a ``content://`` URI through the host Activity's
    ContentResolver. Detaches the underlying ``ParcelFileDescriptor``
    so the returned Python file owns the FD lifetime via
    ``os.fdopen``. Raises ``IOError`` if the provider can't supply a
    descriptor (URI not granted, missing file, permission denied)."""
    from jnius import autoclass
    PythonActivity = autoclass('org.kivy.android.PythonActivity')
    Uri = autoclass('android.net.Uri')
    activity = PythonActivity.mActivity
    if activity is None:
        raise IOError(
            'no Activity available to open content URI '
            f'{uri!r} — not running in an Android Activity context?')
    resolver = activity.getContentResolver()
    java_uri = Uri.parse(uri)
    java_mode = 'r' if 'r' in mode else 'w'
    pfd = resolver.openFileDescriptor(java_uri, java_mode)
    if pfd is None:
        raise IOError(f'openFileDescriptor returned null for {uri!r} '
                      f'(mode={java_mode!r})')
    fd = pfd.detachFd()
    if java_mode == 'w':
        # Defensive: ParcelFileDescriptor.parseMode("w") should set
        # MODE_TRUNCATE → O_TRUNC at open time, but empirically that
        # truncation does not always fire across the ContentProvider
        # boundary (a write shorter than the existing file leaves the
        # original tail intact, producing a doubled / mid-file </lift>
        # corruption). ftruncate to 0 here unconditionally so a partial
        # write can never resurface old content. Cheap (no I/O on a
        # fresh-or-already-zero file).
        try:
            os.ftruncate(fd, 0)
        except OSError as ex:
            # If ftruncate fails (rare — maybe the FD is read-only or
            # the underlying fs doesn't support it), close the FD and
            # surface as IOError so the caller doesn't end up writing
            # into a possibly-tail-contaminated file.
            try:
                os.close(fd)
            except OSError:
                pass
            raise IOError(
                f'ftruncate failed for {uri!r}: {ex}') from ex
    return os.fdopen(fd, 'rb' if java_mode == 'r' else 'wb')
