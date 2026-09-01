"""Request and response models for the provenance endpoints.

These serialise the **authenticated** view: full attributes, registrant ids,
chain state. The public verification view in Phase 11 is a different serialiser
with a deliberately smaller field set. They must not be merged -- an
authenticated model reused on a public route is how a scan endpoint starts
leaking who registered what.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.core.clock import UtcDatetime
from app.db.models.enums import DisputeStatus, ItemStatus
from app.provenance.item_hash import quantise
from app.provenance.mass_balance import MAX_TREE_DEPTH

__all__ = [
    "ItemDetail",
    "ItemEventResponse",
    "ItemSummary",
    "RegisterItemRequest",
    "SplitChild",
    "SplitRequest",
    "SplitResponse",
    "TreeNodeResponse",
]


class _QuantityMixin(BaseModel):
    """Serialises Decimal quantities as fixed-4dp strings.

    Never a JSON number: 12.0 through a float becomes 12.000000000000002
    somewhere downstream, and this is the value the item hash commits to.
    """

    @field_serializer("quantity", check_fields=False)
    def _quantity(self, value: Decimal) -> str:
        return str(quantise(value))


class RegisterItemRequest(BaseModel):
    """Register a new item, or a root of a new provenance tree."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    category_slug: str = Field(min_length=1, max_length=64)
    attributes: dict[str, Any]
    quantity: Decimal = Field(gt=0)
    quantity_unit: str = Field(min_length=1, max_length=32)
    parent_id: uuid.UUID | None = None

    @field_validator("quantity")
    @classmethod
    def _quantise(cls, value: Decimal) -> Decimal:
        return quantise(value)


class SplitChild(BaseModel):
    """One piece cut from a parent."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    attributes: dict[str, Any]
    quantity: Decimal = Field(gt=0)
    quantity_unit: str | None = None

    @field_validator("quantity")
    @classmethod
    def _quantise(cls, value: Decimal) -> Decimal:
        return quantise(value)


class SplitRequest(BaseModel):
    """Cut a parent into children. Mass balance is enforced across all of them."""

    model_config = ConfigDict(extra="ignore")

    children: list[SplitChild] = Field(min_length=1, max_length=50)


class ItemSummary(_QuantityMixin):
    """An item in a listing."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    category_id: uuid.UUID
    category_schema_version: int
    parent_id: uuid.UUID | None
    registered_by: uuid.UUID
    quantity: Decimal
    quantity_unit: str
    item_hash: str
    tag_code: str | None
    status: ItemStatus
    dispute_status: DisputeStatus
    created_at: UtcDatetime


class TreeNodeResponse(_QuantityMixin):
    """One node of an ancestry chain or subtree."""

    id: uuid.UUID
    parent_id: uuid.UUID | None
    depth: int
    quantity: Decimal
    quantity_unit: str
    item_hash: str
    tag_code: str | None
    status: str


class ChainState(BaseModel):
    """What the chain currently knows. Honest about PENDING.

    An item is PENDING until Phase 7's worker sees the required confirmations.
    Reporting CONFIRMED before that would be a lie the demo tells, and the whole
    proposition is that this record can be trusted.
    """

    status: ItemStatus
    anchored: bool
    tx_hash: str | None = None
    block_number: int | None = None
    confirmations: int = 0


class ItemDetail(ItemSummary):
    """One item with everything an authenticated caller may see."""

    category_slug: str
    attributes: dict[str, Any]
    remaining_quantity: Decimal
    ancestry: list[TreeNodeResponse]
    children: list[TreeNodeResponse]
    chain: ChainState

    @field_serializer("remaining_quantity")
    def _remaining(self, value: Decimal) -> str:
        return str(quantise(value))


class SplitResponse(BaseModel):
    """The outcome of a split, with the parent's updated accounting."""

    parent_id: uuid.UUID
    children: list[ItemSummary]
    parent_quantity: str
    allocated: str
    remaining: str
    max_depth: int = MAX_TREE_DEPTH


class ItemEventResponse(BaseModel):
    """One append-only provenance event."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    item_id: uuid.UUID
    event_type: str
    actor_id: uuid.UUID | None
    payload: dict[str, Any]
    payload_hash: str
    created_at: UtcDatetime


class ItemListResponse(BaseModel):
    """Collection envelope with a keyset cursor, per app.core.pagination."""

    data: list[ItemSummary]
    pagination: dict[str, Any]


class ItemEventListResponse(BaseModel):
    data: list[ItemEventResponse]
    pagination: dict[str, Any]
