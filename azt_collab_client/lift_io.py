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
import secrets
import threading


_CONTENT_PREFIX = 'content://'
# Provider authority of the standalone server APK. Hard-coded
# because the suite has exactly one canonical authority for
# every install (it's a signature-protected permission constant)
# and peers building CAWL URIs from (langcode, basename) need it
# without a round-trip to discover.
_AZTCOLLAB_AUTHORITY = 'org.atoznback.aztcollab'


def is_content_uri(s):
    """True iff ``s`` is a ``content://`` URI string. Useful for
    code paths that need to branch before instantiating a handle
    (e.g. registering a project: working_dir on a URI is the URI
    itself, on a path it's ``os.path.dirname``)."""
    return isinstance(s, str) and s.startswith(_CONTENT_PREFIX)


# ── Process-local serialization for writes to the same target ────────────
#
# Two threads of the same peer calling ``open_write`` on the same
# path can race at the byte level: each opens an independent FD,
# each ``ftruncate(0)``s the underlying file, each writes from
# offset 0, the kernel interleaves their writes. The resulting
# bytes are a mishmash that LOOKS like LIFT in places but has
# torn-tag boundaries — and the daemon's next merge attempt
# parses the torn XML as empty (via the pre-0.35.2 silent
# ``ParseError`` mask) and produces a catastrophic merge result.
# Field-reported 2026-05-12 (``baf`` repro in NOTES_TO_DAEMON.md,
# closed); the malformed bytes showed two glosses with the same
# ``lang`` overlapping mid-stream because two ``open_write``
# calls truncated and wrote concurrently.
#
# The path-keyed lock below serializes write paths within the
# peer process: two threads calling ``open_write`` on the same
# target queue rather than race. Reentrant so a single thread
# that recursively reaches ``open_write`` for the same path
# doesn't deadlock against itself.
#
# This DOES NOT cover cross-process races (peer vs daemon, peer
# vs another peer process on the same shared filesystem). For
# that, use ``LiftHandle.atomic_open_write`` (filesystem path
# only) which writes to a sibling tempfile and renames over the
# destination via ``os.replace`` — atomic on POSIX/NTFS within
# a single filesystem.

_write_locks = {}
_write_locks_registry_lock = threading.Lock()


def _path_lock(key):
    """Return the (reentrant) lock associated with this target
    path/URI, creating it on first use. Process-local."""
    with _write_locks_registry_lock:
        lock = _write_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _write_locks[key] = lock
        return lock


class _LockedWriteFile:
    """File-like proxy holding a path-keyed write lock. Releases
    the lock when ``close`` runs, even if the underlying file's
    ``close`` raises. Used by ``LiftHandle.open_write`` to
    serialize concurrent in-process writes to the same target."""

    __slots__ = ('_file', '_lock', '_closed')

    def __init__(self, file_obj, lock):
        self._file = file_obj
        self._lock = lock
        self._closed = False

    def write(self, data):
        return self._file.write(data)

    def flush(self):
        flush_fn = getattr(self._file, 'flush', None)
        if flush_fn is not None:
            flush_fn()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._file.close()
        finally:
            self._lock.release()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __getattr__(self, name):
        # Delegate other file-like methods (seekable, fileno,
        # writable, etc.) to the wrapped object so callers that
        # treat us as a regular binary file still work.
        return getattr(self._file, name)


class _AtomicWriteFile:
    """Atomic write via tempfile + ``os.replace``. Returns a
    file-like context manager. On clean exit, atomically replaces
    the destination with the tempfile. On exception, removes the
    tempfile and leaves the destination untouched.

    Filesystem path only — Android's ContentResolver has no clean
    atomic-rename semantic for arbitrary Provider URIs.
    ``LiftHandle.atomic_open_write`` on a URI uses
    ``_UriAtomicWriteFile`` instead, which ships the bytes to the
    daemon's ``/v1/projects/<lang>/atomic_commit`` RPC and the
    daemon performs the tempfile+rename in its own process.

    Two concurrent ``atomic_open_write`` calls on the same
    destination are safe: each writes to its own random-suffixed
    tempfile, and whichever ``os.replace`` runs last wins. The
    destination is *always* a complete copy of one of the
    versions, never a torn mix."""

    __slots__ = ('_dest', '_tmp', '_file', '_closed', '_committed')

    def __init__(self, dest_path):
        self._dest = dest_path
        # Random suffix per call so concurrent atomic writes
        # don't collide on the tempfile itself.
        suffix = secrets.token_hex(8)
        self._tmp = f'{dest_path}.tmp.{os.getpid()}.{suffix}'
        self._file = None
        self._closed = False
        self._committed = False

    def __enter__(self):
        parent = os.path.dirname(self._dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._file = open(self._tmp, 'wb')
        return self

    def write(self, data):
        return self._file.write(data)

    def flush(self):
        if self._file is not None:
            self._file.flush()

    def commit(self):
        """Atomic rename of the tempfile over the destination.
        Called automatically by ``__exit__`` on clean exit;
        exposed so peers that want to control flush ordering
        (e.g., ``os.fsync(fd)`` for durability) can do so
        explicitly before the rename."""
        if self._committed:
            return
        if self._file is not None:
            self._file.close()
            self._file = None
        os.replace(self._tmp, self._dest)
        self._committed = True

    def close(self):
        """Close the tempfile. If commit hasn't run (exception
        path), remove the tempfile so we don't leave dangling
        ``.tmp.<pid>.<rand>`` files in the project directory."""
        if self._closed:
            return
        self._closed = True
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None
        if not self._committed:
            try:
                os.unlink(self._tmp)
            except OSError:
                pass

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None:
                self.commit()
        finally:
            self.close()
        return False  # don't suppress

    def __getattr__(self, name):
        if self._file is None:
            raise AttributeError(name)
        return getattr(self._file, name)


def _parse_provider_uri(uri):
    """Split a ``content://<authority>/<lang>/<rest>`` URI into
    ``(langcode, rel_path)``. Raises ``ValueError`` if the URI
    shape isn't recognized."""
    if not isinstance(uri, str) or not uri.startswith(_CONTENT_PREFIX):
        raise ValueError(f'not a content:// URI: {uri!r}')
    rest = uri[len(_CONTENT_PREFIX):]
    slash = rest.find('/')
    if slash < 0:
        raise ValueError(f'no path component: {uri!r}')
    path = rest[slash + 1:].split('?', 1)[0]
    parts = path.split('/')
    if len(parts) < 2 or not parts[0]:
        raise ValueError(f'malformed URI: {uri!r}')
    return parts[0], '/'.join(parts[1:])


class _UriAtomicWriteFile:
    """Atomic write over a ``content://`` URI, two-phase.

    Phase 1 (bytes): on commit, generate a per-write hex token,
    open ``content://<auth>/<lang>/_atomic_pending/<token>`` via
    ``ContentResolver.openFileDescriptor`` for write, and ship the
    buffered bytes through that kernel FD. No Binder size cap on
    the FD path — a 3+ MB LIFT crosses fine.

    Phase 2 (finalize): call ``POST
    /v1/projects/<lang>/atomic_finalize`` with the token + final
    rel_path. The daemon atomic-renames the pending scratch file
    into the canonical location under ``project_lock``, so
    concurrent writers (peer-vs-peer, peer-vs-daemon's merge
    output) can't tear the destination.

    Why two-phase: the previous single-RPC ``atomic_commit`` path
    shipped the full payload as base64 inside the Bundle that
    ``ContentResolver.call`` shuttles over Binder. Binder caps
    individual transactions at ~1 MB, so any LIFT bigger than
    ~700 KB (base64 inflates 1.33x) silently failed the round
    trip — the daemon never even saw the request. Splitting the
    bytes onto the FD path closes that gap while preserving the
    atomicity contract (rename is still serialized via project_lock
    on the daemon).

    On exception, the buffered bytes are dropped and neither phase
    runs; the destination is untouched. If phase 1 succeeds but
    phase 2 fails the pending scratch file may linger under
    ``.azt_atomic_pending/`` on the daemon — the daemon best-
    effort cleans it on finalize-failure but a peer crash between
    phases would leave a stranded file.

    Memory: still holds the full file bytes in a peer-side BytesIO
    until commit. For LIFT (tens of MB at worst) this is fine.
    Streaming directly from the BytesIO into the FD avoids a
    second copy.

    Backward compatibility: pre-0.41.7 daemons return 404 / not_found
    on the ``atomic_finalize`` route. ``commit`` falls back to the
    old single-RPC ``atomic_commit_bytes`` path in that case (which
    works for small payloads against any daemon ≥ 0.36.0).
    """

    __slots__ = ('_uri', '_buf', '_lock', '_lock_held', '_committed')

    def __init__(self, uri):
        from io import BytesIO
        self._uri = uri
        self._buf = BytesIO()
        # Same path-keyed lock plain ``open_write`` uses, so
        # in-process callers serialize without making any of them
        # wait for the network round-trip.
        self._lock = _path_lock(uri)
        self._lock_held = False
        self._committed = False

    def __enter__(self):
        self._lock.acquire()
        self._lock_held = True
        return self

    def write(self, data):
        return self._buf.write(data)

    def flush(self):
        pass   # buffered write — nothing to flush before commit

    def commit(self):
        """Ship the buffered bytes to the daemon and atomic-rename.
        Raises ``IOError`` on transport / finalize failure; the
        destination is untouched in that case because the daemon
        only swaps the inode after the rename succeeds."""
        if self._committed:
            return
        self._committed = True
        # Deferred imports avoid a circular dependency between
        # ``azt_collab_client.__init__`` (which imports lift_io)
        # and the wrappers (which live there).
        from . import (atomic_commit_bytes,
                       atomic_finalize_pending)
        from . import status as _S
        langcode, rel_path = _parse_provider_uri(self._uri)
        data = self._buf.getvalue()
        self._buf = None

        # Phase 1: ship bytes via FD to per-token scratch path.
        # The FD route has no Binder size cap, so a 3+ MB LIFT
        # crosses cleanly.
        import secrets
        token = secrets.token_hex(16)
        pending_uri = (f'{_CONTENT_PREFIX}{_AZTCOLLAB_AUTHORITY}/'
                       f'{langcode}/_atomic_pending/{token}')
        try:
            with _open_content_uri(pending_uri, 'w') as f:
                f.write(data)
        except IOError as ex:
            # FD-write path failed. Either the daemon's
            # _resolve_path doesn't know the _atomic_pending route
            # (pre-0.41.7 daemon) or the openFile itself failed.
            # Fall back to the legacy single-RPC path — works for
            # small payloads against any 0.36.0+ daemon.
            result = atomic_commit_bytes(langcode, rel_path, data)
            if not result.has(_S.ATOMIC_COMMITTED):
                raise IOError(
                    f'atomic_commit({self._uri!r}, {rel_path!r}) '
                    f'failed (FD path: {ex!r}; '
                    f'fallback RPC: {result.codes()!r})')
            return

        # Phase 2: atomic-rename via small RPC.
        result = atomic_finalize_pending(langcode, rel_path, token)
        if result.has(_S.ATOMIC_COMMITTED):
            return
        # Finalize endpoint missing → pre-0.41.7 daemon. The
        # phase-1 write left a scratch file the daemon won't know
        # how to consume; the legacy single-RPC path is our
        # fallback. (No need to clean up the scratch file from the
        # client — the daemon's pending-cleanup logic / a future
        # GC sweep handles strays.) Detect the missing endpoint by
        # the SERVER_ERROR status with error='not_found'.
        codes = result.codes()
        if 'SERVER_ERROR' in codes:
            # Try legacy path; if it also fails the finalize error
            # is the more informative one to surface.
            fallback = atomic_commit_bytes(langcode, rel_path, data)
            if fallback.has(_S.ATOMIC_COMMITTED):
                return
        # Surface the daemon's diagnostic params alongside the code
        # — without this, ``failed: ['SERVER_ERROR']`` tells us a
        # call failed but not *why*. The result carries
        # ``{error: '<daemon-side reason>'}`` on the SERVER_ERROR
        # status; include it so logcat shows the actual cause
        # (project_not_found, pending_not_found, path_rejected,
        # filesystem-error string, etc.).
        detail = ''
        for status in result.statuses:
            params = getattr(status, 'params', {}) or {}
            err = params.get('error') or params.get('detail')
            if err:
                detail = f' ({status.code}: {err})'
                break
        raise IOError(
            f'atomic_commit({self._uri!r}, {rel_path!r}) failed: '
            f'{codes!r}{detail}')

    def close(self):
        """Release the path lock. Idempotent. Does NOT commit on
        its own — commit happens via ``__exit__`` on the clean
        path. Calling ``close()`` directly without going through
        the context manager is unusual; callers should use
        ``with handle.atomic_open_write() as f``."""
        if self._lock_held:
            self._lock_held = False
            self._lock.release()

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None:
                self.commit()
            else:
                # Exception in the with body — drop the buffer and
                # do NOT ship the RPC. Destination stays untouched,
                # same contract as ``_AtomicWriteFile``.
                self._buf = None
                self._committed = True
        finally:
            self.close()
        return False


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
        usable as a context manager.

        Serialized within the peer process via a path-keyed lock
        (since 0.35.4): two threads calling ``open_write`` on the
        same path queue rather than race. Pre-0.35.4 the lock
        didn't exist and rapid back-to-back writes (e.g., a
        recorder serializing the LIFT twice in quick succession
        after two audio captures) could interleave at the byte
        level — producing malformed XML that the daemon's next
        merge then misparsed catastrophically. NOTES_TO_DAEMON.md
        2026-05-12 ``baf`` repro is the case that motivated this
        serialization; the malformed bytes showed two same-lang
        gloss elements with one's text mid-stream embedded in the
        other's, the signature of two FDs writing concurrently
        from offset 0.

        The process-local lock does NOT cover cross-process races
        (peer vs daemon, peer vs another peer process on a shared
        filesystem). For full atomicity on filesystem paths, use
        ``atomic_open_write``."""
        lock = _path_lock(self.path_or_uri)
        lock.acquire()
        try:
            if self.is_uri:
                file_obj = _open_content_uri(self.path_or_uri, 'w')
            else:
                file_obj = open(self.path_or_uri, 'wb')
            return _LockedWriteFile(file_obj, lock)
        except BaseException:
            lock.release()
            raise

    def atomic_open_write(self):
        """Atomic write context manager. On clean exit, the
        destination is atomically replaced with the bytes the
        caller wrote into the file-like; on exception, the
        destination is untouched.

        Two transports, same contract:

        - **Filesystem path**: ``_AtomicWriteFile`` writes a
          sibling tempfile and renames over the destination via
          ``os.replace``.
        - **``content://`` URI** (daemon 0.36.0+):
          ``_UriAtomicWriteFile`` buffers in memory and ships the
          bytes to ``/v1/projects/<lang>/atomic_commit``; the
          daemon does the tempfile+rename in its own process,
          serialized via ``project_lock``. Without the RPC the
          URI path would have to fall back to the lock-only
          ``open_write`` (same-process safe, cross-process race
          prone) — which is what pre-0.36.0 clients did.

        In both cases two concurrent ``atomic_open_write`` calls
        on the same destination are safe: whichever rename runs
        last wins, and the destination is *always* a complete
        copy of one of the versions, never torn."""
        if self.is_uri:
            return _UriAtomicWriteFile(self.path_or_uri)
        return _AtomicWriteFile(self.path_or_uri)

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
    ``LiftHandle`` plus a ``kind`` for log lines and error
    messages.

    Both ``kind='audio'`` and ``kind='image'`` are writable via the
    provider's ``openFile(mode='w')`` (which mkdir-p's the parent
    dir on first write — see the daemon's
    ``android_cp/service.py:_resolve_path`` whitelist
    ``_ALLOWED_MEDIA_DIRS``). 0.18.0 through 0.35.1 raised
    ``PermissionError`` on image writes under an unsubstantiated
    "daemon owns image additions" rule; tracing the history showed
    no concern actually driving that gate, and the symmetry with
    audio (same provider, same URI shape, same race semantics) is
    cleaner. Peers attaching an image to a LIFT entry write the
    bytes through this handle and the ``<illustration href=…>``
    ref through ``LiftHandle.open_write`` — same two-write pattern
    as audio + its LIFT-side ref. Binary-conflict resolution on
    collision lives in ``repo._merge_diverged``'s non-LIFT
    modify-modify branch (surfaces as ``non-lift-modify-modify``
    Conflict; merging-side wins on disk, both versions remain in
    git history).

    ``MediaHandle('content://.../audio/foo.wav', 'audio')`` /
    ``MediaHandle('content://.../images/foo.png', 'image')`` —
    both read+write.

    Compose the path/URI with ``audio_uri_for`` / ``image_uri_for``
    given the picker-emitted LIFT path/URI plus a basename."""

    def __init__(self, path_or_uri, kind):
        if kind not in ('audio', 'image'):
            raise ValueError(f'kind must be audio or image, got {kind!r}')
        super().__init__(path_or_uri)
        self.kind = kind

    def __repr__(self):
        kind = 'uri' if self.is_uri else 'path'
        return (f'<MediaHandle kind={self.kind!r} {kind}='
                f'{self.path_or_uri!r}>')


class CAWLHandle:
    """Read-only binary handle for a CAWL image served by the
    daemon, identified by ``(langcode, basename)``.

    The daemon owns the canonical image cache under
    ``$AZT_HOME/cawl/<owner>/<repo>/images/<basename>``, keyed by
    the image_repo slug the project's ``cawl_image_repo`` field
    resolves to (per-project value with daemon-global fallback).
    Peers don't write CAWL images — they're shared infrastructure
    served by the daemon, fetched lazily from
    ``raw.githubusercontent.com`` on first access — so this handle
    is read-only.

    Transport selection happens inside ``open_read``:

    - **Android** opens
      ``content://org.atoznback.aztcollab/<lang>/cawl/images/<basename>``
      via ContentResolver. Kernel-managed FD; zero-copy from the
      daemon's cache file. The provider's openFile path triggers
      the lazy fetch via ``_resolve_cawl_path`` →
      ``cawl.get_image_path``.
    - **Desktop** issues a loopback ``GET
      /v1/projects/<lang>/cawl/images/<basename>`` and returns an
      ``io.BytesIO`` wrapping the response bytes. The daemon
      handler triggers the same lazy fetch. The in-memory buffer
      is fine — CAWL images are typically < 200 KB; using
      ``io.BytesIO`` keeps the call-site API symmetric (a file-
      like usable as a context manager).

    Raises ``FileNotFoundError`` when the image isn't in the
    repo or the fetch failed and no cached copy exists. Peers
    should treat that as "no illustration for this entry" — the
    same code path their pre-migration empty resolver dict
    triggered.

    Example::

        from azt_collab_client import CAWLHandle
        with CAWLHandle(langcode, 'cawl-1234.jpg').open_read() as f:
            png_bytes = f.read()
            # decode / display
    """

    __slots__ = ('langcode', 'rel_path')

    def __init__(self, langcode, rel_path):
        if not langcode or not isinstance(langcode, str):
            raise ValueError(
                f'CAWLHandle needs a langcode, got {langcode!r}')
        if not rel_path or not isinstance(rel_path, str):
            raise ValueError(
                f'CAWLHandle needs a rel_path, got {rel_path!r}')
        # Reject path-traversal at construction time. Nested
        # paths (``0001_body/foo.png``) are fine — CAWL repos
        # commonly use category subdirs — but absolute paths
        # and ``..``/``.`` components are rejected here so the
        # ``CAWLHandle('x', '../../etc/passwd')`` mistake fails
        # loud at the call site rather than silently at the
        # daemon.
        if '\\' in rel_path or rel_path.startswith('/'):
            raise ValueError(f'invalid CAWL rel_path: {rel_path!r}')
        for seg in rel_path.split('/'):
            if not seg or seg in ('.', '..'):
                raise ValueError(
                    f'invalid CAWL rel_path component: {rel_path!r}')
        self.langcode = langcode
        self.rel_path = rel_path

    @property
    def basename(self):
        """Back-compat alias for ``rel_path``. Pre-0.41.1 the
        attribute was named ``basename`` under a flat-filename
        assumption. The on-disk path is rel_path-shaped now;
        keeping this read-only alias lets any peer that read
        ``handle.basename`` (e.g. for log lines) keep working."""
        return self.rel_path

    def open_read(self):
        """Open for binary read. Returns a file-like usable as a
        context manager. Closing closes the underlying descriptor
        / releases the in-memory buffer."""
        # Kivy-free platform probe (0.53.1) — importing Kivy from a
        # non-Kivy host lets its argv parser kill the process; see
        # azt_collab_client/_platform.py.
        from ._platform import platform as _plat
        platform = _plat()
        # URL-encode each path component for transit so that
        # spaces, commas, parentheses, etc. that CAWL filenames
        # commonly contain don't break URI / URL parsing. The
        # slashes between components stay intact (``safe='/'``)
        # so the daemon sees the same component structure.
        from urllib.parse import quote as _urlquote
        encoded = _urlquote(self.rel_path, safe='/')
        if platform == 'android':
            uri = (f'{_CONTENT_PREFIX}{_AZTCOLLAB_AUTHORITY}/'
                   f'{self.langcode}/cawl/images/{encoded}')
            return _open_content_uri(uri, 'r')
        return _http_get_cawl_image(self.langcode, encoded)

    def __repr__(self):
        return (f'<CAWLHandle langcode={self.langcode!r} '
                f'rel_path={self.rel_path!r}>')


def _cawl_index_via_fd(langcode):
    """Read the CAWL index JSON for ``langcode`` through the
    ContentProvider's file route, bypassing the JSON-RPC path.

    Why this exists: ``GET /v1/projects/<lang>/cawl/index`` over
    ContentResolver.call() ships the daemon's response as a
    Bundle through Binder, which caps individual transactions at
    ~1 MB. A populated CAWL index (5000+ entries with long
    GitHub raw-content URLs) blows past that cap, the Bundle
    silently fails to traverse the boundary, the peer's wrapper
    catches the resulting Java exception as ``ServerUnavailable``
    and returns ``{}``. The daemon-side success log fires (the
    handler ran) but the peer reads empty — the diagnostic gap
    that hid this bug pre-0.41.2.

    The fix is to read the on-disk cache file directly via
    ``openFileDescriptor``, which uses a kernel FD and has no
    Binder size cap. The daemon's ContentProvider already
    publishes ``<lang>/cawl/index.json`` as a file route
    (``service.py:_resolve_cawl_path``), so peer-side this is
    just a ``ContentResolver.openFileDescriptor`` against the
    same URI shape ``CAWLHandle`` uses for image binaries.

    Returns the parsed index dict. Raises ``IOError`` if the
    file route can't open (URI grant missing, daemon process
    not up). Caller (``cawl_index``) catches and returns ``{}``
    to preserve the wrapper's empty-on-failure contract."""
    import json as _json
    uri = (f'{_CONTENT_PREFIX}{_AZTCOLLAB_AUTHORITY}/'
           f'{langcode}/cawl/index.json')
    with _open_content_uri(uri, 'r') as f:
        data = f.read()
    if not data:
        return {}
    try:
        return _json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, _json.JSONDecodeError):
        return {}


def _http_get_cawl_image(langcode, encoded_rel_path):
    """Loopback-HTTP path for ``CAWLHandle.open_read`` on desktop.

    ``encoded_rel_path`` is already URL-encoded by the caller
    (``CAWLHandle.open_read`` uses ``urllib.parse.quote(...,
    safe='/')`` to keep slashes intact but encode unsafe chars).

    Reads ``server.json`` for the bearer token, GETs the binary
    endpoint, returns an ``io.BytesIO``. Raises
    ``FileNotFoundError`` on 404 (image not available); raises
    ``ServerUnavailable`` on transport failure so peer code can
    branch on the same exception type the JSON-dispatch wrappers
    raise."""
    import io
    import json as _json
    import urllib.error
    import urllib.request
    from .paths import server_info_path
    from .transports import ServerUnavailable

    try:
        with open(server_info_path()) as f:
            info = _json.load(f)
    except (OSError, ValueError) as ex:
        raise ServerUnavailable(
            f'cannot read server.json: {ex}', kind='http')
    port = info.get('port')
    token = info.get('token')
    if not port or not token:
        raise ServerUnavailable(
            'server.json missing port/token', kind='http')
    url = (f'http://127.0.0.1:{port}/v1/projects/{langcode}/'
           f'cawl/images/{encoded_rel_path}')
    req = urllib.request.Request(
        url, headers={'Authorization': f'Bearer {token}'})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
    except urllib.error.HTTPError as ex:
        if ex.code == 404:
            raise FileNotFoundError(
                f'CAWL image not available: '
                f'{langcode}/{encoded_rel_path}') from ex
        raise IOError(
            f'CAWL image HTTP {ex.code}: {ex.reason}') from ex
    except (urllib.error.URLError, OSError) as ex:
        raise ServerUnavailable(
            f'connection failed: {ex}', kind='http')
    return io.BytesIO(data)


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
    from ._debug import first_try_log
    first_try_log('lift_io.openFileDescriptor.pre',
                  uri=uri, mode=java_mode)
    try:
        pfd = resolver.openFileDescriptor(java_uri, java_mode)
    except Exception as ex:
        first_try_log('lift_io.openFileDescriptor.raised',
                      uri=uri, mode=java_mode,
                      exc_type=type(ex).__name__, exc=str(ex))
        raise
    first_try_log('lift_io.openFileDescriptor.post',
                  uri=uri, mode=java_mode,
                  pfd_null=pfd is None)
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
