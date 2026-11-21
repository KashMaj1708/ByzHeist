"""Ed25519 signing/verification (PyNaCl) and canonical JSON serialization."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey


def generate_keypair() -> tuple[SigningKey, VerifyKey]:
    """Generate a fresh Ed25519 (SigningKey, VerifyKey) pair."""
    sk = SigningKey.generate()
    return sk, sk.verify_key


def canonical_bytes(payload: Any) -> bytes:
    """Serialize payload deterministically with sorted keys and compact separators."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def sha256_hex(payload: bytes | Any) -> str:
    """Return hex SHA-256 digest of raw bytes or canonical JSON of payload."""
    raw = payload if isinstance(payload, (bytes, bytearray)) else canonical_bytes(payload)
    return hashlib.sha256(raw).hexdigest()


def sign_payload(signing_key: SigningKey, payload: Mapping[str, Any]) -> str:
    """Sign canonical JSON bytes of payload and return hex-encoded Ed25519 signature."""
    signed = signing_key.sign(canonical_bytes(payload))
    return signed.signature.hex()


def verify_payload(
    verify_key: VerifyKey,
    payload: Mapping[str, Any],
    signature_hex: str,
) -> bool:
    """Verify hex-encoded Ed25519 signature against canonical JSON bytes of payload."""
    if not signature_hex:
        return False
    try:
        sig_bytes = bytes.fromhex(signature_hex)
        verify_key.verify(canonical_bytes(payload), sig_bytes)
        return True
    except (BadSignatureError, ValueError, TypeError):
        return False


def ledger_hash(balances: Mapping[str, int]) -> str:
    """Compute canonical SHA-256 hash of account balances."""
    return sha256_hex(dict(balances))
