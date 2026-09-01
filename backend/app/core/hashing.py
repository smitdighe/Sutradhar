"""The one and only way this codebase computes a hash.

keccak256 is used rather than SHA-256 because these digests are compared
against values stored on an EVM chain, and the chain's native hash is keccak256.
Nothing else in the codebase may hash an object; everything routes through
:func:`hash_object` so the canonical form and the digest function can never
drift apart between the writer and the verifier.

The one exception is :mod:`app.core.crypto_shred`, which hashes raw bytes
rather than an object, and content addressing for media, which uses SHA-256
because that is what IPFS expects.
"""

from __future__ import annotations

import hashlib
from typing import Any

# eth_utils re-exports keccak without declaring it in __all__, so the
# canonical import path is the submodule it actually lives in.
from eth_utils.crypto import keccak as _keccak

from app.core.canonical import canonicalize

__all__ = ["from_hex", "hash_hex", "hash_object", "keccak256", "sha256_hex"]


def keccak256(data: bytes) -> bytes:
    """Return the 32-byte keccak256 digest of *data*."""
    digest: bytes = _keccak(data)
    return digest


def hash_hex(data: bytes) -> str:
    """Render a digest as ``0x``-prefixed lowercase hex."""
    return "0x" + data.hex()


def from_hex(value: str) -> bytes:
    """Parse a ``0x``-prefixed hex digest back into bytes."""
    cleaned = value[2:] if value.startswith(("0x", "0X")) else value
    return bytes.fromhex(cleaned)


def sha256_hex(data: bytes) -> str:
    """Bare SHA-256 hex, no ``0x`` prefix.

    Not for anchoring. This is for the values stored in ``*_hash`` columns that
    never reach the chain -- IP addresses, user agents, refresh tokens, media
    content addresses -- where SHA-256 is the conventional choice and the
    digest is compared only against other rows in this database.
    """
    return hashlib.sha256(data).hexdigest()


def hash_object(obj: Any) -> str:
    """Canonicalize *obj* per RFC 8785, then keccak256 it.

    This is the value anchored on chain and the value recomputed from the
    Postgres row at verification time. The two must agree byte for byte.
    """
    return hash_hex(keccak256(canonicalize(obj)))
