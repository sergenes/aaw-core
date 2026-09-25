"""AES-256-GCM encryption for prompt, response, and question fields.

Wire format (a JSON string), identical on the computer and in the phone apps:

    {"iv": "<base64 12-byte nonce>", "ct": "<base64 ciphertext>", "tag": "<base64 16-byte GCM tag>"}

The key is 32 random bytes, generated once on the computer, stored base64-encoded
in a 0600 file, and handed to the phone exactly once by QR code. It never travels
over the relay, so the relay only ever sees ciphertext.

Compatibility contract: the envelope above and this key encoding are what the
already-shipped phone apps read and write. Do not change them.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16


def generate_key_b64() -> str:
    """A fresh 256-bit key, base64-encoded (the form stored on disk and put in the QR)."""
    return base64.b64encode(os.urandom(KEY_BYTES)).decode()


def load_or_create_key(path: Path) -> str:
    """Return the base64 key stored at ``path``, creating it (mode 0600) if missing."""
    if path.exists():
        key_b64 = path.read_text().strip()
        if len(base64.b64decode(key_b64)) != KEY_BYTES:
            raise ValueError(f"{path}: not a {KEY_BYTES}-byte key")
        return key_b64
    path.parent.mkdir(parents=True, exist_ok=True)
    key_b64 = generate_key_b64()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key_b64)
    return key_b64


def encrypt(plaintext: str, key_b64: str) -> str:
    key = base64.b64decode(key_b64)
    nonce = os.urandom(NONCE_BYTES)
    ct_and_tag = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
    ct, tag = ct_and_tag[:-TAG_BYTES], ct_and_tag[-TAG_BYTES:]
    return json.dumps(
        {
            "iv": base64.b64encode(nonce).decode(),
            "ct": base64.b64encode(ct).decode(),
            "tag": base64.b64encode(tag).decode(),
        },
        separators=(",", ":"),
    )


def decrypt(ciphertext_json: str, key_b64: str) -> str:
    """Decrypt an envelope. Raises on a bad key, a tampered payload, or a malformed envelope."""
    d = json.loads(ciphertext_json)
    key = base64.b64decode(key_b64)
    nonce = base64.b64decode(d["iv"])
    ct = base64.b64decode(d["ct"]) + base64.b64decode(d["tag"])
    return AESGCM(key).decrypt(nonce, ct, None).decode()


def is_encrypted(value: object) -> bool:
    """True if ``value`` looks like an envelope rather than plaintext."""
    if not isinstance(value, str) or not value.startswith("{"):
        return False
    try:
        d = json.loads(value)
    except ValueError:
        return False
    return isinstance(d, dict) and "iv" in d and "ct" in d and "tag" in d


def decrypt_if_encrypted(value: str, key_b64: str | None) -> str:
    """Fail-soft decrypt: plaintext, a missing key, or an undecryptable envelope is returned as-is.

    This is how every client behaves, so old plaintext records and key mismatches never crash a feed.
    """
    if not key_b64 or not is_encrypted(value):
        return value
    try:
        return decrypt(value, key_b64)
    except (InvalidTag, ValueError, KeyError):
        # InvalidTag: wrong key or tampered payload. ValueError/KeyError: malformed envelope or key.
        return value
