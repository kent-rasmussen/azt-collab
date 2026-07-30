"""
Per-device identity for the LAN sync transport (parked design in
``docs/local_lan_sync_stub.md``, phase 1).

On first use, generates an ed25519 keypair and a self-signed X.509
cert with the ed25519 pubkey as the subject public key and a 100-year
validity. Identity is the *fingerprint of the cert*, pinned out of
band via QR at pairing time — CA validity is irrelevant.

Files (all in ``$AZT_HOME``):

  peer_id    PKCS#8 PEM-encoded ed25519 private key. 0600 on POSIX.
  peer.crt   X.509 PEM-encoded self-signed certificate.

The hex ``peer_id`` advertised on the wire is the lowercase hex
encoding of the 32-byte raw ed25519 public key (64 chars). The
``fp`` is the lowercase hex SHA-256 of the DER form of the cert
(64 chars), matching the standard "openssl x509 -fingerprint
-sha256" output minus the colons.

Lazy by design: ``ensure()`` runs on first read so an auto-spawned
daemon doesn't pay the cert-generation cost (~0.5-2 s) when the
user never touches LAN sync.
"""

import hashlib
import os
import sys
import tempfile
import threading

from . import paths as _paths


_LOCK = threading.Lock()


def _key_path():
    return os.path.join(_paths.azt_home(), 'peer_id')


def _cert_path():
    return os.path.join(_paths.azt_home(), 'peer.crt')


def _atomic_write(target_path, data, mode):
    target_dir = os.path.dirname(target_path) or '.'
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.peer_id.', suffix='.tmp',
                               dir=target_dir)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, target_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _backend():
    """Return ``cryptography.hazmat.backends.default_backend()`` if
    importable, else ``None``. Older ``cryptography`` versions
    require an explicit ``backend=`` kwarg on signing /loading
    calls; newer ones accept (and ignore) it. Passing it
    conditionally keeps both shapes working from one code path."""
    try:
        from cryptography.hazmat.backends import default_backend
        return default_backend()
    except Exception:
        return None


def _sign_cert(builder, key):
    """``CertificateBuilder.sign`` is the call that broke
    historically: pre-3.x cryptography required a positional
    ``backend`` and a non-None ``algorithm``; ed25519 keys want
    ``algorithm=None``. Try with backend first; on TypeError fall
    through to the kwarg-less newer signature."""
    backend = _backend()
    if backend is not None:
        try:
            return builder.sign(private_key=key, algorithm=None,
                                backend=backend)
        except TypeError:
            pass
    return builder.sign(private_key=key, algorithm=None)


def _load_pem_private_key(key_pem):
    from cryptography.hazmat.primitives import serialization
    backend = _backend()
    if backend is not None:
        try:
            return serialization.load_pem_private_key(
                key_pem, password=None, backend=backend)
        except TypeError:
            pass
    return serialization.load_pem_private_key(key_pem, password=None)


def _load_pem_x509_cert(cert_pem):
    from cryptography import x509
    backend = _backend()
    if backend is not None:
        try:
            return x509.load_pem_x509_certificate(cert_pem, backend=backend)
        except TypeError:
            pass
    return x509.load_pem_x509_certificate(cert_pem)


def _generate(key=None):
    """Generate an ed25519 keypair + self-signed X.509 cert — or,
    when *key* (a loaded Ed25519PrivateKey) is passed, re-issue the
    cert from that existing key (the KEY_VALUES_MISMATCH self-heal:
    preserves peer_id, only the cert/fingerprint change).
    Returns ``(key_pem, cert_pem, cert_der, pubkey_raw)``. Raises
    ``ImportError`` if ``cryptography`` is unavailable; callers
    treat that as "LAN sync not available on this platform"."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    import datetime

    if key is None:
        key = ed25519.Ed25519PrivateKey.generate()
    pubkey = key.public_key()
    pubkey_raw = pubkey.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, 'azt-collab-peer'),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(pubkey)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365 * 100))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None),
            critical=True,
        )
    )
    cert = _sign_cert(builder, key)

    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    return key_pem, cert_pem, cert_der, pubkey_raw


def _pubkey_raw_from_key_pem(key_pem):
    from cryptography.hazmat.primitives import serialization
    key = _load_pem_private_key(key_pem)
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _pem_to_der(cert_pem):
    from cryptography.hazmat.primitives import serialization
    cert = _load_pem_x509_cert(cert_pem)
    return cert.public_bytes(serialization.Encoding.DER)


def _pubkey_raw_from_cert_pem(cert_pem):
    from cryptography.hazmat.primitives import serialization
    cert = _load_pem_x509_cert(cert_pem)
    return cert.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _summary(pubkey_raw, cert_der, key_path, cert_path):
    return {
        'peer_id': pubkey_raw.hex(),
        'fp': hashlib.sha256(cert_der).hexdigest(),
        'key_path': key_path,
        'cert_path': cert_path,
    }


def ensure():
    """Idempotent first-use creation. Returns
    ``{'peer_id', 'fp', 'key_path', 'cert_path'}`` on success;
    raises ``RuntimeError`` if ``cryptography`` isn't importable
    or the existing files are corrupt.

    Serialized by an in-process lock so two near-simultaneous LAN
    endpoint hits don't race the generation."""
    with _LOCK:
        key_path = _key_path()
        cert_path = _cert_path()
        if os.path.exists(key_path) and os.path.exists(cert_path):
            try:
                with open(key_path, 'rb') as f:
                    key_pem = f.read()
                with open(cert_path, 'rb') as f:
                    cert_pem = f.read()
                pubkey_raw = _pubkey_raw_from_key_pem(key_pem)
                cert_der = _pem_to_der(cert_pem)
                cert_pub_raw = _pubkey_raw_from_cert_pem(cert_pem)
            except ImportError as ex:
                raise RuntimeError(
                    f'cryptography unavailable: {ex!r}') from ex
            except Exception as ex:
                raise RuntimeError(
                    f'peer_id files unreadable or corrupt: '
                    f'{ex!r}') from ex
            if cert_pub_raw != pubkey_raw:
                # KEY_VALUES_MISMATCH self-heal (0.54.15, audit
                # F13): both files parse, but they're from
                # DIFFERENT generations — the pair is two separate
                # atomic writes, so a crash or a racing second
                # daemon between them leaves new-key + old-cert.
                # Left alone, ``ssl.load_cert_chain`` later dies
                # with KEY_VALUES_MISMATCH deep inside whatever LAN
                # op first touches TLS, and the failure gets
                # misread as a network problem (field 2026-07-21,
                # Windows machine: surfaced as "not on the same
                # network"). Re-issue the cert from the EXISTING
                # key: peer_id (the pubkey) is preserved, so
                # peers.json entries keyed by it stay valid on both
                # sides; the cert FINGERPRINT still changes, so
                # peers that pinned the old cert will refuse this
                # device until re-paired — say so loudly.
                print(f'[peer_id] cert/key MISMATCH (pair from '
                      f'different generations) — re-issuing cert '
                      f'from the existing key. The fingerprint '
                      f'changes: previously paired peers must '
                      f're-pair with this device.',
                      file=sys.stderr, flush=True)
                key = _load_pem_private_key(key_pem)
                key_pem, cert_pem, cert_der, pubkey_raw = \
                    _generate(key=key)
                _atomic_write(key_path, key_pem, mode=0o600)
                _atomic_write(cert_path, cert_pem, mode=0o644)
            return _summary(pubkey_raw, cert_der, key_path, cert_path)

        try:
            key_pem, cert_pem, cert_der, pubkey_raw = _generate()
        except ImportError as ex:
            raise RuntimeError(
                f'cryptography unavailable; LAN sync identity '
                f'cannot be generated: {ex!r}') from ex

        _atomic_write(key_path, key_pem, mode=0o600)
        _atomic_write(cert_path, cert_pem, mode=0o644)
        print(f'[peer_id] generated identity at {key_path!r} / '
              f'{cert_path!r}', file=sys.stderr, flush=True)
        return _summary(pubkey_raw, cert_der, key_path, cert_path)


def peer_id_hex():
    """Hex ed25519 pubkey (64 chars) for the wire. ``''`` if the
    identity can't be created."""
    try:
        return ensure()['peer_id']
    except RuntimeError:
        return ''


_NONCES = {}                    # nonce hex → issued-at monotonic
_NONCE_TTL_S = 60.0
_NONCE_MAX = 512


def issue_nonce():
    """Mint a single-use challenge nonce; return it as hex (0.55.101).

    A signature alone would only prove the caller once held the key —
    replayable by anyone who captured a previous request. The nonce makes
    each proof fresh: we hand out a random value, the caller signs THAT,
    and we spend it on verification.

    Bounded two ways so a hostile or buggy peer can't grow this forever:
    entries expire after ``_NONCE_TTL_S``, and the map is capped at
    ``_NONCE_MAX`` (oldest dropped). Both matter — this is reachable by
    anything that can open a socket to the listener, before any identity
    check has happened."""
    import secrets
    import time as _t
    nonce = secrets.token_hex(32)
    now = _t.monotonic()
    with _LOCK:
        for k, at in list(_NONCES.items()):
            if now - at > _NONCE_TTL_S:
                _NONCES.pop(k, None)
        while len(_NONCES) >= _NONCE_MAX:
            oldest = min(_NONCES, key=lambda k: _NONCES[k])
            _NONCES.pop(oldest, None)
        _NONCES[nonce] = now
    return nonce


def spend_nonce(nonce_hex):
    """Consume a nonce. True iff it was outstanding and unexpired
    (0.55.101).

    Single-use by construction — removed on the first successful spend,
    so a replayed request fails even inside the TTL."""
    import time as _t
    key = str(nonce_hex or '')
    with _LOCK:
        at = _NONCES.pop(key, None)
    if at is None:
        return False
    return (_t.monotonic() - at) <= _NONCE_TTL_S


def sign_hex(message):
    """Sign *message* (bytes) with this device's ed25519 private key;
    return a hex signature, or ``''`` if we have no identity (0.55.101).

    Half of the fix for LAN identity being **forgeable**. Until now a
    caller merely stated its ``peer_id`` in the request body and the
    listener looked it up in ``peers.json`` — no signature, no nonce, no
    demonstration that it holds the private key. And ``peer_id`` is not
    a secret: it is advertised over mDNS so peers can find each other,
    so anything on the same network could read one and assert it. The
    effective access control was *"be on this LAN and know a paired
    peer_id."*

    `lan_listener._handle_hello_bodyauth` has carried the TODO for this
    since the body-auth path was written: *"a future-hardening pass
    should add a signature so we can cryptographically verify the body
    really came from the holder of that private key."*

    Cheap to do properly, because **``peer_id`` IS the raw ed25519
    public key** — verification needs no cert parsing, just
    ``Ed25519PublicKey.from_public_bytes(bytes.fromhex(peer_id))``.
    See ``verify_hex``."""
    try:
        info = ensure()
    except RuntimeError as ex:
        # Was a SILENT ''. The caller reads empty as "no identity" and
        # raises before posting, so a failure here looked identical to a
        # network problem — and grepping for the sign-failure line found
        # nothing because this path never wrote one (0.55.145).
        print(f'[peer-id] cannot sign: no LAN identity ({ex})',
              file=sys.stderr, flush=True)
        return ''
    try:
        with open(info['key_path'], 'rb') as fh:
            key = _load_pem_private_key(fh.read())
        return key.sign(message).hex()
    except Exception as ex:
        print(f'[peer-id] sign failed: {ex!r}', file=sys.stderr, flush=True)
        return ''


def verify_hex(peer_id_hex_str, message, sig_hex):
    """True iff *sig_hex* is a valid ed25519 signature over *message*
    by the holder of the private key behind *peer_id_hex_str*
    (0.55.101).

    ``peer_id`` is the raw 32-byte ed25519 pubkey as 64 hex chars, so
    this is a direct verify with no certificate involved.

    Returns False on ANY problem — malformed hex, wrong length, bad
    signature, missing ``cryptography``. **Never** raise: callers use
    this as an authorization gate, and an exception escaping into a
    request handler could turn a failed check into a 500 that some
    outer ``except`` treats as something other than 'denied'."""
    try:
        raw = bytes.fromhex(str(peer_id_hex_str or ''))
        if len(raw) != 32:
            return False
        sig = bytes.fromhex(str(sig_hex or ''))
    except Exception:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        pub = ed25519.Ed25519PublicKey.from_public_bytes(raw)
        pub.verify(sig, message)
        return True
    except Exception:
        return False


def cert_fp_hex():
    """SHA-256 fingerprint of the X.509 cert (DER), hex. ``''`` on
    error."""
    try:
        return ensure()['fp']
    except RuntimeError:
        return ''


def cert_path():
    """Absolute path to ``peer.crt`` for TLS load. ``''`` on error."""
    try:
        return ensure()['cert_path']
    except RuntimeError:
        return ''


def key_path():
    """Absolute path to the private-key PEM for TLS load. ``''`` on
    error."""
    try:
        return ensure()['key_path']
    except RuntimeError:
        return ''
