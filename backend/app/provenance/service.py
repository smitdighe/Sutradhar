"""Item registration and splitting.

**Everything in a registration is one transaction.** Item row, provenance event,
and chain-outbox entry commit together or not at all. A partial write here is
not a tidiness problem: an item with no outbox row never gets anchored and looks
permanently pending, and an outbox row with no item anchors a hash of nothing.
``tests/integration/test_provenance.py`` injects a failure after the item insert
and asserts every table is unchanged.

**Status is honest.** A new item is ``PENDING`` and stays that way until Phase
7's worker sees the required confirmations. Marking it ``CONFIRMED`` at
registration would make the demo smoother and the record worthless.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog import registry
from app.catalog.validator import validate_attributes
from app.core.clock import now
from app.core.errors import ErrorCode, NotFoundError, ValidationError
from app.core.hashing import hash_object
from app.core.ids import new_uuid
from app.db.models.catalog import GICategory, Item, ItemEvent
from app.db.models.enums import ItemEventType, ItemStatus, OutboxJobType
from app.db.models.outbox import Outbox
from app.db.models.user import User
from app.provenance.item_hash import hash_item, quantise, registrant_hash
from app.provenance.mass_balance import (
    allocated_quantity,
    assert_depth_within_limit,
    check_split_allowed,
    lock_parent,
)
from app.provenance.schemas import RegisterItemRequest, SplitChild, SplitRequest

__all__ = ["get_item", "list_items", "register_item", "split_item"]


async def _resolve_category(
    session: AsyncSession, slug: str, quantity_unit: str
) -> registry.CompiledCategory:
    """Find the category, pin its version, and check the unit agrees."""
    compiled = await registry.latest_active(session, slug)
    if compiled is None:
        any_version = await registry.get_compiled(session, slug)
        if any_version is not None:
            raise ValidationError(
                code=ErrorCode.CATEGORY_RETIRED,
                status=422,
                message=f"category '{slug}' is retired and accepts no new items",
            )
        raise NotFoundError(
            code=ErrorCode.CATEGORY_NOT_FOUND, message=f"no category with slug '{slug}'"
        )

    if quantity_unit != compiled.quantity_unit:
        # Metres of leather, or pairs of silk. Caught here rather than absorbed,
        # because the unit is part of what the item hash commits to.
        raise ValidationError(
            code=ErrorCode.QUANTITY_UNIT_MISMATCH,
            status=422,
            message=(
                f"category '{slug}' is measured in {compiled.quantity_unit}, "
                f"not {quantity_unit}"
            ),
            details={"expected": compiled.quantity_unit, "received": quantity_unit},
        )
    return compiled


async def _category_row(session: AsyncSession, compiled: registry.CompiledCategory) -> GICategory:
    row = await session.get(GICategory, compiled.id)
    if row is None:  # pragma: no cover - the registry mirrors this table
        raise NotFoundError(
            code=ErrorCode.CATEGORY_NOT_FOUND, message=f"category '{compiled.slug}' is gone"
        )
    return row


def _record_event(
    session: AsyncSession,
    item_id: uuid.UUID,
    event_type: ItemEventType,
    actor_id: uuid.UUID | None,
    payload: dict[str, Any],
) -> ItemEvent:
    event = ItemEvent(
        item_id=item_id,
        event_type=event_type,
        actor_id=actor_id,
        payload=payload,
        payload_hash=hash_object(payload),
    )
    session.add(event)
    return event


def _enqueue_anchor(session: AsyncSession, item: Item, issuer_hash: str) -> Outbox:
    """Queue the item for anchoring.

    ``dedupe_key`` is the item hash, so re-enqueueing the same item is a no-op
    at the unique index rather than a second on-chain write. Committed in the
    same transaction as the item, which is the whole point of an outbox: the
    chain call cannot join this transaction, so the *intent* to make it does.

    ``issuer_hash`` is the second argument ``anchorItem`` takes, copied in here
    rather than joined back out of the ``REGISTERED`` event at send time. The
    row carries everything the transaction needs, so draining the outbox reads
    one table and a replay does not depend on an event log still being intact.
    It is the salted identity digest, never a user id -- the same value that
    goes into the preimage, and unlinkable once the subject's salt is deleted.
    """
    outbox = Outbox(
        job_type=OutboxJobType.ANCHOR_ITEM,
        payload={
            "item_id": str(item.id),
            "item_hash": item.item_hash,
            "issuer_hash": issuer_hash,
        },
        dedupe_key=item.item_hash,
    )
    session.add(outbox)
    return outbox


async def _build_item(
    session: AsyncSession,
    *,
    registrant: User,
    compiled: registry.CompiledCategory,
    attributes: dict[str, Any],
    quantity: Decimal,
    parent: Item | None,
) -> Item:
    """Validate, hash, insert. Shared by registration and splitting."""
    validate_attributes(compiled.validator, attributes)

    item_id = new_uuid()
    registered_at = now()
    quantised = quantise(quantity)

    item_hash, preimage = hash_item(
        item_id=item_id,
        category_slug=compiled.slug,
        category_schema_version=compiled.schema_version,
        parent_id=parent.id if parent else None,
        quantity=quantised,
        quantity_unit=compiled.quantity_unit,
        attributes=attributes,
        # The only representation of a person that reaches the chain. Deleting
        # this user's identity_salt makes the anchored hash unlinkable to them.
        registered_by_hash=registrant_hash(registrant.id, registrant.identity_salt),
        registered_at=registered_at,
    )

    item = Item(
        id=item_id,
        category_id=compiled.id,
        category_schema_version=compiled.schema_version,
        parent_id=parent.id if parent else None,
        registered_by=registrant.id,
        attributes=attributes,
        quantity=quantised,
        quantity_unit=compiled.quantity_unit,
        item_hash=item_hash,
        # Honest until the chain says otherwise.
        status=ItemStatus.PENDING,
        created_at=registered_at,
        updated_at=registered_at,
    )
    session.add(item)
    await session.flush()

    _record_event(
        session,
        item.id,
        ItemEventType.REGISTERED,
        registrant.id,
        # The preimage is recorded verbatim, so a disputed hash is auditable
        # without re-deriving it from a row that may since have been touched.
        {"preimage": preimage, "item_hash": item_hash},
    )
    _enqueue_anchor(session, item, issuer_hash=str(preimage["registered_by_hash"]))
    return item


async def register_item(
    session: AsyncSession, payload: RegisterItemRequest, registrant: User
) -> Item:
    """Register one item. Caller commits."""
    compiled = await _resolve_category(session, payload.category_slug, payload.quantity_unit)

    parent: Item | None = None
    if payload.parent_id is not None:
        # Registering directly under a parent is a split of one child, and gets
        # the same locking and accounting.
        parent = await lock_parent(session, payload.parent_id)
        await assert_depth_within_limit(session, parent.id)
        await check_split_allowed(session, parent, [payload.quantity])

    return await _build_item(
        session,
        registrant=registrant,
        compiled=compiled,
        attributes=payload.attributes,
        quantity=payload.quantity,
        parent=parent,
    )


async def split_item(
    session: AsyncSession, parent_id: uuid.UUID, payload: SplitRequest, actor: User
) -> tuple[Item, list[Item], Decimal, Decimal]:
    """Cut a parent into children. Returns ``(parent, children, allocated, remaining)``.

    The parent row is locked before anything is summed and stays locked to
    commit, so two simultaneous splits serialise: the second re-reads the
    allocation the first committed and is refused if it no longer fits.
    """
    parent = await lock_parent(session, parent_id)
    await assert_depth_within_limit(session, parent.id)

    # Called for the refusal, not the return value: the post-split accounting
    # is recomputed below once the children exist.
    await check_split_allowed(
        session, parent, [child.quantity for child in payload.children]
    )

    compiled = await registry.get_compiled(
        session, *_slug_and_version_of(await _category_of(session, parent))
    )
    if compiled is None:  # pragma: no cover - the parent's category must exist
        raise NotFoundError(
            code=ErrorCode.CATEGORY_NOT_FOUND, message="the parent's category is unavailable"
        )

    children: list[Item] = []
    for child in payload.children:
        _assert_child_unit(child, parent.quantity_unit)
        children.append(
            await _build_item(
                session,
                registrant=actor,
                compiled=compiled,
                attributes=child.attributes,
                quantity=child.quantity,
                parent=parent,
            )
        )

    # The SPLIT event is written on the PARENT: the transformation happened to
    # it, and its event log is where somebody auditing the bolt will look.
    _record_event(
        session,
        parent.id,
        ItemEventType.SPLIT,
        actor.id,
        {
            "parent_hash": parent.item_hash,
            "children": [
                {"item_id": str(child.id), "item_hash": child.item_hash,
                 "quantity": str(child.quantity)}
                for child in children
            ],
        },
    )

    allocated = await allocated_quantity(session, parent.id)
    remaining = quantise(Decimal(parent.quantity) - allocated)
    return parent, children, allocated, remaining


def _assert_child_unit(child: SplitChild, parent_unit: str) -> None:
    """A child is measured in its parent's unit, always.

    Cutting a 12-metre bolt into "two pairs" is not a split, and allowing it
    would make mass balance meaningless -- you cannot subtract pairs from metres.
    """
    if child.quantity_unit is not None and child.quantity_unit != parent_unit:
        raise ValidationError(
            code=ErrorCode.QUANTITY_UNIT_MISMATCH,
            status=422,
            message=f"children are measured in {parent_unit}, like their parent",
            details={"expected": parent_unit, "received": child.quantity_unit},
        )


async def _category_of(session: AsyncSession, item: Item) -> GICategory:
    row = await session.get(GICategory, item.category_id)
    if row is None:  # pragma: no cover - FK guarantees this
        raise NotFoundError(
            code=ErrorCode.CATEGORY_NOT_FOUND, message="the item's category is gone"
        )
    return row


def _slug_and_version_of(category: GICategory) -> tuple[str, int]:
    """A child inherits its parent's pinned schema version, not the latest.

    Otherwise cutting a sari from a bolt registered under v1 would validate the
    piece against v2 and could fail on a bolt that was perfectly legal when it
    was registered.
    """
    return category.slug, category.schema_version


# ---------------------------------------------------------------- reads


async def get_item(session: AsyncSession, item_id: uuid.UUID) -> Item:
    item = await session.get(Item, item_id)
    if item is None:
        raise NotFoundError(
            code=ErrorCode.ITEM_NOT_FOUND, message=f"no item with id {item_id}"
        )
    return item


async def list_items(
    session: AsyncSession,
    *,
    category_id: uuid.UUID | None = None,
    status: ItemStatus | None = None,
    registered_by: uuid.UUID | None = None,
    cursor_created_at: Any = None,
    cursor_id: uuid.UUID | None = None,
    limit: int = 20,
) -> list[Item]:
    """Keyset page over items, newest first.

    ``(created_at DESC, id DESC)`` with a strict tuple comparison, never
    OFFSET -- see :mod:`app.core.pagination` for why.
    """
    statement = select(Item).order_by(Item.created_at.desc(), Item.id.desc()).limit(limit)

    if category_id is not None:
        statement = statement.where(Item.category_id == category_id)
    if status is not None:
        statement = statement.where(Item.status == status)
    if registered_by is not None:
        statement = statement.where(Item.registered_by == registered_by)
    if cursor_created_at is not None and cursor_id is not None:
        # tuple_(), not a Python tuple: this has to render as the SQL row
        # constructor `(created_at, id) < (?, ?)` so the composite index is
        # usable. A Python tuple would compare as a boolean and silently
        # degrade the keyset into a full scan.
        statement = statement.where(
            tuple_(Item.created_at, Item.id) < (cursor_created_at, cursor_id)
        )

    return list((await session.execute(statement)).scalars().all())


async def list_events(
    session: AsyncSession, item_id: uuid.UUID, limit: int = 50, offset: int = 0
) -> list[ItemEvent]:
    """Append-only event log for one item, oldest first."""
    await get_item(session, item_id)
    statement = (
        select(ItemEvent)
        .where(ItemEvent.item_id == item_id)
        .order_by(ItemEvent.created_at, ItemEvent.id)
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(statement)).scalars().all())
