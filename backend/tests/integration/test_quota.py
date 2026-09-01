"""The strict consumption path, which is the only way ``QUOTA_EXCEEDED`` is raised.

``QuotaTracker.consume`` records usage by default and refuses it under
``strict=True``. The default path is exercised all over the media and chain
suites; the strict one had no coverage at all, which meant a documented error
code that nothing in the suite could produce. It is documented in
``docs/API_CONTRACT.md``, so it is triggered here.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.core.errors import ErrorCode, RateLimitError
from app.core.quota import QuotaTracker
from app.db.models.ops import QuotaUsage

pytestmark = pytest.mark.asyncio


class TestStrictConsumption:
    async def test_a_consumption_that_would_cross_the_budget_raises_quota_exceeded(
        self, session_factory: Any
    ) -> None:
        tracker = QuotaTracker("test_budget", Decimal(100), session_factory)
        assert await tracker.consume(90) == Decimal("90.0000")

        with pytest.raises(RateLimitError) as caught:
            await tracker.consume(20, strict=True)

        assert caught.value.code is ErrorCode.QUOTA_EXCEEDED
        assert caught.value.status == 429
        # Mirrored into the Retry-After header by the error handler.
        assert caught.value.retry_after == 60
        assert caught.value.details == {
            "retry_after": 60,
            "quota": "test_budget",
            "budget": "100",
        }

    async def test_a_refused_consumption_is_not_recorded(
        self, session_factory: Any
    ) -> None:
        """The refusal has to leave the counter alone.

        A strict refusal that still incremented would spend the budget it just
        declined to spend, and the next honest caller would be refused for
        bytes nobody sent.
        """
        tracker = QuotaTracker("test_budget", Decimal(100), session_factory)
        await tracker.consume(90)

        with pytest.raises(RateLimitError):
            await tracker.consume(20, strict=True)

        async with session_factory() as session:
            used = (
                await session.execute(
                    select(QuotaUsage.used).where(QuotaUsage.name == "test_budget")
                )
            ).scalar_one()
        assert Decimal(used) == Decimal("90.0000")

    async def test_a_consumption_inside_the_budget_is_recorded_under_strict(
        self, session_factory: Any
    ) -> None:
        tracker = QuotaTracker("test_budget", Decimal(100), session_factory)
        assert await tracker.consume(40, strict=True) == Decimal("40.0000")
        assert await tracker.remaining() == Decimal("60.0000")
