"""The operator surface: one endpoint, one screen, everything worth knowing.

``GET /admin/system/status`` answers the questions somebody actually asks five
minutes before a demo -- is anything queued, is anything parked, is the indexer
behind, how much budget is left, are the workers alive, and *is this thing
anchoring or not*.

Two rules it shares with the public surface and with nothing else:

**It never 500s.** Every remote read is wrapped, and an unreachable dependency
is reported as a null with an explanation rather than as an exception. An
operator opens this page precisely when something is wrong, and a status page
that fails when a dependency fails is a status page that is never there when it
is needed.

**It reports, it does not decide.** No knobs, no retry buttons, no requeue. This
is a read. Anything that changes state belongs on an endpoint that says so.

``chain_mode`` is the line to read first. ``postgres_only`` means records are
real and anchors are not -- the ordinary state of this deployment today -- and
saying that plainly is better than a green tick that means nothing.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.schemas import (
    ChainStatusView,
    IndexerStatusView,
    OutboxDepthView,
    QuotaView,
    SchedulerJobView,
    SystemStatusResponse,
)
from app.auth.guards import require_role
from app.auth.roles import Role
from app.chain.indexer import INDEXER_NAME
from app.config import get_settings
from app.core.clock import now
from app.db.models.chain import IndexerCheckpoint
from app.db.models.ops import DeadLetter, QuotaUsage
from app.db.models.outbox import Outbox
from app.db.session import get_session
from app.workers.jobs import LAST_RUN
from app.workers.scheduler import get_scheduler

__all__ = ["router"]

router = APIRouter(
    prefix="/admin/system",
    tags=["admin"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)


def _is_deployed(address: str) -> bool:
    """Whether ``CONTRACT_ADDRESS`` names anything at all."""
    try:
        return int(address, 16) != 0
    except (TypeError, ValueError):
        return False


async def _outbox_depth(session: AsyncSession) -> tuple[list[OutboxDepthView], int]:
    """Queue depth grouped by job type and state. One statement.

    Grouped rather than totalled: "eleven jobs queued" and "eleven jobs dead"
    are opposite situations, and a single depth number cannot tell them apart.
    """
    rows = (
        await session.execute(
            select(Outbox.job_type, Outbox.status, func.count())
            .group_by(Outbox.job_type, Outbox.status)
            .order_by(Outbox.job_type, Outbox.status)
        )
    ).all()
    depths = [
        OutboxDepthView(job_type=str(job_type), status=str(status), count=int(count))
        for job_type, status, count in rows
    ]
    return depths, sum(item.count for item in depths)


async def _dead_letters(session: AsyncSession) -> tuple[int, int]:
    """Total parked jobs, and how many nobody has resolved yet."""
    total = int(
        (await session.execute(select(func.count()).select_from(DeadLetter))).scalar_one()
    )
    unresolved = int(
        (
            await session.execute(
                select(func.count())
                .select_from(DeadLetter)
                .where(DeadLetter.resolved_at.is_(None))
            )
        ).scalar_one()
    )
    return total, unresolved


async def _indexer(session: AsyncSession, request: Request) -> IndexerStatusView:
    """Checkpoint, head, and the distance between them.

    The head read is the only network call this endpoint makes. It is allowed to
    fail: an unreachable node leaves ``head_block`` and ``lag_blocks`` null with
    the reason in ``detail``, which is a truthful answer to "how far behind am
    I" when the answer is "unknown".
    """
    checkpoint = int(
        (
            await session.execute(
                select(IndexerCheckpoint.last_block).where(
                    IndexerCheckpoint.name == INDEXER_NAME
                )
            )
        ).scalar_one_or_none()
        or 0
    )

    runtime = getattr(request.app.state, "chain_runtime", None)
    if runtime is None:
        return IndexerStatusView(
            checkpoint_block=checkpoint,
            detail="no chain runtime in this process; the indexer is not running here",
        )
    if not runtime.client.available:
        return IndexerStatusView(
            checkpoint_block=checkpoint,
            detail="chain RPC unreachable; the head block is unknown",
        )

    try:
        head = int(await runtime.client.block_number())
    except Exception as exc:  # noqa: BLE001 - a status page never fails on a dependency
        return IndexerStatusView(
            checkpoint_block=checkpoint,
            detail=f"head unreadable: {type(exc).__name__}: {exc}"[:200],
        )

    return IndexerStatusView(
        checkpoint_block=checkpoint,
        head_block=head,
        lag_blocks=max(0, head - checkpoint),
    )


async def _quotas(session: AsyncSession) -> list[QuotaView]:
    """Every tracked budget, newest period first."""
    rows = (
        await session.execute(
            select(QuotaUsage).order_by(QuotaUsage.name, QuotaUsage.period_start.desc())
        )
    ).scalars()

    views: list[QuotaView] = []
    for row in rows:
        budget = Decimal(row.budget)
        used = Decimal(row.used)
        # A zero budget is a misconfiguration, not a divide-by-zero.
        percent = float(used / budget * 100) if budget > 0 else 0.0
        views.append(
            QuotaView(
                name=row.name,
                used=str(used),
                budget=str(budget),
                used_percent=round(percent, 1),
                period_start=row.period_start,
            )
        )
    return views


def _jobs() -> tuple[bool, list[SchedulerJobView]]:
    """Registered jobs with their next and last run.

    ``next_run_at`` comes from APScheduler; ``last_run_at`` comes from
    :data:`app.workers.jobs.LAST_RUN`, because APScheduler does not keep one and
    "when did this last actually run" is the more useful of the two.
    """
    scheduler = get_scheduler()
    if scheduler is None or not scheduler.running:
        return False, []

    views: list[SchedulerJobView] = []
    for job in scheduler.get_jobs():
        next_run: datetime | None = getattr(job, "next_run_time", None)
        # The scheduler's job id and the guard's name differ by prefix; match on
        # the suffix so a renamed job shows a null last-run rather than a wrong
        # one.
        last_run = next(
            (stamp for name, stamp in LAST_RUN.items() if job.id.endswith(name)), None
        )
        views.append(
            SchedulerJobView(id=str(job.id), next_run_at=next_run, last_run_at=last_run)
        )
    return True, views


def _chain(request: Request) -> ChainStatusView:
    """Whether an anchor could be written right now, and what stands in the way."""
    settings = get_settings()
    runtime: Any = getattr(request.app.state, "chain_runtime", None)

    deployed = _is_deployed(settings.contract_address)
    rpc_available = bool(runtime is not None and runtime.client.available)
    can_write = bool(runtime is not None and runtime.can_write)

    return ChainStatusView(
        # Every one of these has to hold. A signer with no contract, or a
        # contract with writes disabled, is postgres_only however healthy the
        # node is.
        mode="live" if (can_write and deployed and rpc_available) else "postgres_only",
        contract_address=settings.contract_address,
        chain_id=settings.chain_id,
        write_enabled=settings.chain_write_enabled,
        signer_configured=settings.chain_signer_configured,
        contract_deployed=deployed,
        rpc_available=rpc_available,
    )


@router.get(
    "/status",
    response_model=SystemStatusResponse,
    summary="Queue depth, dead letters, indexer lag, quotas, workers, chain mode",
)
async def system_status(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> SystemStatusResponse:
    """One read of everything an operator needs before presenting."""
    settings = get_settings()

    depths, total = await _outbox_depth(session)
    dead_total, dead_unresolved = await _dead_letters(session)
    scheduler_running, jobs = _jobs()

    return SystemStatusResponse(
        observed_at=now(),
        app_env=settings.app_env,
        chain=_chain(request),
        outbox=depths,
        outbox_total=total,
        dead_letters=dead_total,
        dead_letters_unresolved=dead_unresolved,
        indexer=await _indexer(session, request),
        quotas=await _quotas(session),
        scheduler_enabled=settings.scheduler_enabled,
        scheduler_running=scheduler_running,
        jobs=jobs,
    )
