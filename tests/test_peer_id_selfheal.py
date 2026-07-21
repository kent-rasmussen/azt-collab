"""peer_id cert/key mismatch self-heal (0.54.15, LAN audit F13).

The identity pair is two separate atomic writes; a crash or racing
second daemon between them leaves new-key + old-cert, which
previously survived ``ensure()``'s per-file parsing and killed
``ssl.load_cert_chain`` later with KEY_VALUES_MISMATCH (field
2026-07-21: surfaced to the user as "not on the same network").
``ensure()`` now cross-checks the pair and re-issues the cert from
the existing key, preserving peer_id.
"""

import pytest

pytest.importorskip('cryptography')

from azt_collabd import peer_id as pid


def test_mismatched_pair_selfheals_preserving_key(azt_home):
    first = pid.ensure()

    # Corrupt the pair the way the field does: overwrite the KEY
    # with a different generation's key, leave the old cert.
    key_pem, _cert_pem, _cert_der, pubkey_raw = pid._generate()
    with open(pid.key_path(), 'wb') as f:
        f.write(key_pem)

    healed = pid.ensure()
    # Identity follows the surviving key; cert re-issued to match,
    # so the fingerprint moves off the orphaned cert's value.
    assert healed['peer_id'] == pubkey_raw.hex()
    assert healed['peer_id'] != first['peer_id']
    assert healed['fp'] != first['fp']

    # Healed pair is stable: no re-issue on the next call.
    again = pid.ensure()
    assert again == healed


def test_matched_pair_untouched(azt_home):
    first = pid.ensure()
    assert pid.ensure() == first
