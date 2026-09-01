"""Rate limiting against a real PostgreSQL instance.

The whole reason this limiter lives in Postgres is the atomicity of
``INSERT ... ON CONFLICT DO UPDATE ... RETURNING``. That guarantee cannot be
tested against a mock or SQLite -- it either holds under genuine concurrent
connections or it does not.
"""

from __future__ import annotations

import asyncio

import pytest
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ErrorCode, RateLimitError
from app.core.ratelimit import consume, window_bounds
from app.db.models.ops import RateLimitBucket

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SCOPE = "login"
WINDOW = 60


class TestCounting:
    async def test_first_call_returns_one(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        assert await consume(session_factory, SCOPE, "ip-a", limit=5, window_seconds=WINDOW) == 1

    async def test_sequential_calls_increment(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        counts = [
            await consume(session_factory, SCOPE, "ip-b", limit=100, window_seconds=WINDOW)
            for _ in range(10)
        ]
        assert counts == list(range(1, 11))

    async def test_identifiers_are_counted_separately(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await consume(session_factory, SCOPE, "ip-c", limit=100, window_seconds=WINDOW)
        await consume(session_factory, SCOPE, "ip-c", limit=100, window_seconds=WINDOW)
        assert await consume(session_factory, SCOPE, "ip-d", limit=100, window_seconds=WINDOW) == 1

    async def test_scopes_are_counted_separately(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await consume(session_factory, "login", "ip-e", limit=100, window_seconds=WINDOW)
        count = await consume(
            session_factory, "register", "ip-e", limit=100, window_seconds=WINDOW
        )
        assert count == 1


class TestConcurrency:
    async def test_concurrent_increments_are_exact(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # The lost-update test. A naive SELECT-then-UPDATE limiter passes the
        # sequential tests above and fails this one, which is exactly the bug
        # an attacker exploits by firing requests in parallel.
        attempts = 50
        results = await asyncio.gather(
            *(
                consume(session_factory, SCOPE, "ip-race", limit=10_000, window_seconds=WINDOW)
                for _ in range(attempts)
            )
        )

        assert sorted(results) == list(range(1, attempts + 1))

        async with session_factory() as session:
            bucket = (
                await session.execute(
                    select(RateLimitBucket).where(RateLimitBucket.identifier == "ip-race")
                )
            ).scalar_one()
            assert bucket.count == attempts

    async def test_concurrent_calls_past_the_limit_raise_the_right_number_of_times(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        limit, attempts = 10, 30
        outcomes = await asyncio.gather(
            *(
                consume(session_factory, SCOPE, "ip-over", limit=limit, window_seconds=WINDOW)
                for _ in range(attempts)
            ),
            return_exceptions=True,
        )
        rejected = [item for item in outcomes if isinstance(item, RateLimitError)]
        assert len(rejected) == attempts - limit


class TestLimitEnforcement:
    async def test_calls_up_to_the_limit_pass(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        for _ in range(5):
            await consume(session_factory, SCOPE, "ip-f", limit=5, window_seconds=WINDOW)

    async def test_the_call_past_the_limit_raises(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        for _ in range(5):
            await consume(session_factory, SCOPE, "ip-g", limit=5, window_seconds=WINDOW)
        with pytest.raises(RateLimitError) as caught:
            await consume(session_factory, SCOPE, "ip-g", limit=5, window_seconds=WINDOW)

        error = caught.value
        assert error.code == ErrorCode.RATE_LIMITED
        assert error.status == 429
        assert 0 < error.retry_after <= WINDOW
        assert error.details is not None
        assert error.details["retry_after"] == error.retry_after
        assert error.details["limit"] == 5

    async def test_a_rejected_call_still_counts(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # Hammering a limited endpoint must not be free, or the limiter can be
        # held open indefinitely. The third call is rejected, and the rejection
        # is still recorded.
        await consume(session_factory, SCOPE, "ip-h", limit=2, window_seconds=WINDOW)
        await consume(session_factory, SCOPE, "ip-h", limit=2, window_seconds=WINDOW)
        with pytest.raises(RateLimitError):
            await consume(session_factory, SCOPE, "ip-h", limit=2, window_seconds=WINDOW)

        async with session_factory() as session:
            bucket = (
                await session.execute(
                    select(RateLimitBucket).where(RateLimitBucket.identifier == "ip-h")
                )
            ).scalar_one()
            assert bucket.count == 3


class TestWindows:
    async def test_window_rollover_resets_the_counter(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        with freeze_time("2026-08-26 12:00:00") as frozen:
            for _ in range(5):
                await consume(session_factory, SCOPE, "ip-window", limit=5, window_seconds=WINDOW)
            with pytest.raises(RateLimitError):
                await consume(session_factory, SCOPE, "ip-window", limit=5, window_seconds=WINDOW)

            frozen.move_to("2026-08-26 12:01:30")
            assert (
                await consume(session_factory, SCOPE, "ip-window", limit=5, window_seconds=WINDOW)
                == 1
            )

    async def test_a_new_window_is_a_new_row(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        with freeze_time("2026-08-26 12:00:00") as frozen:
            await consume(session_factory, SCOPE, "ip-rows", limit=100, window_seconds=WINDOW)
            frozen.move_to("2026-08-26 12:05:00")
            await consume(session_factory, SCOPE, "ip-rows", limit=100, window_seconds=WINDOW)

        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(RateLimitBucket).where(RateLimitBucket.identifier == "ip-rows")
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 2

    async def test_windows_are_aligned_to_the_epoch(self) -> None:
        # Aligned, not sliding: everyone's window boundary is the same instant,
        # which is what makes the bucket key computable without a read.
        from datetime import UTC, datetime

        at = datetime(2026, 8, 26, 12, 0, 37, tzinfo=UTC)
        start, end = window_bounds(at, 60)
        assert start == datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)
        assert end == datetime(2026, 8, 26, 12, 1, 0, tzinfo=UTC)

    async def test_zero_window_is_rejected(self) -> None:
        from datetime import UTC, datetime

        with pytest.raises(ValueError, match="must be positive"):
            window_bounds(datetime.now(UTC), 0)
