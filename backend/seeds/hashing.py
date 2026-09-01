"""Seed hashing -- now a thin adapter over the real Phase 6 hasher.

This module used to carry a provisional preimage because
``app.provenance.item_hash`` did not exist yet. It does now, so the provisional
shape is gone and seeded items carry the same hashes the API produces. There is
one implementation of the item preimage in this codebase and it is
:mod:`app.provenance.item_hash`.

Kept as a module rather than deleted because the seed loader wants a statement
hash too, and that has no home in the provenance package yet.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.core.hashing import hash_object
from app.provenance.item_hash import PREIMAGE_VERSION, hash_item, quantise, registrant_hash

__all__ = ["SEED_HASH_VERSION", "hash_item", "quantise", "registrant_hash", "seed_statement_hash"]

# Recorded in each seeded item's REGISTERED event so a row's provenance is
# identifiable in the database. Now tracks the real preimage version.
SEED_HASH_VERSION = f"item-preimage-v{PREIMAGE_VERSION}"


def seed_statement_hash(
    *, item_hash: str, attestor_id: uuid.UUID, role: str, statement: dict[str, Any]
) -> str:
    """keccak256 of an attestation.

    Binds the statement to the item *hash* rather than its id: an attestation is
    a claim about a specific version of an item, so if the item changes the
    attestation should no longer apply to it.

    TODO(phase-8): move this into the attestation module when it exists.
    """
    return hash_object(
        {
            "v": PREIMAGE_VERSION,
            "item_hash": item_hash,
            "attestor": str(attestor_id),
            "role": role,
            "statement": statement,
        }
    )
