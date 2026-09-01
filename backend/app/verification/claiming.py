"""First scan wins: one tag, one claim, decided by the database.

**The primary key is the rule.** ``claims.item_id`` is the primary key, so a
second claim on the same object is refused by PostgreSQL, not by an
``if already_claimed`` this code could lose a race on. Two shoppers scanning
the same tag at the same instant is not hypothetical -- it is a shelf in a shop
-- and an application-level check would let both through under exactly the
concurrency that matters.

**The first claim is never overwritten.** Not by a later scan, not by an admin,
not by a retry. A record that can be rewritten by whoever scanned most recently
is not a record of anything.

**Wording is factual, and that is a product decision, not politeness.** When a
second device scans a claimed tag, this says *what happened* -- it was claimed
on a date -- and suggests contacting the seller. It does not say what that
means. A retail display gets scanned by dozens of people who are not doing
anything wrong, a person who scans their own object twice has done nothing at
all, and a system that tells the second scanner they are holding something
illegitimate will be wrong far more often than it is right, in public, to the
one customer who cared enough to check. Say what happened; let the person and
their seller work out what it means.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.catalog import Item
from app.db.models.scan import Claim
from app.verification.scan import ScanContext

__all__ = ["ClaimStatus", "ClaimView", "attempt_claim", "read_claim"]


class ClaimStatus(StrEnum):
    """What this request's device found, and what it was allowed to do."""

    # Nobody has claimed this object yet. Only ever returned by a read.
    UNCLAIMED = "UNCLAIMED"
    # This device holds the claim -- it either just made it or made it earlier.
    CLAIMED = "CLAIMED"
    # Somebody else's device claimed it first. Stated, not judged.
    ALREADY_CLAIMED = "ALREADY_CLAIMED"


@dataclass(frozen=True, slots=True)
class ClaimView:
    """The claim block of the public payload."""

    status: ClaimStatus
    claimed: bool
    claimed_at: datetime | None
    is_your_claim: bool
    # Present only when a different device claimed first. Never an accusation.
    message: str | None = None
    # The region the claim was made from, when there was one. Coarse, like
    # everything else here, and useful for "was that where I bought it".
    claimed_region: str | None = None


def _already_claimed_message(claimed_at: datetime, region: str | None) -> str:
    """One sentence of fact, and one of advice. Nothing about the object."""
    where = f" in {region}" if region else ""
    return (
        f"This tag was already claimed{where} on "
        f"{claimed_at.strftime('%d %B %Y')} by the first device that scanned it. "
        "If you did not expect that, ask the seller you bought this from about it."
    )


def _view(claim: Claim, *, mine: bool) -> ClaimView:
    if mine:
        return ClaimView(
            status=ClaimStatus.CLAIMED,
            claimed=True,
            claimed_at=claim.claimed_at,
            is_your_claim=True,
            claimed_region=claim.region_code,
        )
    return ClaimView(
        status=ClaimStatus.ALREADY_CLAIMED,
        claimed=True,
        claimed_at=claim.claimed_at,
        is_your_claim=False,
        message=_already_claimed_message(claim.claimed_at, claim.region_code),
        claimed_region=claim.region_code,
    )


def _is_mine(claim: Claim, fingerprint_hash: str | None) -> bool:
    """A claim belongs to this device only when both sides know the device.

    Two unknown fingerprints are not the same device; they are two absences.
    Treating them as equal would hand a claim to whoever scanned next from a
    client that sends nothing.
    """
    return bool(fingerprint_hash) and claim.device_fingerprint == fingerprint_hash


async def read_claim(
    session: AsyncSession, item_id: uuid.UUID, fingerprint_hash: str | None
) -> ClaimView:
    """The claim state without touching it. Used by the public GET."""
    claim = await session.get(Claim, item_id)
    if claim is None:
        return ClaimView(
            status=ClaimStatus.UNCLAIMED,
            claimed=False,
            claimed_at=None,
            is_your_claim=False,
        )
    return _view(claim, mine=_is_mine(claim, fingerprint_hash))


async def attempt_claim(
    session: AsyncSession, item_id: uuid.UUID, context: ScanContext
) -> ClaimView:
    """Claim the object for this device if nobody has. Caller commits.

    ``ON CONFLICT DO NOTHING`` is the whole mechanism: exactly one of any number
    of simultaneous inserts returns a row, and every other one falls through to
    read the winner. There is no read-then-write window here to lose.

    A request with no device fingerprint at all does not claim. Binding an
    object to "whoever this was" is worse than leaving it unclaimed, because the
    next person to scan would be told somebody else already owns it and there
    would be no way to tell whether that was true.
    """
    if not context.fingerprint_hash:
        return await read_claim(session, item_id, None)

    statement = (
        insert(Claim)
        .values(
            item_id=item_id,
            device_fingerprint=context.fingerprint_hash,
            country_code=context.country_code,
            region_code=context.region_code,
        )
        .on_conflict_do_nothing(index_elements=[Claim.item_id])
        .returning(Claim.item_id)
    )
    won = (await session.execute(statement)).scalar_one_or_none() is not None

    if won:
        # The denormalised copy on the item, kept in step in the same
        # transaction. It is not part of the hashed preimage, so writing it
        # cannot disturb verification.
        item = await session.get(Item, item_id)
        claim = await session.get(Claim, item_id)
        if item is not None and claim is not None:
            item.claimed_at = claim.claimed_at
        await session.flush()

    claim = await session.get(Claim, item_id)
    if claim is None:  # pragma: no cover - the insert above guarantees a row
        return ClaimView(
            status=ClaimStatus.UNCLAIMED,
            claimed=False,
            claimed_at=None,
            is_your_claim=False,
        )
    return _view(claim, mine=_is_mine(claim, context.fingerprint_hash))
