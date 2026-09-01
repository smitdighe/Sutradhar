"""Per-subject salted identity hashing -- the DPDP Act 2023 erasure mechanism.

A chain is append-only, so anything anchored there can never be deleted. The
Digital Personal Data Protection Act 2023 nonetheless gives a Data Principal
the right to erasure. This module is how both hold at once.

The chain stores only ``identity_hash(subject_id, salt)``. The salt lives in a
single Postgres column, ``users.identity_salt``, and nowhere else. Erasing a
subject means deleting that salt row.

**Deleting the salt makes the on-chain hash permanently unlinkable to the
person.** The digest stays on chain forever, but with a 32-byte secret salt
gone there is no feasible way to confirm that any particular subject produced
it -- the search space is 2**256, not the size of the user table, which is what
an unsalted hash of an email address would give an attacker.

This is a deliberate design decision, not a side effect. The consequence is
that erasure is irreversible: once the salt is gone, the service itself can no
longer prove which subject an on-chain record refers to, and there is no
recovery path. That is the point.
"""

from __future__ import annotations

import hmac
import secrets

from app.config import get_settings
from app.core.hashing import hash_hex, keccak256

__all__ = ["SALT_BYTES", "identity_hash", "matches_identity", "new_salt"]

SALT_BYTES = 32


def new_salt() -> bytes:
    """Return 32 cryptographically random bytes for one subject's salt."""
    return secrets.token_bytes(SALT_BYTES)


def identity_hash(subject_id: str, salt: bytes) -> str:
    """Return the anchorable identity digest for *subject_id* under *salt*.

    Computed as ``keccak256(salt + IDENTITY_HASH_PEPPER + subject_id)``. The
    salt is per subject and lives in the database; the pepper is a single
    process-wide secret from the environment and never touches the database, so
    a database dump alone does not permit offline recomputation.
    """
    if len(salt) != SALT_BYTES:
        raise ValueError(f"salt must be exactly {SALT_BYTES} bytes, got {len(salt)}")
    pepper = get_settings().identity_hash_pepper.encode("utf-8")
    return hash_hex(keccak256(salt + pepper + subject_id.encode("utf-8")))


def matches_identity(subject_id: str, salt: bytes, expected: str) -> bool:
    """Constant-time comparison of a recomputed identity hash against *expected*."""
    return hmac.compare_digest(identity_hash(subject_id, salt), expected)
