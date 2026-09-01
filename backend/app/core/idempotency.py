"""Idempotent request replay, keyed on ``(user_id, Idempotency-Key)``.

A client that times out mid-request cannot tell whether the write landed.
Retrying with the same key must return the original response rather than
performing the write twice.

The request body is hashed and stored alongside the key. Reusing a key for a
*different* body is a client bug -- replaying the old response there would
silently swallow the new request -- so that case raises
``IDEMPOTENCY_KEY_REUSED`` (409) instead.

Unlike the rate limiter, this uses the *caller's* session. The stored response
must commit atomically with the business write it describes, or a crash between
the two leaves a recorded response for work that never happened.

Records expire after 24 hours; :func:`purge_expired` is what the cleanup job
calls.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import now
from app.core.errors import ConflictError, ErrorCode
from app.core.hashing import hash_object
from app.db.models.ops import IdempotencyKey

__all__ = ["RETENTION", "IdempotencyOutcome", "begin", "complete", "purge_expired"]

RETENTION = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class IdempotencyOutcome:
    """The result of claiming a key.

    ``replay`` is true when a completed response was found and should be
    returned verbatim. When it is false the caller does the work, then calls
    :func:`complete` to record the outcome.
    """

    replay: bool
    record_id: uuid.UUID
    response_status: int | None = None
    response_body: dict[str, Any] | None = None


async def begin(
    session: AsyncSession,
    user_id: uuid.UUID,
    key: str,
    request_body: Any,
) -> IdempotencyOutcome:
    """Claim *key* for this request, or return the stored response to replay.

    The claim is a single ``INSERT ... ON CONFLICT DO NOTHING RETURNING id``.
    Read-then-insert would be two statements with a gap, and two simultaneous
    retries of one request — which is exactly the traffic this table exists to
    absorb — would both find nothing, both insert, and the loser would take a
    unique-violation into the generic 500 handler. Losing the insert here is a
    normal outcome, not an error: it means somebody else claimed the key, and
    the row they wrote is then read back and treated like any other replay.
    """
    request_hash = hash_object(request_body)

    claimed = (
        await session.execute(
            insert(IdempotencyKey)
            .values(user_id=user_id, key=key, request_hash=request_hash)
            .on_conflict_do_nothing(
                index_elements=[IdempotencyKey.user_id, IdempotencyKey.key]
            )
            .returning(IdempotencyKey.id)
        )
    ).scalar_one_or_none()

    if claimed is not None:
        return IdempotencyOutcome(replay=False, record_id=claimed)

    existing = (
        await session.execute(
            select(IdempotencyKey).where(
                IdempotencyKey.user_id == user_id, IdempotencyKey.key == key
            )
        )
    ).scalar_one_or_none()
    if existing is None:  # pragma: no cover - the conflict above proves a row
        raise ConflictError(
            code=ErrorCode.IDEMPOTENCY_KEY_REUSED,
            message="idempotency key could not be claimed",
            details={"key": key},
        )

    if existing.request_hash != request_hash:
        raise ConflictError(
            code=ErrorCode.IDEMPOTENCY_KEY_REUSED,
            message="idempotency key was already used for a different request",
            details={"key": key},
        )
    # A null status means the original attempt claimed the key and never
    # finished. Treat it as still in flight and let the caller redo the
    # work; `complete` will fill in the response.
    if existing.response_status is None:
        return IdempotencyOutcome(replay=False, record_id=existing.id)
    return IdempotencyOutcome(
        replay=True,
        record_id=existing.id,
        response_status=existing.response_status,
        response_body=existing.response_body,
    )


async def complete(
    session: AsyncSession,
    record_id: uuid.UUID,
    status: int,
    body: dict[str, Any] | None,
) -> None:
    """Record the response for a claimed key. Commits with the caller."""
    record = await session.get(IdempotencyKey, record_id)
    if record is None:  # pragma: no cover - only reachable if the row was deleted
        return
    record.response_status = status
    record.response_body = body
    await session.flush()


async def purge_expired(session: AsyncSession) -> int:
    """Delete records older than :data:`RETENTION`. Returns the row count."""
    cutoff = now() - RETENTION
    result = cast(
        CursorResult[Any],
        await session.execute(delete(IdempotencyKey).where(IdempotencyKey.created_at < cutoff)),
    )
    return int(result.rowcount or 0)
