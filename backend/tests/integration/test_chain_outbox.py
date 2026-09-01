"""Outbox mechanics: ordering, backoff, dead letters, stale locks, idempotency.

Against real Postgres, because ``FOR UPDATE SKIP LOCKED`` is the entire design
and no in-memory queue reproduces it.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chain.outbox import OutboxRepository, enqueue_job
from app.core.clock import now
from app.db.models.enums import OutboxJobType, OutboxStatus
from app.db.models.ops import DeadLetter
from app.db.models.outbox import Outbox
from tests.fakes.chain_harness import build_settings

pytestmark = [pytest.mark.integration, pytest.mark.chain]


async def queue(session: AsyncSession, key: str, **payload: Any) -> bool:
    created = await enqueue_job(
        session,
        job_type=OutboxJobType.ANCHOR_ITEM,
        payload={"item_hash": key, **payload},
        dedupe_key=key,
    )
    await session.commit()
    return created


def repo(session_factory: async_sessionmaker[AsyncSession], **overrides: Any) -> OutboxRepository:
    return OutboxRepository(session_factory, build_settings(**overrides), worker_id="w1")


class TestClaiming:
    async def test_drains_in_creation_order(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        for index in range(5):
            await queue(session, f"0x{index:064x}", order=index)

        claimed = await repo(session_factory).claim()

        assert [job.payload["order"] for job in claimed] == [0, 1, 2, 3, 4]

    async def test_claiming_marks_rows_in_flight_and_stamps_the_worker(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        await queue(session, "0x" + "aa" * 32)

        claimed = await repo(session_factory).claim()
        assert len(claimed) == 1

        row = (await session.execute(select(Outbox))).scalar_one()
        await session.refresh(row)
        assert row.status == OutboxStatus.IN_FLIGHT
        assert row.locked_by == "w1"
        assert row.locked_at is not None

    async def test_two_workers_split_the_queue_without_overlap(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        for index in range(10):
            await queue(session, f"0x{index:064x}")

        one = OutboxRepository(session_factory, build_settings(), worker_id="a")
        two = OutboxRepository(session_factory, build_settings(), worker_id="b")

        first, second = await asyncio.gather(one.claim(5), two.claim(5))

        ids = [job.id for job in first] + [job.id for job in second]
        # SKIP LOCKED is what guarantees this: no job is handed to two workers.
        assert len(ids) == len(set(ids)) == 10

    async def test_a_job_not_yet_due_is_not_claimed(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        await queue(session, "0x" + "bb" * 32)
        row = (await session.execute(select(Outbox))).scalar_one()
        row.next_attempt_at = now().replace(year=now().year + 1)
        await session.commit()

        assert await repo(session_factory).claim() == []


class TestRetryAndDeadLetter:
    async def test_failure_schedules_a_growing_backoff(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        await queue(session, "0x" + "cc" * 32)
        repository = repo(session_factory, outbox_max_attempts=10)
        job = (await repository.claim())[0]

        gaps: list[float] = []
        for attempt in range(4):
            await repository.fail(job.id, f"boom {attempt}")
            row = (await session.execute(select(Outbox))).scalar_one()
            await session.refresh(row)
            gaps.append((row.next_attempt_at - now()).total_seconds())
            row.next_attempt_at = now()
            await session.commit()
            job = (await repository.claim())[0]

        # 2**n with jitter: strictly growing even at the jitter extremes,
        # because 0.8 * 2**(n+1) > 1.2 * 2**n.
        assert gaps == sorted(gaps), gaps
        assert gaps[-1] > gaps[0]

    async def test_exhausting_attempts_dead_letters_with_the_whole_error_chain(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        await queue(session, "0x" + "dd" * 32)
        repository = repo(session_factory, outbox_max_attempts=3)

        errors = ["rpc unreachable", "rpc unreachable again", "nonce too low"]
        for message in errors:
            job = (await repository.claim())[0]
            await repository.fail(job.id, message)
            row = (await session.execute(select(Outbox))).scalar_one()
            await session.refresh(row)
            row.next_attempt_at = now()
            await session.commit()

        row = (await session.execute(select(Outbox))).scalar_one()
        await session.refresh(row)
        assert row.status == OutboxStatus.DEAD
        assert row.attempts == 3

        letter = (await session.execute(select(DeadLetter))).scalar_one()
        assert letter.attempts == 3
        # Every attempt, not just the last: the first error is usually the cause
        # and the last is usually the symptom.
        for message in errors:
            assert message in letter.error_chain
        assert letter.error_chain.count("attempt ") == 3
        assert letter.original_payload["dedupe_key"] == "0x" + "dd" * 32

    async def test_kill_parks_immediately_without_burning_attempts(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        await queue(session, "0x" + "ee" * 32)
        repository = repo(session_factory, outbox_max_attempts=6)
        job = (await repository.claim())[0]

        await repository.kill(job.id, "transaction reverted on chain: NotWriter()")

        row = (await session.execute(select(Outbox))).scalar_one()
        await session.refresh(row)
        assert row.status == OutboxStatus.DEAD
        assert row.attempts == 1
        letter = (await session.execute(select(DeadLetter))).scalar_one()
        assert "NotWriter" in letter.error_chain

    async def test_release_requeues_without_counting_an_attempt(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        await queue(session, "0x" + "ff" * 32)
        repository = repo(session_factory)
        job = (await repository.claim())[0]

        await repository.release(job.id, "CHAIN_WRITE_ENABLED=false")

        row = (await session.execute(select(Outbox))).scalar_one()
        await session.refresh(row)
        assert row.status == OutboxStatus.QUEUED
        # An outage must not consume the retry budget of every queued job.
        assert row.attempts == 0
        assert row.locked_by is None


class TestStaleLocks:
    async def test_a_stale_lock_is_reclaimed_after_the_threshold(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        await queue(session, "0x" + "11" * 32)
        repository = repo(session_factory, outbox_lock_stale_seconds=600)
        await repository.claim()

        row = (await session.execute(select(Outbox))).scalar_one()
        await session.refresh(row)
        # Simulate a worker that died holding the lease.
        row.locked_at = now().replace(year=now().year - 1)
        await session.commit()

        assert await repository.reclaim_stale() == 1

        await session.refresh(row)
        assert row.status == OutboxStatus.QUEUED
        assert row.locked_at is None
        assert row.locked_by is None

    async def test_a_fresh_lock_is_left_alone(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        await queue(session, "0x" + "22" * 32)
        repository = repo(session_factory, outbox_lock_stale_seconds=600)
        await repository.claim()

        assert await repository.reclaim_stale() == 0

        row = (await session.execute(select(Outbox))).scalar_one()
        await session.refresh(row)
        assert row.status == OutboxStatus.IN_FLIGHT


class TestIdempotentEnqueue:
    async def test_the_same_dedupe_key_produces_one_row(
        self, session: AsyncSession
    ) -> None:
        key = "0x" + "33" * 32

        assert await queue(session, key) is True
        assert await queue(session, key) is False
        assert await queue(session, key) is False

        rows = (await session.execute(select(Outbox))).scalars().all()
        # Re-registering the same item must not spend gas twice.
        assert len(rows) == 1

    async def test_concurrent_enqueues_of_one_key_collide_at_the_index(
        self, session_factory: Any
    ) -> None:
        key = "0x" + "44" * 32

        async def attempt() -> bool:
            async with session_factory() as session:
                created = await enqueue_job(
                    session,
                    job_type=OutboxJobType.ANCHOR_ITEM,
                    payload={"item_hash": key},
                    dedupe_key=key,
                )
                await session.commit()
                return created

        results = await asyncio.gather(*(attempt() for _ in range(8)))

        assert sum(1 for created in results if created) == 1

        async with session_factory() as session:
            rows = (await session.execute(select(Outbox))).scalars().all()
            assert len(rows) == 1


class TestRequeue:
    async def test_requeue_for_hash_revives_a_finished_job(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        key = "0x" + "55" * 32
        await queue(session, key)
        repository = repo(session_factory)
        job = (await repository.claim())[0]
        await repository.complete(job.id)

        assert await repository.requeue_for_hash(key) is True

        row = (await session.execute(select(Outbox))).scalar_one()
        await session.refresh(row)
        assert row.status == OutboxStatus.QUEUED
        assert row.attempts == 0

    async def test_requeue_for_an_unknown_hash_reports_false(
        self, session_factory: Any
    ) -> None:
        assert await repo(session_factory).requeue_for_hash("0x" + "66" * 32) is False

    async def test_completing_a_missing_job_does_not_raise(
        self, session_factory: Any
    ) -> None:
        await repo(session_factory).complete(uuid.uuid4())
