"""Item endpoints. Authenticated.

The public verification view is Phase 11 and uses a different serialiser with a
smaller field set. Nothing here should end up on a public route: these responses
carry registrant ids and full attributes.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.guards import get_current_user, require_role
from app.auth.roles import Role
from app.core import idempotency
from app.core.errors import ErrorCode, ValidationError
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, clamp_limit, decode_cursor, encode_cursor
from app.db.models.catalog import GICategory
from app.db.models.enums import ItemStatus
from app.db.models.user import User
from app.db.session import get_session
from app.provenance import service, tree
from app.provenance.schemas import (
    ChainState,
    ItemDetail,
    ItemEventListResponse,
    ItemEventResponse,
    ItemListResponse,
    ItemSummary,
    RegisterItemRequest,
    SplitRequest,
    SplitResponse,
    TreeNodeResponse,
)

__all__ = ["router"]

router = APIRouter(prefix="/items", tags=["provenance"])

# Registration is a claim about physical goods. A consumer scanning a tag has no
# business making one.
_can_register = require_role(Role.WEAVER, Role.COOP_OFFICER)


def _require_idempotency_key(key: str | None) -> str:
    """Registration and splitting both mutate a provenance chain.

    A retried POST that created a second item would put two hashes on chain for
    one bolt, so the key is required rather than optional.
    """
    if not key:
        raise ValidationError(
            code=ErrorCode.VALIDATION_FAILED,
            status=422,
            message="the Idempotency-Key header is required for this request",
        )
    return key


def _node(item: Any) -> TreeNodeResponse:
    return TreeNodeResponse(
        id=item.id,
        parent_id=item.parent_id,
        depth=item.depth,
        quantity=item.quantity,
        quantity_unit=item.quantity_unit,
        item_hash=item.item_hash,
        tag_code=item.tag_code,
        status=str(item.status),
    )


# ---------------------------------------------------------------- writes


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ItemSummary)
async def register_item(
    payload: RegisterItemRequest,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(_can_register),
    session: AsyncSession = Depends(get_session),
) -> ItemSummary:
    """Register an item. Item, event, and outbox row commit together or not at all."""
    key = _require_idempotency_key(idempotency_key)
    outcome = await idempotency.begin(session, user.id, key, payload.model_dump(mode="json"))
    if outcome.replay and outcome.response_body is not None:
        response.status_code = outcome.response_status or status.HTTP_201_CREATED
        return ItemSummary.model_validate(outcome.response_body)

    item = await service.register_item(session, payload, user)
    body = ItemSummary.model_validate(item)

    await idempotency.complete(
        session, outcome.record_id, status.HTTP_201_CREATED, body.model_dump(mode="json")
    )
    await session.commit()
    return body


@router.post("/{item_id}/split", response_model=SplitResponse)
async def split_item(
    item_id: uuid.UUID,
    payload: SplitRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(_can_register),
    session: AsyncSession = Depends(get_session),
) -> SplitResponse:
    """Cut an item into children, enforcing mass balance under a row lock."""
    key = _require_idempotency_key(idempotency_key)
    outcome = await idempotency.begin(
        session, user.id, key, {"item_id": str(item_id), **payload.model_dump(mode="json")}
    )
    if outcome.replay and outcome.response_body is not None:
        return SplitResponse.model_validate(outcome.response_body)

    parent, children, allocated, remaining = await service.split_item(
        session, item_id, payload, user
    )
    body = SplitResponse(
        parent_id=parent.id,
        children=[ItemSummary.model_validate(child) for child in children],
        parent_quantity=str(parent.quantity),
        allocated=str(allocated),
        remaining=str(remaining),
    )

    await idempotency.complete(
        session, outcome.record_id, status.HTTP_200_OK, body.model_dump(mode="json")
    )
    await session.commit()
    return body


# ---------------------------------------------------------------- reads


@router.get("", response_model=ItemListResponse)
async def list_items(
    category_slug: str | None = Query(default=None),
    item_status: ItemStatus | None = Query(default=None, alias="status"),
    registered_by: uuid.UUID | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, le=MAX_LIMIT),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ItemListResponse:
    """Keyset page over items, newest first."""
    bounded = clamp_limit(limit)

    category_id: uuid.UUID | None = None
    if category_slug is not None:
        category_id = (
            await session.execute(
                select(GICategory.id)
                .where(GICategory.slug == category_slug)
                .order_by(GICategory.schema_version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if category_id is None:
            return ItemListResponse(data=[], pagination={"next_cursor": None, "limit": bounded})

    cursor_created_at = None
    cursor_id = None
    if cursor:
        decoded = decode_cursor(cursor)
        cursor_created_at, cursor_id = decoded.key, decoded.id

    rows = await service.list_items(
        session,
        category_id=category_id,
        status=item_status,
        registered_by=registered_by,
        cursor_created_at=cursor_created_at,
        cursor_id=cursor_id,
        limit=bounded + 1,
    )
    has_more = len(rows) > bounded
    page = rows[:bounded]

    return ItemListResponse(
        data=[ItemSummary.model_validate(row) for row in page],
        pagination={
            "next_cursor": (
                encode_cursor(page[-1].created_at, page[-1].id) if has_more and page else None
            ),
            "limit": bounded,
        },
    )


@router.get("/{item_id}", response_model=ItemDetail)
async def read_item(
    item_id: uuid.UUID,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ItemDetail:
    """One item with its lineage, children, and current chain state."""
    item = await service.get_item(session, item_id)

    category = await session.get(GICategory, item.category_id)
    ancestry = await tree.get_ancestry(session, item_id)
    subtree = await tree.get_descendants(session, item_id)
    remaining = await tree.get_remaining_quantity(session, item_id)

    return ItemDetail(
        **ItemSummary.model_validate(item).model_dump(),
        category_slug=category.slug if category else "",
        attributes=dict(item.attributes),
        remaining_quantity=remaining if remaining is not None else item.quantity,
        # Root first; the item itself is the last element.
        ancestry=[_node(node) for node in ancestry if node.id != item.id],
        children=[_node(node) for node in subtree if node.parent_id == item.id],
        # Phase 7 populates tx_hash, block_number and confirmations once the
        # anchoring worker exists. Until then this reports the truth -- queued,
        # not anchored -- rather than inventing a transaction.
        chain=ChainState(status=item.status, anchored=item.status is ItemStatus.CONFIRMED),
    )


@router.get("/{item_id}/tree", response_model=list[TreeNodeResponse])
async def read_tree(
    item_id: uuid.UUID,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[TreeNodeResponse]:
    """Full subtree, depth-annotated. One recursive CTE."""
    await service.get_item(session, item_id)
    return [_node(node) for node in await tree.get_descendants(session, item_id)]


@router.get("/{item_id}/events", response_model=ItemEventListResponse)
async def read_events(
    item_id: uuid.UUID,
    limit: int = Query(default=50, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ItemEventListResponse:
    """Append-only event log, oldest first."""
    bounded = clamp_limit(limit)
    rows = await service.list_events(session, item_id, limit=bounded + 1, offset=offset)
    has_more = len(rows) > bounded
    page = rows[:bounded]

    return ItemEventListResponse(
        data=[ItemEventResponse.model_validate(row) for row in page],
        pagination={"next_offset": offset + bounded if has_more else None, "limit": bounded},
    )
