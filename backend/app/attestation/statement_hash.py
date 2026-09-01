"""The canonical attestation preimage. **A wire format, not an implementation.**

Same contract as :mod:`app.provenance.item_hash`, and frozen for the same
reason: every statement hash anchored on chain is the digest of the structure
below, and verification recomputes it from the Postgres row. Change the field
set or an encoding after anything is anchored and every prior attestation
becomes permanently unverifiable, because the chain cannot be rewritten.

The preimage, v1::

    {
      "v": 1,                      # preimage schema version
      "kind": "attestation",       # domain separation from the item preimage
      "item_hash": str,            # the item being attested to, not its id
      "attestor_hash": str,        # salted identity hash, NOT the user id
      "attestor_role": str,        # role held at attestation time
      "statement": {...},          # the free-form claim, verbatim
      "attested_at": str           # RFC 3339 UTC, exactly 6 fractional digits
    }

Three choices worth stating.

*``kind`` is present and constant.* Item hashes and statement hashes are both
32-byte keccak digests anchored through the same contract function. Without a
domain tag in the preimage, a structure that happened to serialise identically
would produce the same digest for two different kinds of claim. It costs seven
bytes to make that impossible.

*The item is referenced by its hash, not its id.* An id is a database detail; the
item hash is the thing already on chain. A verifier holding only chain data can
follow ``item_hash`` to the item's own anchor, and never has to trust that this
service's ids mean anything.

*The attestor appears only as a salted digest.* Identical to the item preimage's
``registered_by_hash`` rule and for the identical reason: the chain is
append-only and cannot forget, so putting a user id or an email here would
anchor it forever and make DPDP erasure impossible. Deleting the subject's
``identity_salt`` makes this digest permanently unlinkable to them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from app.core.canonical import canonicalize
from app.core.clock import to_rfc3339
from app.core.crypto_shred import identity_hash
from app.core.hashing import hash_hex, keccak256
from app.db.models.enums import UserRole

__all__ = [
    "PREIMAGE_FIELDS",
    "PREIMAGE_KIND",
    "PREIMAGE_VERSION",
    "attestor_hash",
    "build_preimage",
    "compute_statement_hash",
    "hash_statement",
]

PREIMAGE_VERSION = 1
PREIMAGE_KIND = "attestation"

# The frozen field set. A test asserts the built preimage has exactly these
# keys, so adding or removing one fails loudly rather than silently changing
# every future digest.
PREIMAGE_FIELDS = frozenset(
    {
        "v",
        "kind",
        "item_hash",
        "attestor_hash",
        "attestor_role",
        "statement",
        "attested_at",
    }
)


def attestor_hash(user_id: uuid.UUID, identity_salt: bytes) -> str:
    """The attestor's anchorable identity digest.

    The only representation of a person that may enter the preimage.
    """
    return identity_hash(str(user_id), identity_salt)


def build_preimage(
    *,
    item_hash: str,
    attestor_hash: str,
    attestor_role: UserRole,
    statement: dict[str, Any],
    attested_at: datetime,
) -> dict[str, Any]:
    """Assemble the v1 preimage. Keyword-only, so no caller can transpose two
    positional arguments and silently produce a valid-looking wrong hash.
    """
    return {
        "v": PREIMAGE_VERSION,
        "kind": PREIMAGE_KIND,
        "item_hash": item_hash,
        "attestor_hash": attestor_hash,
        "attestor_role": str(attestor_role),
        # Verbatim. The statement is the claim; normalising it here would mean
        # anchoring something the attestor did not write.
        "statement": statement,
        # Exactly 6 fractional digits, always 'Z'.
        "attested_at": to_rfc3339(attested_at),
    }


def compute_statement_hash(preimage: dict[str, Any]) -> str:
    """keccak256 of the RFC 8785 canonical form. ``0x``-prefixed lowercase hex."""
    return hash_hex(keccak256(canonicalize(preimage)))


def hash_statement(
    *,
    item_hash: str,
    attestor_id: uuid.UUID,
    identity_salt: bytes,
    attestor_role: UserRole,
    statement: dict[str, Any],
    attested_at: datetime,
) -> tuple[str, dict[str, Any]]:
    """Build the preimage and hash it. Returns ``(hash, preimage)``.

    The preimage comes back too, so callers can record it in an item event and a
    disputed hash stays auditable without re-deriving it from rows that may
    since have been touched.
    """
    preimage = build_preimage(
        item_hash=item_hash,
        attestor_hash=attestor_hash(attestor_id, identity_salt),
        attestor_role=attestor_role,
        statement=statement,
        attested_at=attested_at,
    )
    return compute_statement_hash(preimage), preimage
