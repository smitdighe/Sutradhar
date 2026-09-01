"""Fixed-window rate limiting, counted in Postgres.

No Redis. Render's free tier runs a single instance, the database is already a
hard dependency, and a limiter that goes down with its own datastore is worse
than a slightly less precise one.

Each check is a single statement::

    INSERT INTO rate_limit_buckets (...) VALUES (...)
    ON CONFLICT (scope, identifier, window_start)
    DO UPDATE SET count = rate_limit_buckets.count + 1
    RETURNING count

That is atomic under concurrency -- there is no read-then-write window for two
requests to both observe the same count and both increment it to the same value.

Counting happens in its own short transaction, deliberately separate from the
request's. A request that fails and rolls back must still have consumed its
allowance, or a caller could hammer an endpoint for free by ensuring every
attempt errors.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from fastapi import Request
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import now
from app.core.errors import ErrorCode, RateLimitError
from app.core.hashing import sha256_hex
from app.db.models.ops import RateLimitBucket

__all__ = ["client_identifier", "consume", "rate_limit", "window_bounds"]

SessionFactory = async_sessionmaker[AsyncSession]


def window_bounds(at: datetime, window_seconds: int) -> tuple[datetime, datetime]:
    """Return the ``(start, end)`` of the fixed window containing *at*."""
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    epoch_seconds = int(at.timestamp())
    start_epoch = epoch_seconds - (epoch_seconds % window_seconds)
    start = datetime.fromtimestamp(start_epoch, UTC)
    return start, start + timedelta(seconds=window_seconds)


def client_identifier(request: Request) -> str:
    """Hashed client address, the default limiter identity.

    Hashed rather than raw: this value is written to a table, and a rate-limit
    row is not a good enough reason to hold a plaintext IP address.
    """
    client = request.client
    host = client.host if client is not None else "unknown"
    return sha256_hex(host.encode("utf-8"))


async def consume(
    session_factory: SessionFactory,
    scope: str,
    identifier: str,
    limit: int,
    window_seconds: int,
) -> int:
    """Count one hit against a limiter and return the new count.

    Raises :class:`~app.core.errors.RateLimitError` once the count exceeds
    *limit*, carrying whole seconds until the window rolls over in
    ``details['retry_after']``.
    """
    at = now()
    start, end = window_bounds(at, window_seconds)
    # Retained for one extra window so a cleanup job has an unambiguous cutoff.
    expires_at = end + timedelta(seconds=window_seconds)

    statement = (
        insert(RateLimitBucket)
        .values(
            scope=scope,
            identifier=identifier,
            window_start=start,
            count=1,
            expires_at=expires_at,
        )
        .on_conflict_do_update(
            index_elements=[
                RateLimitBucket.scope,
                RateLimitBucket.identifier,
                RateLimitBucket.window_start,
            ],
            set_={"count": RateLimitBucket.count + 1},
        )
        .returning(RateLimitBucket.count)
    )

    # Its own session, and its own translation of an unreachable database. This
    # runs as a route dependency and can be resolved *before* `get_session`, so
    # it cannot rely on that dependency's 503 to cover it -- without this, every
    # rate-limited route answers 500 during a database outage while the rest of
    # the API answers 503.
    from app.db.session import UNREACHABLE_DATABASE, unavailable

    try:
        async with session_factory() as session:
            count = (await session.execute(statement)).scalar_one()
            await session.commit()
    except UNREACHABLE_DATABASE as exc:
        raise unavailable(exc) from exc

    if count > limit:
        retry_after = max(1, math.ceil((end - at).total_seconds()))
        raise RateLimitError(
            retry_after=retry_after,
            code=ErrorCode.RATE_LIMITED,
            message=f"rate limit exceeded for {scope}",
            details={"scope": scope, "limit": limit, "window_seconds": window_seconds},
        )
    return int(count)


def rate_limit(
    scope: str,
    limit: int,
    window_seconds: int,
    identifier_fn: Callable[[Request], str] = client_identifier,
) -> Callable[[Request], Awaitable[None]]:
    """Build a FastAPI dependency enforcing one limiter.

    Honours ``RATE_LIMIT_ENABLED``: when false the dependency is a no-op, which
    is what tests and local development want rather than having to reason about
    a limiter while debugging something else.
    """

    async def dependency(request: Request) -> None:
        # Imported lazily so importing this module does not construct an engine.
        from app.config import get_settings
        from app.db.session import SessionLocal

        if not get_settings().rate_limit_enabled:
            return
        await consume(SessionLocal, scope, identifier_fn(request), limit, window_seconds)

    return dependency
