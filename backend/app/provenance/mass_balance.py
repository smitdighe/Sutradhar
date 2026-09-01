"""Mass balance: children may never allocate more than their parent holds.

**Why this is the load-bearing rule of the whole system.** A six-metre bolt cut
into two three-metre pieces, both sold under the parent's tag, means two objects
that both scan as genuine. Neither is a forgery in the usual sense -- the tag is
real and the provenance chain is real -- and no amount of cryptography detects
it, because nothing was faked. That is precisely how counterfeit goods get
laundered into a legitimate supply chain: not by forging a certificate, but by
attaching one genuine certificate to more objects than it covers.

The defence is structural, not cryptographic. Tags are issued at the smallest
sellable unit, every split is recorded, and the sum of a parent's children can
never exceed the parent. Then "two pieces, one tag" is not a thing the database
can represent.

Two implementation details that are the difference between enforcing this and
appearing to:

*Decimal, quantised to 4dp, never float.* ``0.1 + 0.2 > 0.3`` in binary floating
point. A comparison that is wrong in the eighth decimal place is a hole a
counterfeiter walks through a few grams at a time.

*``SELECT ... FOR UPDATE`` on the parent before summing.* Without the row lock,
two simultaneous splits both read the same "remaining" and both commit, and the
children over-allocate. Read-then-write is not enough here; the lock is what
serialises them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ErrorCode, ValidationError
from app.db.models.catalog import Item
from app.provenance.item_hash import quantise

__all__ = [
    "MAX_TREE_DEPTH",
    "MassBalance",
    "allocated_quantity",
    "assert_depth_within_limit",
    "check_split_allowed",
    "lock_parent",
]

# Root is depth 1. A bolt -> saris -> pieces chain is three; five is already
# generous. Unbounded recursion is a denial-of-service vector, and the recursive
# CTEs in tree.py would walk it.
MAX_TREE_DEPTH = 5


@dataclass(frozen=True, slots=True)
class MassBalance:
    """A parent's quantity accounting, all values quantised to 4dp."""

    parent_id: uuid.UUID
    total: Decimal
    allocated: Decimal

    @property
    def remaining(self) -> Decimal:
        return quantise(self.total - self.allocated)

    def as_details(self) -> dict[str, str]:
        """Serialised for an error body. Strings, so no float ever appears."""
        return {
            "parent_id": str(self.parent_id),
            "parent_quantity": str(quantise(self.total)),
            "already_allocated": str(quantise(self.allocated)),
            "remaining": str(self.remaining),
        }


async def lock_parent(session: AsyncSession, parent_id: uuid.UUID) -> Item:
    """Take a row lock on the parent, or raise 404.

    Every split must go through here. The lock is held to the end of the
    transaction, so a concurrent split of the same parent blocks until this one
    commits and then re-reads the true allocation.
    """
    parent = (
        await session.execute(select(Item).where(Item.id == parent_id).with_for_update())
    ).scalar_one_or_none()

    if parent is None:
        from app.core.errors import NotFoundError

        raise NotFoundError(
            code=ErrorCode.ITEM_NOT_FOUND, message=f"no item with id {parent_id}"
        )
    return parent


async def allocated_quantity(session: AsyncSession, parent_id: uuid.UUID) -> Decimal:
    """Sum of existing children. Zero when there are none."""
    total = (
        await session.execute(
            select(func.coalesce(func.sum(Item.quantity), 0)).where(Item.parent_id == parent_id)
        )
    ).scalar_one()
    return quantise(Decimal(total))


async def depth_of(session: AsyncSession, item_id: uuid.UUID) -> int:
    """How many levels above this item, counting itself. Root is 1."""
    from sqlalchemy import text

    result = await session.execute(
        text(
            """
            WITH RECURSIVE lineage(id, parent_id, depth) AS (
                SELECT id, parent_id, 1 FROM items WHERE id = :item_id
                UNION ALL
                SELECT i.id, i.parent_id, lineage.depth + 1
                FROM items i JOIN lineage ON i.id = lineage.parent_id
                -- Guard, not an optimisation: a cyclic chain would otherwise
                -- spin here forever. The CHECK on parent_id <> id makes a
                -- self-loop impossible; this covers longer cycles.
                WHERE lineage.depth < :ceiling
            )
            SELECT max(depth) FROM lineage
            """
        ),
        {"item_id": item_id, "ceiling": MAX_TREE_DEPTH + 2},
    )
    return int(result.scalar_one() or 1)


async def assert_depth_within_limit(session: AsyncSession, parent_id: uuid.UUID) -> int:
    """Raise 422 if adding a child under *parent_id* would exceed the limit."""
    parent_depth = await depth_of(session, parent_id)
    child_depth = parent_depth + 1
    if child_depth > MAX_TREE_DEPTH:
        raise ValidationError(
            code=ErrorCode.MAX_DEPTH_EXCEEDED,
            status=422,
            message=f"splitting here would create depth {child_depth}; the limit is "
            f"{MAX_TREE_DEPTH}",
            details={"parent_depth": str(parent_depth), "max_depth": str(MAX_TREE_DEPTH)},
        )
    return child_depth


async def check_split_allowed(
    session: AsyncSession, parent: Item, requested: list[Decimal]
) -> MassBalance:
    """Verify a proposed split fits, or raise 409.

    *parent* must already be locked by :func:`lock_parent`. Requires the caller
    to have taken the lock rather than taking it here, so the lock covers the
    whole read-decide-insert sequence rather than just the read.
    """
    allocated = await allocated_quantity(session, parent.id)
    balance = MassBalance(
        parent_id=parent.id, total=quantise(Decimal(parent.quantity)), allocated=allocated
    )

    total_requested = quantise(sum(requested, Decimal(0)))

    if total_requested <= 0:
        raise ValidationError(
            code=ErrorCode.VALIDATION_FAILED,
            status=422,
            message="split quantities must be positive",
        )

    if total_requested > balance.remaining:
        # 409, not 422: the request is well-formed, it collides with state that
        # may have changed since the client last looked.
        raise ConflictError(
            code=ErrorCode.MASS_BALANCE_EXCEEDED,
            message=(
                f"cannot allocate {total_requested} {parent.quantity_unit}; "
                f"only {balance.remaining} remains of this item"
            ),
            details={**balance.as_details(), "requested": str(total_requested)},
        )

    return balance
