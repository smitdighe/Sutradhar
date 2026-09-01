"""In-process cache of compiled category validators.

Compiling a JSON Schema is not free, and item registration validates against one
on every write. Compiled validators are cached by ``(slug, version)`` and reused.

**Why an in-process cache is correct here, and when it stops being.** The
deployment target is a single Render free-tier instance, so this process is the
only one holding the cache -- a write invalidates it directly and no other
process can be holding a stale copy. That assumption is checked at startup by
:func:`assert_single_instance`.

On a multi-instance deploy this becomes wrong: instance A publishes v2, instance
B keeps serving v1 from its own cache until it happens to restart. The fix is
either a short TTL (simple, briefly stale) or pub/sub invalidation over the
database's LISTEN/NOTIFY (correct, more moving parts). Neither is built now --
building for a scale this project does not have would be the same mistake as
not thinking about it at all. This docstring is the handover note.

Cold start is lazy: an empty registry loads on first use rather than answering
500, so a fresh process serves the first request that reaches it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.validator import compile_schema
from app.core.logging import get_logger
from app.db.models.catalog import GICategory

__all__ = [
    "CompiledCategory",
    "assert_single_instance",
    "get_compiled",
    "invalidate",
    "reload",
    "stats",
]

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CompiledCategory:
    """A category's identity plus its ready-to-use validator."""

    id: uuid.UUID
    slug: str
    schema_version: int
    display_name: str
    quantity_unit: str
    is_textile: bool
    is_active: bool
    schema: dict[str, Any]
    validator: Draft202012Validator


_cache: dict[tuple[str, int], CompiledCategory] = {}
_loaded = False


def assert_single_instance() -> None:
    """Log the assumption this cache rests on, loudly enough to be noticed.

    Not a hard failure: a second instance is a deployment decision, not a bug
    the process can detect. What it must not do is fail silently, so the
    condition is stated at startup where somebody scaling out will see it.
    """
    from app.config import get_settings

    settings = get_settings()
    if not settings.scheduler_enabled:
        # SCHEDULER_ENABLED=false is how a second instance would normally be
        # run (workers on one node only), which makes it a decent proxy for
        # "this is not the single instance".
        logger.warning(
            "category_registry_multi_instance_risk",
            detail=(
                "SCHEDULER_ENABLED is false, which usually means more than one "
                "instance is running. The category registry is an in-process "
                "cache: a category published on another instance will not be "
                "visible here until this process restarts. Add a TTL or "
                "LISTEN/NOTIFY invalidation before scaling out."
            ),
        )


def _compile(row: GICategory) -> CompiledCategory:
    return CompiledCategory(
        id=row.id,
        slug=row.slug,
        schema_version=row.schema_version,
        display_name=row.display_name,
        quantity_unit=row.quantity_unit,
        is_textile=row.is_textile,
        is_active=row.is_active,
        schema=dict(row.attribute_schema),
        validator=compile_schema(dict(row.attribute_schema)),
    )


async def reload(session: AsyncSession) -> int:
    """Rebuild the cache from the database. Returns the entry count.

    Called after any category write. Rebuilds wholesale rather than patching
    the affected key: the table is small, and a full rebuild cannot drift.
    """
    global _loaded
    rows = (await session.execute(select(GICategory))).scalars().all()
    rebuilt = {(row.slug, row.schema_version): _compile(row) for row in rows}
    _cache.clear()
    _cache.update(rebuilt)
    _loaded = True
    logger.info("category_registry_reloaded", entries=len(_cache))
    return len(_cache)


def invalidate() -> None:
    """Drop everything. The next lookup reloads."""
    _cache.clear()
    global _loaded
    _loaded = False


async def _ensure_loaded(session: AsyncSession) -> None:
    if not _loaded:
        await reload(session)


async def get_compiled(
    session: AsyncSession, slug: str, version: int | None = None
) -> CompiledCategory | None:
    """Return a compiled category, loading the cache if this is a cold start.

    ``version=None`` means the highest version, which is what an item
    registration without an explicit pin should get.
    """
    await _ensure_loaded(session)

    if version is not None:
        return _cache.get((slug, version))

    candidates = [entry for (entry_slug, _), entry in _cache.items() if entry_slug == slug]
    if not candidates:
        return None
    return max(candidates, key=lambda entry: entry.schema_version)


async def latest_active(session: AsyncSession, slug: str) -> CompiledCategory | None:
    """Highest *active* version of a slug, or None.

    Distinct from :func:`get_compiled`: a retired category still resolves for
    reads, but registering new items against it must not fall back to an older
    active version and quietly succeed.
    """
    await _ensure_loaded(session)
    candidates = [
        entry
        for (entry_slug, _), entry in _cache.items()
        if entry_slug == slug and entry.is_active
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda entry: entry.schema_version)


def stats() -> dict[str, Any]:
    """Cache contents, for /readyz and tests."""
    return {
        "loaded": _loaded,
        "entries": len(_cache),
        "slugs": sorted({slug for slug, _ in _cache}),
    }
