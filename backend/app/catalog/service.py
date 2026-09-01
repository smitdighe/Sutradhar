"""Category lifecycle: creation, versioning, retirement, and attribute checks.

The invariant this module exists to protect: **publishing a new version never
changes what an existing item means.** Items pin ``category_schema_version`` at
write time and are always validated against that pinned version, so a v1 item
stays valid forever even after v2 removes half its fields. Immutability of the
record is the product; a schema edit that retroactively invalidated history
would quietly undo it.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog import registry
from app.catalog.schemas import (
    CreateCategoryRequest,
    CreateVersionRequest,
    SchemaDiff,
    UpdateCategoryRequest,
)
from app.catalog.validator import validate_attributes, validate_schema_document
from app.core.errors import ConflictError, ErrorCode, NotFoundError, ValidationError
from app.db.models.catalog import GICategory

__all__ = [
    "create_category",
    "create_version",
    "diff_schemas",
    "get_version",
    "latest_version",
    "list_categories",
    "list_versions",
    "update_category",
    "validate_item_attributes",
]


# ---------------------------------------------------------------- reads


async def list_categories(
    session: AsyncSession, include_inactive: bool = False, limit: int = 50, offset: int = 0
) -> list[GICategory]:
    """Latest version of each slug, newest first.

    ``DISTINCT ON`` rather than a group-by join: this returns whole rows, and
    the alternative needs a self-join that Postgres plans worse.
    """
    statement = (
        select(GICategory)
        .distinct(GICategory.slug)
        .order_by(GICategory.slug, GICategory.schema_version.desc())
    )
    if not include_inactive:
        statement = statement.where(GICategory.is_active.is_(True))

    rows = list((await session.execute(statement)).scalars().all())
    rows.sort(key=lambda row: row.created_at, reverse=True)
    return rows[offset : offset + limit]


async def latest_version(
    session: AsyncSession, slug: str, active_only: bool = True
) -> GICategory:
    """Highest version of *slug*, or raise 404."""
    statement = select(GICategory).where(GICategory.slug == slug)
    if active_only:
        statement = statement.where(GICategory.is_active.is_(True))
    row = (
        await session.execute(statement.order_by(GICategory.schema_version.desc()).limit(1))
    ).scalar_one_or_none()

    if row is None:
        raise NotFoundError(
            code=ErrorCode.CATEGORY_NOT_FOUND, message=f"no category with slug '{slug}'"
        )
    return row


async def get_version(session: AsyncSession, slug: str, version: int) -> GICategory:
    """One pinned version, active or not.

    Retired versions still resolve: existing items reference them, and a
    verification that 404s because a category was retired would be a broken
    provenance record.
    """
    row = (
        await session.execute(
            select(GICategory).where(
                GICategory.slug == slug, GICategory.schema_version == version
            )
        )
    ).scalar_one_or_none()

    if row is None:
        raise NotFoundError(
            code=ErrorCode.CATEGORY_VERSION_NOT_FOUND,
            message=f"category '{slug}' has no version {version}",
        )
    return row


async def list_versions(session: AsyncSession, slug: str) -> list[GICategory]:
    """Every version of a slug, oldest first. Raises 404 for an unknown slug."""
    rows = list(
        (
            await session.execute(
                select(GICategory)
                .where(GICategory.slug == slug)
                .order_by(GICategory.schema_version)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        raise NotFoundError(
            code=ErrorCode.CATEGORY_NOT_FOUND, message=f"no category with slug '{slug}'"
        )
    return rows


# ---------------------------------------------------------------- writes


async def create_category(
    session: AsyncSession, payload: CreateCategoryRequest, actor_id: uuid.UUID
) -> GICategory:
    """Create a category at v1."""
    normalized = validate_schema_document(payload.attribute_schema)

    existing = (
        await session.execute(
            select(func.count()).select_from(GICategory).where(GICategory.slug == payload.slug)
        )
    ).scalar_one()
    if existing:
        raise ConflictError(
            code=ErrorCode.CATEGORY_SLUG_EXISTS,
            message=f"category '{payload.slug}' already exists; publish a new version instead",
            details={"slug": payload.slug},
        )

    category = GICategory(
        slug=payload.slug,
        display_name=payload.display_name,
        is_textile=payload.is_textile,
        quantity_unit=payload.quantity_unit,
        attribute_schema=normalized,
        schema_version=1,
        is_active=True,
        created_by=actor_id,
    )
    session.add(category)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            code=ErrorCode.CATEGORY_SLUG_EXISTS,
            message=f"category '{payload.slug}' already exists",
        ) from exc
    return category


async def create_version(
    session: AsyncSession, slug: str, payload: CreateVersionRequest, actor_id: uuid.UUID
) -> tuple[GICategory, SchemaDiff]:
    """Publish v(n+1). Returns the new row and what changed."""
    normalized = validate_schema_document(payload.attribute_schema)

    versions = await list_versions(session, slug)
    previous = versions[-1]

    category = GICategory(
        slug=slug,
        # Metadata is inherited: a new version changes the schema, nothing else.
        display_name=previous.display_name,
        is_textile=previous.is_textile,
        quantity_unit=previous.quantity_unit,
        attribute_schema=normalized,
        schema_version=previous.schema_version + 1,
        is_active=previous.is_active,
        created_by=actor_id,
    )
    session.add(category)
    try:
        await session.flush()
    except IntegrityError as exc:
        # Two operators publishing simultaneously; the unique index decides.
        await session.rollback()
        raise ConflictError(
            code=ErrorCode.CONFLICT,
            message="another version was published concurrently; retry",
        ) from exc

    return category, diff_schemas(dict(previous.attribute_schema), normalized)


async def update_category(
    session: AsyncSession, slug: str, payload: UpdateCategoryRequest
) -> GICategory:
    """Update display name and active flag across every version of a slug.

    Applied to all versions, not just the latest: retiring "the category" means
    retiring it, and leaving v1 active while v2 is retired would be a way to
    keep registering items against a category somebody believed was closed.
    """
    versions = await list_versions(session, slug)

    for version in versions:
        if payload.display_name is not None:
            version.display_name = payload.display_name
        if payload.is_active is not None:
            version.is_active = payload.is_active

    await session.flush()
    return versions[-1]


# ---------------------------------------------------------------- diffing


def diff_schemas(previous: dict[str, Any], current: dict[str, Any]) -> SchemaDiff:
    """Summarise what changed between two schemas.

    Only the root's ``properties`` and ``required`` are compared. That is where
    the changes an operator cares about live, and a full structural diff would
    produce noise nobody reads at the moment of publishing.
    """
    before = previous.get("properties", {}) or {}
    after = current.get("properties", {}) or {}
    before_required = set(previous.get("required", []) or [])
    after_required = set(current.get("required", []) or [])

    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))

    type_changed = []
    for name in sorted(set(before) & set(after)):
        old_type = before[name].get("type") if isinstance(before[name], dict) else None
        new_type = after[name].get("type") if isinstance(after[name], dict) else None
        if old_type != new_type:
            type_changed.append(
                {"field": name, "from": str(old_type), "to": str(new_type)}
            )

    return SchemaDiff(
        added=added,
        removed=removed,
        type_changed=type_changed,
        # Newly required fields break existing payloads that omitted them.
        newly_required=sorted(after_required - before_required),
        no_longer_required=sorted(before_required - after_required),
    )


# ---------------------------------------------------------------- validation


async def validate_item_attributes(
    session: AsyncSession,
    slug: str,
    attributes: Any,
    schema_version: int | None = None,
    for_new_item: bool = True,
) -> registry.CompiledCategory:
    """Validate *attributes* against a category, returning the version used.

    With ``schema_version`` set, that exact version is used -- which is how an
    existing item is re-verified against the schema it was written under, long
    after newer versions exist.

    With it unset, the latest *active* version is used and a retired category is
    refused. A retired category must not silently fall back to an older active
    version: somebody retired it to stop new items.
    """
    if schema_version is not None:
        compiled = await registry.get_compiled(session, slug, schema_version)
        if compiled is None:
            raise NotFoundError(
                code=ErrorCode.CATEGORY_VERSION_NOT_FOUND,
                message=f"category '{slug}' has no version {schema_version}",
            )
    else:
        compiled = await registry.latest_active(session, slug)
        if compiled is None:
            # Distinguish "never existed" from "retired": the operator response
            # is different, and so is the weaver's.
            any_version = await registry.get_compiled(session, slug)
            if any_version is not None and for_new_item:
                raise ValidationError(
                    code=ErrorCode.CATEGORY_RETIRED,
                    status=422,
                    message=f"category '{slug}' is retired and accepts no new items",
                    details={"slug": slug},
                )
            raise NotFoundError(
                code=ErrorCode.CATEGORY_NOT_FOUND, message=f"no category with slug '{slug}'"
            )

    validate_attributes(compiled.validator, attributes)
    return compiled
