"""Media endpoints: upload, resolve, serve, link, unlink.

Uploads go through :mod:`app.media.service`, which does the sniffing, the
budgeting and the pinning in an order that matters -- read that module's
docstring before changing anything here.

``/raw`` exists so the mirror and blob tiers are reachable at all: neither is a
public URL on its own, and the frontend needs somewhere to fall back to when the
IPFS gateway is having a day.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.guards import get_current_user, require_role
from app.auth.roles import Role
from app.core.errors import ErrorCode, NotFoundError
from app.db.models.user import User
from app.db.session import get_session
from app.media import service
from app.media.mirror import MirrorStore, resolve
from app.media.schemas import (
    AttachMediaRequest,
    ItemMediaResponse,
    MediaDetail,
    MediaSummary,
    TierResponse,
)

__all__ = ["item_router", "router"]

router = APIRouter(prefix="/media", tags=["media"])
item_router = APIRouter(prefix="/items", tags=["media"])

# Uploading is a claim about physical goods, the same as registering one. A
# consumer scanning a tag has no business adding photographs to the record.
_can_upload = require_role(Role.WEAVER, Role.COOP_OFFICER, Role.INSPECTOR)


def _session_factory() -> async_sessionmaker[AsyncSession]:
    """The quota trackers open their own transactions on purpose.

    Consumption has to survive a rolled-back request: bytes handed to Pinata are
    spent whether or not the surrounding transaction commits, and a budget that
    forgets them under-reports until it is wrong by a lot.

    Resolved from :mod:`app.db.session` **at call time**, never captured at
    import. A module-level ``from app.db.session import SessionLocal`` binds
    whatever that name pointed at the moment this module was first imported,
    and the first import is not a fixed point: it happens inside whichever test
    builds an application first. A run whose first ``create_app()`` sits outside
    the integration fixtures leaves this bound to the *production* sessionmaker,
    and every quota read afterwards silently meters against the development
    database -- passing in isolation and failing in a full run, which is the
    worst shape a bug can have. ``app.core.ratelimit`` resolves it the same way
    and for the same reason.
    """
    from app.db.session import SessionLocal

    return SessionLocal


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=MediaDetail,
    summary="Upload a file",
)
async def upload(
    file: UploadFile = File(...),
    uploader: User = Depends(_can_upload),
    session: AsyncSession = Depends(get_session),
) -> MediaDetail:
    """Stream, bound, sniff, hash, budget, mirror, store, then try to pin.

    Returns 201 even when pinning fails or is switched off. The SHA-256 is the
    integrity proof and it is already committed; the CID only records where a
    copy happens to live, and a pinning service having a bad day is not a reason
    to reject a weaver's photograph.
    """
    result = await service.ingest(
        session,
        _session_factory(),
        file.file,
        uploader,
    )
    await session.commit()

    resolved = resolve(result.media)
    return MediaDetail(
        id=result.media.id,
        sha256=result.media.sha256,
        byte_size=result.media.byte_size,
        content_type=result.media.content_type,
        cid=result.media.cid,
        pin_status=result.media.pin_status,
        created_at=result.media.created_at,
        tiers=[
            TierResponse(tier=option.tier, url=option.url, durable=option.durable)
            for option in resolved.tiers
        ],
        primary_tier=resolved.primary.tier if resolved.primary else None,
        durable=any(option.durable for option in resolved.tiers),
    )


@router.get("/{media_id}", response_model=MediaDetail, summary="Media metadata and tiers")
async def get_media(
    media_id: uuid.UUID,
    _actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MediaDetail:
    """Every tier this file can be read from right now, best first."""
    media = await service.load_media(session, media_id)
    resolved = resolve(media)
    return MediaDetail(
        id=media.id,
        sha256=media.sha256,
        byte_size=media.byte_size,
        content_type=media.content_type,
        cid=media.cid,
        pin_status=media.pin_status,
        created_at=media.created_at,
        tiers=[
            TierResponse(tier=option.tier, url=option.url, durable=option.durable)
            for option in resolved.tiers
        ],
        primary_tier=resolved.primary.tier if resolved.primary else None,
        durable=any(option.durable for option in resolved.tiers),
    )


@router.get("/{media_id}/raw", summary="Serve the bytes from the best available tier")
async def get_raw(
    media_id: uuid.UUID,
    tier: str | None = Query(default=None, description="MIRROR or BLOB; advisory only"),
    _actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Serve the file, falling through tiers rather than trusting any one.

    ``tier`` is a preference, not an instruction: the mirror may have been wiped
    by a redeploy since the row was written, so a request for it still falls
    back to the blob rather than 404ing on a tier that used to exist.
    """
    media = await service.load_media(session, media_id)
    found = service.read_bytes(media, MirrorStore(), prefer=tier)

    if found is None:
        # Both local tiers are gone. Honest 404 rather than a redirect to a
        # gateway that may also be dead.
        raise NotFoundError(
            code=ErrorCode.NOT_FOUND,
            message="no local copy of this file is available",
            details={"media_id": str(media_id), "sha256": media.sha256},
        )

    data, served_from = found
    return Response(
        content=data,
        media_type=media.content_type,
        headers={
            "X-Sutradhar-Tier": served_from,
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'inline; filename="{media.sha256}"',
            "ETag": f'"{media.sha256}"',
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )


# ------------------------------------------------------------------ linkage


@item_router.post(
    "/{item_id}/media",
    status_code=status.HTTP_201_CREATED,
    response_model=ItemMediaResponse,
    summary="Link media to an item",
)
async def attach(
    item_id: uuid.UUID,
    payload: AttachMediaRequest,
    actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ItemMediaResponse:
    """Only the item's registrant, or an admin."""
    link = await service.attach_media(
        session, item_id, payload.media_id, payload.kind, actor
    )
    media = await service.load_media(session, payload.media_id)
    await session.commit()
    return ItemMediaResponse(media=MediaSummary.model_validate(media), kind=link.kind)


@item_router.get(
    "/{item_id}/media",
    response_model=list[ItemMediaResponse],
    summary="List an item's media",
)
async def list_item_media(
    item_id: uuid.UUID,
    _actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ItemMediaResponse]:
    rows = await service.list_for_item(session, item_id)
    return [
        ItemMediaResponse(media=MediaSummary.model_validate(media), kind=kind)
        for media, kind in rows
    ]


@item_router.delete(
    "/{item_id}/media/{media_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unlink media from an item",
)
async def detach(
    item_id: uuid.UUID,
    media_id: uuid.UUID,
    actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Removes the link. The media row and its bytes are kept.

    The SHA-256 may already be anchored on chain, and deleting the bytes behind
    an anchored hash produces exactly the dead reference the three-tier storage
    design exists to prevent. "No longer depicts this item" is not "never
    existed".
    """
    removed = await service.detach_media(session, item_id, media_id, actor)
    await session.commit()
    if not removed:
        raise NotFoundError(
            code=ErrorCode.NOT_FOUND,
            message="this media is not linked to that item",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
