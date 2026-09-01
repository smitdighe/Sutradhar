"""Tree queries: ancestry, descendants, remaining quantity.

Every function here is **one** SQL statement, using a recursive CTE. That is not
premature optimisation -- the obvious implementation walks parent links in a
loop and issues one query per level, and a four-level lineage becomes four round
trips on a page that renders a provenance chain. On Neon's free tier, with
network latency between the app and the database, that is the difference between
a page that feels instant and one that visibly stalls.

``tests/integration/test_provenance.py`` counts the statements and asserts 1.

Cycle safety is enforced twice over: a CHECK constraint makes ``parent_id = id``
impossible, and every CTE carries a depth ceiling so a longer cycle -- which the
CHECK cannot see -- terminates instead of spinning.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.provenance.item_hash import quantise
from app.provenance.mass_balance import MAX_TREE_DEPTH

__all__ = ["TreeNode", "get_ancestry", "get_descendants", "get_remaining_quantity"]

# One more than the enforced maximum, so a tree that somehow exceeds the limit
# is still traversable rather than silently truncated at the boundary.
_CTE_CEILING = MAX_TREE_DEPTH + 2


@dataclass(frozen=True, slots=True)
class TreeNode:
    """One item in a lineage or subtree."""

    id: uuid.UUID
    parent_id: uuid.UUID | None
    depth: int
    quantity: Decimal
    quantity_unit: str
    item_hash: str
    tag_code: str | None
    status: str
    category_id: uuid.UUID
    registered_by: uuid.UUID

    @classmethod
    def from_row(cls, row: object) -> TreeNode:
        mapping = row._mapping  # type: ignore[attr-defined]
        return cls(
            id=mapping["id"],
            parent_id=mapping["parent_id"],
            depth=int(mapping["depth"]),
            quantity=quantise(Decimal(mapping["quantity"])),
            quantity_unit=mapping["quantity_unit"],
            item_hash=mapping["item_hash"],
            tag_code=mapping["tag_code"],
            status=str(mapping["status"]),
            category_id=mapping["category_id"],
            registered_by=mapping["registered_by"],
        )


_COLUMNS = (
    "id, parent_id, quantity, quantity_unit, item_hash, tag_code, status, "
    "category_id, registered_by"
)

_ANCESTRY_SQL = text(
    f"""
    WITH RECURSIVE lineage AS (
        SELECT {_COLUMNS}, 1 AS depth
        FROM items
        WHERE id = :item_id

        UNION ALL

        SELECT {', '.join(f'i.{name.strip()}' for name in _COLUMNS.split(','))},
               lineage.depth + 1
        FROM items i
        JOIN lineage ON i.id = lineage.parent_id
        WHERE lineage.depth < :ceiling
    )
    SELECT * FROM lineage ORDER BY depth DESC
    """
)

_DESCENDANTS_SQL = text(
    f"""
    WITH RECURSIVE subtree AS (
        SELECT {_COLUMNS}, 1 AS depth
        FROM items
        WHERE id = :item_id

        UNION ALL

        SELECT {', '.join(f'i.{name.strip()}' for name in _COLUMNS.split(','))},
               subtree.depth + 1
        FROM items i
        JOIN subtree ON i.parent_id = subtree.id
        WHERE subtree.depth < :ceiling
    )
    SELECT * FROM subtree ORDER BY depth, id
    """
)

_REMAINING_SQL = text(
    """
    SELECT
        parent.quantity
          - COALESCE((SELECT SUM(child.quantity) FROM items child
                      WHERE child.parent_id = parent.id), 0) AS remaining
    FROM items parent
    WHERE parent.id = :item_id
    """
)


async def get_ancestry(session: AsyncSession, item_id: uuid.UUID) -> list[TreeNode]:
    """Root-to-item lineage, ordered root first. One statement.

    The item itself is the last element, so rendering a breadcrumb is a
    straight iteration with no reversal or special-casing of the head.
    """
    result = await session.execute(
        _ANCESTRY_SQL, {"item_id": item_id, "ceiling": _CTE_CEILING}
    )
    # Depth counts *upward* from the item, so descending depth is root first.
    return [TreeNode.from_row(row) for row in result.fetchall()]


async def get_descendants(session: AsyncSession, item_id: uuid.UUID) -> list[TreeNode]:
    """Full subtree including the item itself, depth-annotated. One statement."""
    result = await session.execute(
        _DESCENDANTS_SQL, {"item_id": item_id, "ceiling": _CTE_CEILING}
    )
    return [TreeNode.from_row(row) for row in result.fetchall()]


async def get_remaining_quantity(session: AsyncSession, item_id: uuid.UUID) -> Decimal | None:
    """Parent quantity minus allocated children, or None if the item is gone.

    Computed in SQL rather than in Python: this is the number a split is checked
    against, and summing in the database keeps the arithmetic in ``numeric``
    all the way through.
    """
    row = (await session.execute(_REMAINING_SQL, {"item_id": item_id})).scalar_one_or_none()
    if row is None:
        return None
    return quantise(Decimal(row))
