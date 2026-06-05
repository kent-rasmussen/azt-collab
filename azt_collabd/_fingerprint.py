"""Content fingerprint of the deployed daemon code.

The motivating problem: `azt_collab_client/__version__` is a single
string that gets bumped on release, but it tells us nothing about
whether every individual file in the deployed bundle actually
matches the source tree that *claims* the same version. p4a's stale-
unpack issue (CLAUDE.md → `feedback_p4a_stale_unpack_on_apk_update`)
has bitten the suite multiple times: APK reinstall completes, the
daemon respawns reporting the new version via `/v1/health`, and the
running code is still the previous build's bytes because
`_python_bundle/` wasn't re-extracted. Without a content-derived
signal, the only way to know is to observe symptoms downstream
(behaviour doesn't match the expected new code).

This module computes a SHA-256 fingerprint of every Python module
in both daemon-side packages (`azt_collabd` and `azt_collab_client`).
The walker handles two on-disk layouts:

- **Source tree** (developer's checkout, desktop daemon): `.py`
  files. Hashes raw source bytes.
- **Deployed bundle** (Android, p4a-packaged): `.pyc` files (often
  without their `.py` source). Hashes the bytecode portion of the
  `.pyc` (skipping the 16-byte PEP-552 header so the timestamp /
  source-hash field doesn't perturb the value across rebuilds of
  the same source).

Two bundles with the same `__version__` but different file
contents produce different fingerprints. Within a given format
(`.py` only OR `.pyc` only), the fingerprint is stable across
rebuilds of identical source — so the practical comparison is:

- **Source vs. source**: deterministic across machines with the
  same checkout.
- **Deployed vs. deployed (before/after redeploy)**: changes iff
  any module's content changed. Use this to verify a redeploy
  actually picked up the latest source.
- **Source vs. deployed cross-format**: NOT directly comparable
  (`.py`-source hash ≠ `.pyc`-bytecode hash for the same module).
  Compare deployed-now to deployed-previous instead.

Hash inputs (sorted, deterministic):

  pkg_name/rel_module.ext\0file_content\0  (per module)

Hash output: 16-hex-char prefix of the full SHA-256 (64-bit
prefix — short enough to eyeball, long enough to be
collision-resistant in practice).

This module deliberately has no external dependencies beyond
stdlib so it can be imported as early as possible during boot
without dragging in azt_collabd's transitive deps. Since
0.50.31 (initial); .pyc walker added in 0.50.32 after the
Android deploy returned `SHA-256(b'')` because the walker only
accepted `.py`."""

from __future__ import annotations

import hashlib
import os
import sys


_FINGERPRINT_CACHE = None


# Hash output length (hex chars). 16 = 64-bit prefix, enough for
# eyeball comparison while keeping collision risk negligible in
# practice. Override via env var for forensic comparisons that
# need the full 64-hex SHA256.
_PREFIX_LEN = int(os.environ.get('AZT_FINGERPRINT_LEN', '16'))


# PEP-552 .pyc header is 16 bytes:
#   - 4 bytes magic number (Python version)
#   - 4 bytes flags
#   - 8 bytes timestamp + source size (or source hash)
# Stripping the header removes the timestamp drift between
# rebuilds of identical source.
_PYC_HEADER_LEN = 16


def _normalize_pyc_stem(fname):
    """Strip the `.cpython-XYZ[.opt-N]` suffix from a .pyc
    filename to recover the module's stem. Handles:

    - ``foo.pyc``              → ``foo``
    - ``foo.cpython-311.pyc``  → ``foo``
    - ``foo.cpython-311.opt-1.pyc`` → ``foo``
    """
    stem = fname[:-4]   # drop trailing '.pyc'
    dot = stem.find('.cpython-')
    if dot >= 0:
        stem = stem[:dot]
    return stem


def _collect_modules(root_dir):
    """Return dict ``rel_module → (abs_path, ext)`` for every
    Python module under *root_dir*. ``rel_module`` is the
    package-relative dotted-ish path (with `/` separators) WITHOUT
    extension. ``ext`` is ``'.py'`` or ``'.pyc'``.

    When both a ``.py`` and a ``.pyc`` exist for the same module
    (typical on a desktop with populated ``__pycache__/``), the
    ``.py`` wins so source-tree fingerprints stay stable as
    `__pycache__/` fills and empties.
    """
    modules = {}
    if not os.path.isdir(root_dir):
        return modules
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith('.'))
        rel_dir = os.path.relpath(dirpath, root_dir)
        # __pycache__/ holds .pyc files whose logical home is the
        # parent directory. Normalize so the rel_module path
        # doesn't include the __pycache__ segment — that's a
        # storage detail, not part of the module identity.
        in_pycache = (
            os.path.basename(rel_dir) == '__pycache__')
        rel_dir_logical = (
            os.path.dirname(rel_dir) if in_pycache else rel_dir)
        if rel_dir_logical == '.':
            rel_dir_logical = ''
        for fname in sorted(filenames):
            if fname.endswith('.py'):
                stem = fname[:-3]
                ext = '.py'
            elif fname.endswith('.pyc'):
                stem = _normalize_pyc_stem(fname)
                ext = '.pyc'
            else:
                continue
            if rel_dir_logical:
                rel_module = f'{rel_dir_logical}/{stem}'
            else:
                rel_module = stem
            rel_module = rel_module.replace(os.sep, '/')
            existing = modules.get(rel_module)
            # Prefer .py over .pyc when both exist.
            if existing is not None and existing[1] == '.py':
                continue
            modules[rel_module] = (
                os.path.join(dirpath, fname), ext)
    return modules


def _file_hash_content(abs_path, ext):
    """Return the canonical bytes to hash for one module file.

    For ``.py``: raw source bytes.
    For ``.pyc``: bytecode portion only (header stripped) so
    rebuild-timestamps don't perturb the hash.
    """
    try:
        with open(abs_path, 'rb') as f:
            data = f.read()
    except OSError as ex:
        # File disappeared between collection and read — extremely
        # unlikely (we're hashing our own deployed bundle) but
        # record a stable "missing" marker so the resulting hash
        # is reproducible across runs that hit the same problem.
        return f'<missing: {ex!r}>'.encode('utf-8')
    if ext == '.pyc' and len(data) > _PYC_HEADER_LEN:
        return data[_PYC_HEADER_LEN:]
    return data


def _hash_packages(root_dirs):
    """Compute the canonical SHA-256 hex of the Python module
    contents under *root_dirs*. Each root_dir contributes its
    own module set; modules from different roots can't collide
    because the package name is prefixed to each entry."""
    h = hashlib.sha256()
    for root_dir in root_dirs:
        pkg_name = os.path.basename(os.path.normpath(root_dir))
        modules = _collect_modules(root_dir)
        for rel_module in sorted(modules):
            abs_path, ext = modules[rel_module]
            entry_key = f'{pkg_name}/{rel_module}{ext}'
            h.update(entry_key.encode('utf-8'))
            h.update(b'\0')
            h.update(_file_hash_content(abs_path, ext))
            h.update(b'\0')
    return h.hexdigest()


def _default_root_dirs():
    """Return the on-disk directories whose Python content the
    fingerprint covers. Both daemon-side packages: the daemon
    itself (`azt_collabd`) and the client library it bundles
    (`azt_collab_client`). The client is included because peer-
    facing wrappers can drift independently of daemon internals,
    and the failure shape is identical (`__version__` updated, one
    or more files stale)."""
    here = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(here)   # one level up from azt_collabd/
    return [
        here,
        os.path.join(parent, 'azt_collab_client'),
    ]


def _count_module_files(root_dirs):
    """Diagnostic helper: total .py + .pyc module count across
    *root_dirs* after dedup. Surfaced in the boot-time log so an
    empty fingerprint (`SHA-256(b'')`) is distinguishable from a
    real hash that happens to start with the same prefix."""
    total = 0
    for root_dir in root_dirs:
        total += len(_collect_modules(root_dir))
    return total


def daemon_fingerprint():
    """Return the deployed daemon's content fingerprint as a short
    hex string (default 16 chars = 64-bit prefix). Cached after
    first call.

    Two bundles whose `__version__` is identical but whose
    contents differ — the canonical p4a-stale-unpack failure —
    produce different fingerprints. Two bundles built from the
    same source tree produce the same fingerprint regardless of
    `.pyc` rebuild timestamps (the PEP-552 header is stripped
    before hashing).

    Side effect on first call: prints a single
    ``[fingerprint] daemon=<hex> modules=<n>`` line to stderr so
    a daemon-log capture has the value even if the caller never
    hits ``/v1/health``. Idempotent — subsequent calls are pure.
    The module count makes ``SHA-256(b'')`` (no modules found,
    walker misconfigured for this layout) visibly distinct from
    a real hash."""
    global _FINGERPRINT_CACHE
    if _FINGERPRINT_CACHE is not None:
        return _FINGERPRINT_CACHE
    roots = _default_root_dirs()
    full = _hash_packages(roots)
    _FINGERPRINT_CACHE = full[:_PREFIX_LEN]
    n_modules = _count_module_files(roots)
    print(f'[fingerprint] daemon={_FINGERPRINT_CACHE} '
          f'modules={n_modules} '
          f'(sha256 prefix; full={full})',
          file=sys.stderr, flush=True)
    return _FINGERPRINT_CACHE


def module_fingerprints(root_dirs=None):
    """Per-module breakdown of the fingerprint. Returns a sorted
    dict mapping ``'<pkg_name>/<rel_module>.<ext>'`` to a 16-char
    SHA-256 prefix of that module's hash-input bytes (raw `.py`
    source, or `.pyc` bytecode with the PEP-552 header stripped).

    Use this when the combined ``daemon_fingerprint()`` changed
    between two deploys but the symptoms suggest only one file
    actually updated. Compare the deployed daemon's
    ``/v1/health.modules`` against the source-tree's
    ``python -m azt_collabd fingerprint --modules`` and the
    diverging entries point at the stale files. The combined
    fingerprint can shift from a single one-line edit (e.g.,
    `__version__` bump in `__init__.py`) without proving any
    other module is current; per-module hashes don't have that
    blind spot.

    Cross-format caveat applies module by module: a source-tree
    `.py` hash for ``azt_collabd/cawl.py`` differs from the
    deployed `.pyc` hash for the same module. Use deployed-vs-
    deployed comparisons to verify a redeploy actually picked up
    a specific file."""
    if root_dirs is None:
        root_dirs = _default_root_dirs()
    out = {}
    for root_dir in root_dirs:
        pkg_name = os.path.basename(os.path.normpath(root_dir))
        modules = _collect_modules(root_dir)
        for rel_module in sorted(modules):
            abs_path, ext = modules[rel_module]
            content = _file_hash_content(abs_path, ext)
            h = hashlib.sha256()
            h.update(content)
            out[f'{pkg_name}/{rel_module}{ext}'] = (
                h.hexdigest()[:_PREFIX_LEN])
    return out


def source_fingerprint(*, root=None):
    """Compute the fingerprint of an arbitrary source tree.
    *root* defaults to the repo root (one directory level up
    from this file). Pass an explicit path to fingerprint a
    different checkout.

    Used by the ``python -m azt_collabd fingerprint`` CLI helper
    so users can compare what they're about to ship against what
    the deployed daemon reports. Does NOT cache — each call walks
    the tree fresh, since the source can change between calls in
    a developer workflow.

    Cross-format comparison caveat: a source tree with `.py`
    files produces a different hash than a deployed bundle with
    only `.pyc` files for the same logical content (different
    formats). Use the source-vs-source comparison to confirm
    you're shipping what you expect, and use the deployed-now
    vs. deployed-before comparison to verify a redeploy actually
    took effect on the device."""
    if root is None:
        here = os.path.dirname(os.path.abspath(__file__))
        root = os.path.dirname(here)   # the repo root
    root = os.path.abspath(root)
    dirs = [
        os.path.join(root, 'azt_collabd'),
        os.path.join(root, 'azt_collab_client'),
    ]
    return _hash_packages(dirs)[:_PREFIX_LEN]
