"""Budget tracking for metered external services.

Free tiers have hard ceilings, and hitting one mid-demo means the service looks
broken rather than thrifty. This is the mechanism for staying under them; the
phases that call Alchemy and Pinata wire it up.

Two shapes, both served by ``quota_usage``:

* **Periodic** -- Alchemy compute units, which reset monthly. One row per
  period, so a rollover starts a fresh count and last period stays readable.
* **Cumulative** -- Pinata storage bytes, which only ever grow. ``period_start``
  is pinned to the Unix epoch and the single row lives forever.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import now
from app.core.errors import ErrorCode, RateLimitError
from app.db.models.ops import QuotaUsage

__all__ = ["EPOCH", "QuotaTracker", "month_start"]

SessionFactory = async_sessionmaker[AsyncSession]

# Sentinel period for quotas that never reset.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def month_start(at: datetime | None = None) -> datetime:
    """First instant of the UTC month containing *at*."""
    moment = at or now()
    return datetime(moment.year, moment.month, 1, tzinfo=UTC)


class QuotaTracker:
    """Accounting for one named budget.

    ``periodic=True`` buckets usage by calendar month; ``False`` accumulates
    forever against a single row.
    """

    def __init__(
        self,
        name: str,
        budget: Decimal | int,
        session_factory: SessionFactory,
        periodic: bool = True,
    ) -> None:
        self.name = name
        self.budget = Decimal(budget)
        self.periodic = periodic
        self._session_factory = session_factory

    def _period(self) -> datetime:
        return month_start() if self.periodic else EPOCH

    async def _ensure_row(self, session: AsyncSession) -> QuotaUsage:
        period = self._period()
        statement = (
            insert(QuotaUsage)
            .values(name=self.name, period_start=period, used=Decimal(0), budget=self.budget)
            .on_conflict_do_nothing(index_elements=[QuotaUsage.name, QuotaUsage.period_start])
        )
        await session.execute(statement)
        row = (
            await session.execute(
                select(QuotaUsage).where(
                    QuotaUsage.name == self.name, QuotaUsage.period_start == period
                )
            )
        ).scalar_one()
        return row

    async def used(self) -> Decimal:
        """Consumption so far in the current period."""
        async with self._session_factory() as session:
            row = await self._ensure_row(session)
            await session.commit()
            return Decimal(row.used)

    async def remaining(self) -> Decimal:
        """Budget left in the current period. Never negative."""
        return max(Decimal(0), self.budget - await self.used())

    async def would_exceed(self, amount: Decimal | int) -> bool:
        """True when consuming *amount* now would cross the budget."""
        return (await self.used()) + Decimal(amount) > self.budget

    async def consume(self, amount: Decimal | int, strict: bool = False) -> Decimal:
        """Add *amount* to the current period and return the new total.

        Read-modify-write in one statement so concurrent workers cannot lose an
        increment. With ``strict=True`` a consumption that would cross the
        budget raises ``QUOTA_EXCEEDED`` and is *not* recorded; the default
        records it and lets the caller decide, because refusing to log usage
        already incurred would understate the real burn.
        """
        delta = Decimal(amount)
        period = self._period()

        statement = (
            insert(QuotaUsage)
            .values(name=self.name, period_start=period, used=delta, budget=self.budget)
            .on_conflict_do_update(
                index_elements=[QuotaUsage.name, QuotaUsage.period_start],
                set_={"used": QuotaUsage.used + delta, "updated_at": now()},
            )
            .returning(QuotaUsage.used)
        )

        if strict and await self.would_exceed(delta):
            raise RateLimitError(
                retry_after=60,
                code=ErrorCode.QUOTA_EXCEEDED,
                message=f"quota '{self.name}' would be exceeded",
                details={"quota": self.name, "budget": str(self.budget)},
            )

        async with self._session_factory() as session:
            total = (await session.execute(statement)).scalar_one()
            await session.commit()
        return Decimal(total)
