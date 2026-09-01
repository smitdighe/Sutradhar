"""Idempotent replay against a real PostgreSQL instance."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import now
from app.core.crypto_shred import new_salt
from app.core.errors import ConflictError, ErrorCode
from app.core.idempotency import RETENTION, begin, complete, purge_expired
from app.db.models.enums import UserRole, UserStatus
from app.db.models.ops import IdempotencyKey
from app.db.models.user import User

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BODY: dict[str, Any] = {"quantity": "5.5000", "category": "banarasi-brocade"}
OTHER_BODY: dict[str, Any] = {"quantity": "9.0000", "category": "banarasi-brocade"}


@pytest_asyncio.fixture
async def user(session: AsyncSession) -> User:
    """A committed user, since idempotency_keys.user_id is a real foreign key."""
    record = User(
        email=f"weaver-{uuid.uuid4().hex[:8]}@example.com",
        display_name="Test Weaver",
        identity_salt=new_salt(),
        role=UserRole.WEAVER,
        status=UserStatus.ACTIVE,
    )
    session.add(record)
    await session.commit()
    return record


class TestFirstUse:
    async def test_new_key_is_not_a_replay(self, session: AsyncSession, user: User) -> None:
        outcome = await begin(session, user.id, "key-1", BODY)
        assert outcome.replay is False
        assert outcome.response_status is None

    async def test_new_key_records_the_request_hash(
        self, session: AsyncSession, user: User
    ) -> None:
        await begin(session, user.id, "key-2", BODY)
        await session.commit()
        record = (
            await session.execute(select(IdempotencyKey).where(IdempotencyKey.key == "key-2"))
        ).scalar_one()
        assert record.request_hash.startswith("0x")
        assert record.user_id == user.id


class TestReplay:
    async def test_same_key_and_body_replays_the_stored_response(
        self, session: AsyncSession, user: User
    ) -> None:
        first = await begin(session, user.id, "key-3", BODY)
        await complete(session, first.record_id, 201, {"id": "item-abc"})
        await session.commit()

        second = await begin(session, user.id, "key-3", BODY)
        assert second.replay is True
        assert second.response_status == 201
        assert second.response_body == {"id": "item-abc"}

    async def test_replay_does_not_create_a_second_record(
        self, session: AsyncSession, user: User
    ) -> None:
        first = await begin(session, user.id, "key-4", BODY)
        await complete(session, first.record_id, 200, {"ok": True})
        await session.commit()
        await begin(session, user.id, "key-4", BODY)
        await session.commit()

        rows = (
            (await session.execute(select(IdempotencyKey).where(IdempotencyKey.key == "key-4")))
            .scalars()
            .all()
        )
        assert len(rows) == 1

    async def test_key_order_in_the_body_does_not_matter(
        self, session: AsyncSession, user: User
    ) -> None:
        # The request hash goes through canonicalization, so a client that
        # serialises its JSON keys in a different order still gets its replay.
        first = await begin(session, user.id, "key-5", BODY)
        await complete(session, first.record_id, 201, {"id": "x"})
        await session.commit()

        reordered = {"category": BODY["category"], "quantity": BODY["quantity"]}
        second = await begin(session, user.id, "key-5", reordered)
        assert second.replay is True

    async def test_an_unfinished_attempt_is_retried_not_replayed(
        self, session: AsyncSession, user: User
    ) -> None:
        # The first attempt claimed the key and died before recording a
        # response. Replaying a null response would return nothing at all.
        first = await begin(session, user.id, "key-6", BODY)
        await session.commit()

        second = await begin(session, user.id, "key-6", BODY)
        assert second.replay is False
        assert second.record_id == first.record_id


class TestConflict:
    async def test_same_key_different_body_raises_409(
        self, session: AsyncSession, user: User
    ) -> None:
        first = await begin(session, user.id, "key-7", BODY)
        await complete(session, first.record_id, 201, {"id": "x"})
        await session.commit()

        with pytest.raises(ConflictError) as caught:
            await begin(session, user.id, "key-7", OTHER_BODY)

        error = caught.value
        assert error.status == 409
        assert error.code == ErrorCode.IDEMPOTENCY_KEY_REUSED
        assert error.details == {"key": "key-7"}

    async def test_conflict_is_raised_even_before_the_response_is_recorded(
        self, session: AsyncSession, user: User
    ) -> None:
        await begin(session, user.id, "key-8", BODY)
        await session.commit()
        with pytest.raises(ConflictError):
            await begin(session, user.id, "key-8", OTHER_BODY)

    async def test_a_tiny_body_difference_is_still_a_conflict(
        self, session: AsyncSession, user: User
    ) -> None:
        await begin(session, user.id, "key-9", {"quantity": "5.5000"})
        await session.commit()
        with pytest.raises(ConflictError):
            await begin(session, user.id, "key-9", {"quantity": "5.5001"})


class TestScoping:
    async def test_the_same_key_is_independent_across_users(
        self, session: AsyncSession, user: User
    ) -> None:
        other = User(
            email=f"other-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Other Weaver",
            identity_salt=new_salt(),
            role=UserRole.WEAVER,
            status=UserStatus.ACTIVE,
        )
        session.add(other)
        await session.commit()

        first = await begin(session, user.id, "shared-key", BODY)
        await complete(session, first.record_id, 201, {"id": "mine"})
        await session.commit()

        # A different user with a colliding key must not see the first user's
        # response, and must not be blocked by it either.
        second = await begin(session, other.id, "shared-key", OTHER_BODY)
        assert second.replay is False
        assert second.record_id != first.record_id

    async def test_different_keys_are_independent(
        self, session: AsyncSession, user: User
    ) -> None:
        first = await begin(session, user.id, "key-a", BODY)
        await complete(session, first.record_id, 201, {"id": "a"})
        await session.commit()
        second = await begin(session, user.id, "key-b", BODY)
        assert second.replay is False


class TestExpiry:
    async def test_purge_removes_records_past_retention(
        self, session: AsyncSession, user: User
    ) -> None:
        outcome = await begin(session, user.id, "key-old", BODY)
        await complete(session, outcome.record_id, 201, {"id": "x"})
        await session.commit()

        record = await session.get(IdempotencyKey, outcome.record_id)
        assert record is not None
        record.created_at = now() - RETENTION - RETENTION
        await session.commit()

        assert await purge_expired(session) == 1
        await session.commit()
        assert await session.get(IdempotencyKey, outcome.record_id) is None

    async def test_purge_keeps_recent_records(
        self, session: AsyncSession, user: User
    ) -> None:
        outcome = await begin(session, user.id, "key-fresh", BODY)
        await session.commit()
        assert await purge_expired(session) == 0
        assert await session.get(IdempotencyKey, outcome.record_id) is not None
