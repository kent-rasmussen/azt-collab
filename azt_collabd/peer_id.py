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


def _generate():
    """Generate a fresh ed25519 keypair + self-signed X.509 cert.
    Returns ``(key_pem, cert_pem, cert_der, pubkey_raw)``. Raises
    ``ImportError`` if ``cryptography`` is unavailable; callers
    treat that as "LAN sync not available on this platform"."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    import datetime

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
    cert = (
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
        .sign(private_key=key, algorithm=None)
    )

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
    key = serialization.load_pem_private_key(key_pem, password=None)
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _pem_to_der(cert_pem):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    cert = x509.load_pem_x509_certificate(cert_pem)
    return cert.public_bytes(serialization.Encoding.DER)


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
            except ImportError as ex:
                raise RuntimeError(
                    f'cryptography unavailable: {ex!r}') from ex
            except Exception as ex:
                raise RuntimeError(
                    f'peer_id files unreadable or corrupt: '
                    f'{ex!r}') from ex
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
