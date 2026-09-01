"""Catalog endpoints.

Public reads under ``/categories``; admin writes under ``/admin/categories``.

The write path deliberately reloads the registry inside the request that caused
the change, so the category is usable on the very next request with no restart
and no redeploy. That is the thirty-second stage moment, and
``tests/integration/test_live_category_add.py`` is the proof it works.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.guards import get_current_user, require_role
from app.auth.roles import Role
from app.catalog import registry, service
from app.catalog.schemas import (
    CategoryDetail,
    CategoryListResponse,
    CategorySummary,
    CategoryVersionsResponse,
    CreateCategoryRequest,
    CreateVersionRequest,
    CreateVersionResponse,
    UpdateCategoryRequest,
    ValidateAttributesRequest,
    ValidateAttributesResponse,
)
from app.config import get_settings
from app.core import idempotency
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, clamp_limit
from app.core.ratelimit import rate_limit
from app.db.models.user import User
from app.db.session import get_session

__all__ = ["admin_router", "router"]

router = APIRouter(prefix="/categories", tags=["catalog"])
admin_router = APIRouter(
    prefix="/admin/categories",
    tags=["catalog-admin"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)

# The public-surface ceiling, reused rather than given a knob of its own. It is
# already the number chosen for "how often may somebody with no account make
# this service do work", which is exactly the question here, and a second
# setting would be one more thing to tune consistently and forget to.
_validate_limit = rate_limit(
    "catalog_validate", get_settings().rate_limit_scan_per_minute, 60
)


# ---------------------------------------------------------------- public reads


@router.get("", response_model=CategoryListResponse)
async def list_categories(
    limit: int = Query(default=DEFAULT_LIMIT, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    include_inactive: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
) -> CategoryListResponse:
    """Latest version of every category. Public."""
    bounded = clamp_limit(limit)
    rows = await service.list_categories(
        session, include_inactive=include_inactive, limit=bounded + 1, offset=offset
    )
    has_more = len(rows) > bounded
    page = rows[:bounded]

    return CategoryListResponse(
        data=[CategorySummary.model_validate(row) for row in page],
        pagination={
            # Offset paging here, not the keyset cursors used elsewhere: this
            # table holds tens of rows, not millions, and the ordering is by
            # slug rather than by time.
            "next_offset": offset + bounded if has_more else None,
            "limit": bounded,
        },
    )


@router.get("/{slug}", response_model=CategoryDetail)
async def read_category(
    slug: str, session: AsyncSession = Depends(get_session)
) -> CategoryDetail:
    """Latest active version of one category."""
    return CategoryDetail.model_validate(await service.latest_version(session, slug))


@router.get("/{slug}/versions", response_model=CategoryVersionsResponse)
async def read_versions(
    slug: str, session: AsyncSession = Depends(get_session)
) -> CategoryVersionsResponse:
    """Every published version, oldest first."""
    rows = await service.list_versions(session, slug)
    return CategoryVersionsResponse(
        slug=slug, data=[CategorySummary.model_validate(row) for row in rows]
    )


@router.get("/{slug}/v/{version}", response_model=CategoryDetail)
async def read_pinned_version(
    slug: str, version: int, session: AsyncSession = Depends(get_session)
) -> CategoryDetail:
    """One pinned version, retired or not.

    Retired versions still resolve: existing items reference them, and a
    verification that 404s because a category was retired would be a broken
    provenance record.
    """
    return CategoryDetail.model_validate(await service.get_version(session, slug, version))


@router.post(
    "/{slug}/validate",
    response_model=ValidateAttributesResponse,
    dependencies=[Depends(_validate_limit)],
)
async def validate_attributes(
    slug: str,
    payload: ValidateAttributesRequest,
    session: AsyncSession = Depends(get_session),
) -> ValidateAttributesResponse:
    """Dry-run an attribute payload against a category. Writes nothing.

    Lets a client check a form before submitting it, and gives the live-demo
    sequence a real request that proves a just-created category is usable.
    Item registration itself is Phase 6.

    Rate limited, unlike the reads beside it. This is the only unauthenticated
    endpoint in the system that does real work on a caller-supplied body: it
    loads a schema and runs a validator over arbitrary JSON. The reads answer
    from a cached registry and cost a lookup; this costs compilation, and on a
    single free-tier instance an unmetered one of those is a way to spend the
    process's CPU from outside with no account and no cost to the caller.
    """
    compiled = await service.validate_item_attributes(
        session, slug, payload.attributes, payload.schema_version
    )
    return ValidateAttributesResponse(
        valid=True, slug=compiled.slug, schema_version=compiled.schema_version
    )


# ---------------------------------------------------------------- admin writes


async def _claim_idempotency(
    session: AsyncSession, user: User, key: str | None, body: Any
) -> tuple[idempotency.IdempotencyOutcome | None, None]:
    if not key:
        return None, None
    return await idempotency.begin(session, user.id, key, body), None


@admin_router.post("", status_code=status.HTTP_201_CREATED, response_model=CategoryDetail)
async def create_category(
    payload: CreateCategoryRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CategoryDetail:
    """Create a category at v1, usable immediately.

    Accepts a category JSON document as the request body, so publishing one on
    stage is ``curl -d @banarasi.json`` and nothing else.
    """
    outcome, _ = await _claim_idempotency(
        session, user, idempotency_key, payload.model_dump(mode="json")
    )
    if outcome is not None and outcome.replay and outcome.response_body is not None:
        response.status_code = outcome.response_status or status.HTTP_201_CREATED
        return CategoryDetail.model_validate(outcome.response_body)

    category = await service.create_category(session, payload, user.id)
    body = CategoryDetail.model_validate(category)

    if outcome is not None:
        await idempotency.complete(
            session, outcome.record_id, status.HTTP_201_CREATED, body.model_dump(mode="json")
        )

    await session.commit()
    # Inside the same request that created it: the next request, from any
    # client, sees the new category. No restart, no redeploy.
    await registry.reload(session)
    return body


@admin_router.post("/{slug}/versions", response_model=CreateVersionResponse)
async def create_version(
    slug: str,
    payload: CreateVersionRequest,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CreateVersionResponse:
    """Publish v(n+1), and report what it changes.

    The diff is returned so the operator sees the consequence at the moment
    they cause it, rather than discovering it when registrations start failing.
    Existing items are unaffected either way -- they are pinned to their own
    version.
    """
    outcome, _ = await _claim_idempotency(
        session, user, idempotency_key, {"slug": slug, **payload.model_dump(mode="json")}
    )
    if outcome is not None and outcome.replay and outcome.response_body is not None:
        return CreateVersionResponse.model_validate(outcome.response_body)

    category, diff = await service.create_version(session, slug, payload, user.id)
    body = CreateVersionResponse(
        category=CategoryDetail.model_validate(category),
        diff=diff,
        breaking=diff.is_breaking,
    )

    if outcome is not None:
        await idempotency.complete(
            session, outcome.record_id, status.HTTP_200_OK, body.model_dump(mode="json")
        )

    await session.commit()
    await registry.reload(session)
    return body


@admin_router.patch("/{slug}", response_model=CategoryDetail)
async def update_category(
    slug: str,
    payload: UpdateCategoryRequest,
    session: AsyncSession = Depends(get_session),
) -> CategoryDetail:
    """Update display name and active flag. Slug and schema are unreachable.

    Neither field exists on ``UpdateCategoryRequest``, so no request shape can
    change them -- a slug is referenced by URLs and printed tags, and a schema
    change is a new version by definition.
    """
    category = await service.update_category(session, slug, payload)
    await session.commit()
    await registry.reload(session)
    return CategoryDetail.model_validate(category)
