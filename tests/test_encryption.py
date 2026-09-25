"""Encryption: round trips, the envelope shape, tamper detection, key files, and
byte-compatibility with the shipped implementation (a fixed-key vector minted by it)."""

from __future__ import annotations

import base64
import json
import os

import pytest
from cryptography.exceptions import InvalidTag

from aaw_core.encryption import (
    KEY_BYTES,
    decrypt,
    decrypt_if_encrypted,
    encrypt,
    generate_key_b64,
    is_encrypted,
    load_or_create_key,
)

# Minted once by the original implementation with this fixed, non-secret key.
# If this test ever fails, the wire format has drifted and the phone apps would break.
COMPAT_KEY_B64 = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
COMPAT_PLAINTEXT = "Refactor the auth module and run the tests. ünïcödé ✓ \U0001f6f4"
COMPAT_VECTOR = (
    '{"iv":"OHxZD6qfOUqkNnVe",'
    '"ct":"4OFE1TJKprTFyIN0GXfX7QzlpBSCzQ6h8QzIIwla4hjRyzQItOma+YIwSEhaXHstWXuKV3XGpXQzZz283Nf4Bg==",'
    '"tag":"hW9EqXVCrXrsOZ5urDp4ow=="}'
)


def test_decrypts_vector_from_original_implementation():
    assert decrypt(COMPAT_VECTOR, COMPAT_KEY_B64) == COMPAT_PLAINTEXT


def test_round_trip_and_envelope_shape():
    key = generate_key_b64()
    ct = encrypt("hello, world", key)
    env = json.loads(ct)
    assert set(env) == {"iv", "ct", "tag"}
    assert len(base64.b64decode(env["iv"])) == 12
    assert len(base64.b64decode(env["tag"])) == 16
    assert ct == json.dumps(env, separators=(",", ":"))  # compact, no spaces
    assert decrypt(ct, key) == "hello, world"


def test_each_encryption_uses_a_fresh_nonce():
    key = generate_key_b64()
    assert encrypt("same", key) != encrypt("same", key)


def test_is_encrypted():
    key = generate_key_b64()
    assert is_encrypted(encrypt("x", key))
    assert not is_encrypted("plain text")
    assert not is_encrypted('{"not": "an envelope"}')
    assert not is_encrypted("{broken json")
    assert not is_encrypted(None)
    assert not is_encrypted(42)


def test_tampering_and_wrong_key_are_rejected():
    key = generate_key_b64()
    env = json.loads(encrypt("secret", key))
    tampered = dict(env, ct=base64.b64encode(b"\x00" * len(base64.b64decode(env["ct"]))).decode())
    with pytest.raises(InvalidTag):
        decrypt(json.dumps(tampered), key)
    with pytest.raises(InvalidTag):
        decrypt(encrypt("secret", key), generate_key_b64())


def test_decrypt_if_encrypted_is_fail_soft():
    key = generate_key_b64()
    ct = encrypt("payload", key)
    assert decrypt_if_encrypted(ct, key) == "payload"
    assert decrypt_if_encrypted("plain", key) == "plain"  # plaintext passes through
    assert decrypt_if_encrypted(ct, None) == ct  # no key: leave the envelope alone
    assert decrypt_if_encrypted(ct, generate_key_b64()) == ct  # wrong key: leave it alone


def test_load_or_create_key(tmp_path):
    path = tmp_path / "state" / "session.key"
    key = load_or_create_key(path)
    assert len(base64.b64decode(key)) == KEY_BYTES
    assert path.read_text() == key
    assert oct(path.stat().st_mode & 0o777) == oct(0o600)
    assert load_or_create_key(path) == key  # stable on reload
    path.write_text(base64.b64encode(b"short").decode())
    with pytest.raises(ValueError):
        load_or_create_key(path)


def test_key_generation_is_random():
    assert generate_key_b64() != generate_key_b64()
    assert len(base64.b64decode(generate_key_b64())) == KEY_BYTES
    assert os.urandom  # the source of randomness we rely on
