"""Tag issuance and QR image endpoints.

Three routes, and the split between them is deliberate. Issuing a tag is a
write that binds a physical label to a record, so it is authenticated, role
gated and idempotency-keyed. Rendering the QR is a pure function of the tag
code, so it is cacheable forever. Bulk issuance exists because a co-op officer
preparing a morning's labels should not have to make fifty requests by hand.

The image endpoints stay authenticated even though the payload they encode is
public. The QR *contents* are a printed number; the mapping from an internal
item id to that number is not, and that mapping is what a request here reveals.
Phase 11 serves the consumer side, and it starts from the tag code.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict

from fastapi import APIRouter, Depends, Header, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.guards import require_role
from app.auth.roles import Role
from app.core import idempotency
from app.core.errors import ErrorCode, NotFoundError, ValidationError
from app.db.models.user import User
from app.db.session import get_session
from app.provenance import service as provenance_service
from app.qr import service

__all__ = ["admin_router", "router"]

router = APIRouter(prefix="/items", tags=["qr"])

# The path says admin, the permission says co-op officer as well. Batch tagging
# is the co-op operator's job -- it is the friction this endpoint exists to
# remove -- and routing it through an admin would put a person who is not in the
# room between a weaver's output and its labels.
admin_router = APIRouter(
    prefix="/admin/tags",
    tags=["qr", "admin"],
    dependencies=[Depends(require_role(Role.COOP_OFFICER))],
)

# A consumer scanning a tag has no business minting one. ADMIN is admitted by
# require_role itself; the per-item ownership check for a weaver lives in the
# service, because it depends on the item as well as the actor.
_can_issue = require_role(Role.WEAVER, Role.COOP_OFFICER)

# The payload never changes for a tag: the code is immutable once bound and the
# URL is derived from it. A year is the maximum any cache honours anyway, and
# `immutable` stops revalidation requests entirely.
_CACHE_FOREVER = "public, max-age=31536000, immutable"


# ---------------------------------------------------------------- schemas


class TagResponse(BaseModel):
    """One issued tag, in both stored and printable form."""

    model_config = ConfigDict(extra="forbid")

    item_id: uuid.UUID
    # Canonical: uppercase, no separators. This is what is stored and compared.
    tag_code: str
    # Grouped in fours, for the label and for anybody reading it aloud.
    display_code: str
    # The exact string encoded in the QR. Nothing else goes in the image.
    payload_url: str
    # Non-blocking advisories. A tag on an item that has been split is legal --
    # a bolt can be sold whole -- and is also the laundering shape, so it is
    # said out loud rather than silently allowed.
    warnings: list[str] = Field(default_factory=list)


class BulkTagRequest(BaseModel):
    """Items to tag in one pass."""

    model_config = ConfigDict(extra="forbid")

    item_ids: list[uuid.UUID]


class BulkTagResult(BaseModel):
    """What happened to one item in the batch."""

    model_config = ConfigDict(extra="forbid")

    item_id: uuid.UUID
    # "issued" | "already_tagged" | "failed"
    outcome: str
    tag_code: str | None = None
    display_code: str | None = None
    payload_url: str | None = None
    reason_code: str | None = None
    reason: str | None = None
    warnings: list[str] = Field(default_factory=list)


class BulkTagResponse(BaseModel):
    """Per-item results and their totals. Partial success is a success."""

    model_config = ConfigDict(extra="forbid")

    requested: int
    issued: int
    already_tagged: int
    failed: int
    results: list[BulkTagResult]


def _require_idempotency_key(key: str | None) -> str:
    """A retried POST that minted a second code would orphan the first label."""
    if not key:
        raise ValidationError(
            code=ErrorCode.VALIDATION_FAILED,
            status=422,
            message="the Idempotency-Key header is required for this request",
        )
    return key


# ---------------------------------------------------------------- issuance


@router.post(
    "/{item_id}/tag",
    status_code=status.HTTP_201_CREATED,
    response_model=TagResponse,
    summary="Issue a tag code for an item",
)
async def issue_tag(
    item_id: uuid.UUID,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: User = Depends(_can_issue),
    session: AsyncSession = Depends(get_session),
) -> TagResponse:
    """Generate a code, bind it to the item, and record a ``TAG_ISSUED`` event.

    409 when the item is already tagged, with the existing code in the error
    details: that is a fact to read, not a failure to retry.
    """
    key = _require_idempotency_key(idempotency_key)
    outcome = await idempotency.begin(session, actor.id, key, {"item_id": str(item_id)})
    if outcome.replay and outcome.response_body is not None:
        response.status_code = outcome.response_status or status.HTTP_201_CREATED
        return TagResponse.model_validate(outcome.response_body)

    issued = await service.issue_tag(session, item_id, actor)
    body = TagResponse(
        item_id=issued.item_id,
        tag_code=issued.tag_code,
        display_code=issued.display_code,
        payload_url=issued.payload_url,
        warnings=issued.warnings,
    )

    await idempotency.complete(
        session, outcome.record_id, status.HTTP_201_CREATED, body.model_dump(mode="json")
    )
    await session.commit()
    return body


@admin_router.post(
    "/bulk",
    response_model=BulkTagResponse,
    summary="Issue tags for many items at once",
)
async def bulk_issue(
    payload: BulkTagRequest,
    actor: User = Depends(require_role(Role.COOP_OFFICER)),
    session: AsyncSession = Depends(get_session),
) -> BulkTagResponse:
    """Tag a batch, reporting each item separately.

    An already-tagged item or one whose anchoring failed is reported and
    stepped over. Rejecting the whole batch for one bad row would make an
    operator find and remove it by hand before any of the good ones printed.
    """
    if len(payload.item_ids) > service.BULK_MAX_ITEMS:
        # Checked before anything is written, so an oversized batch issues
        # nothing at all rather than the first 500.
        raise ValidationError(
            code=ErrorCode.BULK_TOO_LARGE,
            status=422,
            message=f"a batch may hold at most {service.BULK_MAX_ITEMS} items",
            details={"limit": service.BULK_MAX_ITEMS, "received": len(payload.item_ids)},
        )

    results = await service.bulk_issue_tags(session, payload.item_ids, actor)
    await session.commit()

    rendered = [BulkTagResult(**asdict(result)) for result in results]
    return BulkTagResponse(
        requested=len(payload.item_ids),
        issued=sum(1 for result in rendered if result.outcome == "issued"),
        already_tagged=sum(1 for result in rendered if result.outcome == "already_tagged"),
        failed=sum(1 for result in rendered if result.outcome == "failed"),
        results=rendered,
    )


# ---------------------------------------------------------------- rendering


@router.get(
    "/{item_id}/tag/qr",
    summary="Render an item's tag as a QR image",
    response_class=Response,
    responses={200: {"content": {"image/png": {}, "image/svg+xml": {}}}},
)
async def render_qr(
    item_id: uuid.UUID,
    image_format: str = Query(default="png", alias="format", pattern="^(png|svg)$"),
    size: int = Query(default=service.DEFAULT_PNG_SIZE, ge=1),
    _actor: User = Depends(require_role(Role.WEAVER, Role.COOP_OFFICER, Role.INSPECTOR)),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """PNG by default, SVG on request. Error correction Q, four-module quiet zone.

    ``size`` is a pixel edge for the PNG and the default ``width``/``height`` on
    the SVG, which also carries a ``viewBox`` so it scales to any label.
    """
    item = await provenance_service.get_item(session, item_id)
    if item.tag_code is None:
        raise NotFoundError(
            code=ErrorCode.NOT_FOUND,
            message="this item has no tag yet; issue one first",
            details={"item_id": str(item_id)},
        )

    headers = {
        "Cache-Control": _CACHE_FOREVER,
        # Handy for a print pipeline that saves the response to disk.
        "Content-Disposition": (
            f'inline; filename="{item.tag_code}.{image_format}"'
        ),
    }

    if image_format == "svg":
        return Response(
            content=service.render_svg(item.tag_code, size),
            media_type="image/svg+xml",
            headers=headers,
        )
    return Response(
        content=service.render_png(item.tag_code, size),
        media_type="image/png",
        headers=headers,
    )
