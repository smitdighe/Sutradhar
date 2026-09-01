"""The canonical item preimage. **This is a wire format, not an implementation.**

Every item hash ever anchored on chain is the digest of the structure below.
Verification recomputes it from the Postgres row and compares. So if the
preimage changes after anything is anchored, every prior record becomes
permanently unverifiable -- there is no migration, because the chain cannot be
rewritten. Phase 7's anchoring worker and the Solidity verifier both consume
this. Treat it as frozen.

The preimage, v1::

    {
      "v": 1,                        # preimage schema version
      "item_id": str,                # UUIDv7, canonical hyphenated
      "category_slug": str,
      "category_schema_version": int,
      "parent_id": str | null,       # UUIDv7 of the parent, or null
      "quantity": str,               # decimal as string, exactly 4dp
      "quantity_unit": str,
      "attributes": {...},           # the item's validated attributes
      "registered_by_hash": str,     # salted identity hash, NOT the user id
      "registered_at": str           # RFC 3339 UTC, exactly 6 fractional digits
    }

**Field order in this file is documentation, not wire format.**
:func:`~app.core.canonical.canonicalize` implements RFC 8785, which sorts object
keys, so Python's insertion order is erased before the digest is taken. That is
the point: a Solidity verifier, a Python reader and a JS client all sort
identically without having to agree on an ordering first. What is frozen is the
**field set and each value's encoding** -- rename a field, add one, drop one, or
change how a value is rendered, and every existing hash breaks.

Two encoding choices that are load-bearing:

*Quantity is a string at exactly 4dp.* It comes out of ``numeric(18,4)``, and
``5.5`` and ``5.5000`` are different stored values. As a JSON number it would
pass through a float somewhere and 12.0 would eventually hash as
12.000000000000002.

*Timestamps are RFC 3339 with exactly 6 fractional digits.* Postgres stores
microseconds; a renderer that trimmed trailing zeros would hash
``...481920Z`` and ``...48192Z`` differently for the same instant.

**No personally identifying data enters this structure -- ever.** The registrant
appears only as ``registered_by_hash``, the per-subject salted digest from
:mod:`app.core.crypto_shred`. That is not decoration, it is the DPDP Act 2023
erasure mechanism: the chain is append-only and cannot forget, but deleting a
subject's ``identity_salt`` row makes the anchored hash permanently unlinkable
to them. Putting a user id or an email in here would anchor it forever and make
erasure impossible. :func:`assert_no_pii` is the guard, and a test greps the
serialised preimage.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.core.canonical import canonicalize
from app.core.clock import to_rfc3339
from app.core.crypto_shred import identity_hash
from app.core.hashing import hash_hex, keccak256

__all__ = [
    "PREIMAGE_FIELDS",
    "PREIMAGE_VERSION",
    "QUANTITY_EXPONENT",
    "build_preimage",
    "compute_item_hash",
    "quantise",
    "registrant_hash",
]

PREIMAGE_VERSION = 1

# numeric(18,4). Quantisation is explicit so the string form is always 4dp.
QUANTITY_EXPONENT = Decimal("0.0001")

# The frozen field set. A test asserts the built preimage has exactly these
# keys, so adding or removing one fails loudly rather than silently changing
# every future digest.
PREIMAGE_FIELDS = frozenset(
    {
        "v",
        "item_id",
        "category_slug",
        "category_schema_version",
        "parent_id",
        "quantity",
        "quantity_unit",
        "attributes",
        "registered_by_hash",
        "registered_at",
    }
)


def quantise(value: Decimal | str | int) -> Decimal:
    """Round to exactly 4 decimal places, half-up.

    Half-up rather than banker's rounding: this is a physical quantity of cloth,
    and "round half to even" is a surprise nobody reading a mass-balance
    calculation expects.
    """
    return Decimal(value).quantize(QUANTITY_EXPONENT, rounding=ROUND_HALF_UP)


def registrant_hash(user_id: uuid.UUID, identity_salt: bytes) -> str:
    """The registrant's anchorable identity digest.

    The only representation of a person that may enter the preimage. Delete the
    salt and this becomes unlinkable -- see the module docstring.
    """
    return identity_hash(str(user_id), identity_salt)


def build_preimage(
    *,
    item_id: uuid.UUID,
    category_slug: str,
    category_schema_version: int,
    parent_id: uuid.UUID | None,
    quantity: Decimal,
    quantity_unit: str,
    attributes: dict[str, Any],
    registered_by_hash: str,
    registered_at: datetime,
) -> dict[str, Any]:
    """Assemble the v1 preimage. Keyword-only, so no caller can transpose two
    positional arguments and silently produce a valid-looking wrong hash.
    """
    return {
        "v": PREIMAGE_VERSION,
        "item_id": str(item_id),
        "category_slug": category_slug,
        "category_schema_version": int(category_schema_version),
        "parent_id": str(parent_id) if parent_id is not None else None,
        # String, exactly 4dp -- never a JSON number. See the module docstring.
        "quantity": str(quantise(quantity)),
        "quantity_unit": quantity_unit,
        "attributes": attributes,
        "registered_by_hash": registered_by_hash,
        # Exactly 6 fractional digits, always 'Z'.
        "registered_at": to_rfc3339(registered_at),
    }


def compute_item_hash(preimage: dict[str, Any]) -> str:
    """keccak256 of the RFC 8785 canonical form. ``0x``-prefixed lowercase hex.

    keccak256 rather than SHA-256 because this digest is compared against a
    value stored on an EVM chain, and keccak256 is what Solidity computes.
    """
    return hash_hex(keccak256(canonicalize(preimage)))


def hash_item(
    *,
    item_id: uuid.UUID,
    category_slug: str,
    category_schema_version: int,
    parent_id: uuid.UUID | None,
    quantity: Decimal,
    quantity_unit: str,
    attributes: dict[str, Any],
    registered_by_hash: str,
    registered_at: datetime,
) -> tuple[str, dict[str, Any]]:
    """Build the preimage and hash it. Returns ``(hash, preimage)``.

    The preimage is returned as well so callers can record it in an item event,
    which is what makes a disputed hash auditable without re-deriving it.
    """
    preimage = build_preimage(
        item_id=item_id,
        category_slug=category_slug,
        category_schema_version=category_schema_version,
        parent_id=parent_id,
        quantity=quantity,
        quantity_unit=quantity_unit,
        attributes=attributes,
        registered_by_hash=registered_by_hash,
        registered_at=registered_at,
    )
    return compute_item_hash(preimage), preimage


def assert_no_pii(preimage: dict[str, Any], forbidden: list[str]) -> None:
    """Raise if any forbidden substring appears in the serialised preimage.

    Used by tests and by the registration path in debug builds. Cheap insurance
    against somebody adding a convenience field to the preimage and anchoring a
    weaver's email address on a public chain forever.
    """
    blob = canonicalize(preimage).decode("utf-8")
    for needle in forbidden:
        if needle and needle in blob:
            raise ValueError(f"preimage contains identifying data: {needle!r}")
