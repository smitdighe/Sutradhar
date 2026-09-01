"""Response shapes for the operator surface.

Hand-written like every other serialiser in this project. These carry counts and
configuration, never rows, so nothing here can grow a column by accident.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.core.clock import UtcDatetime

__all__ = [
    "ChainStatusView",
    "IndexerStatusView",
    "OutboxDepthView",
    "QuotaView",
    "SchedulerJobView",
    "SystemStatusResponse",
]


class OutboxDepthView(BaseModel):
    """How many jobs of one type sit in one state."""

    model_config = ConfigDict(extra="forbid")

    job_type: str
    status: str
    count: int


class QuotaView(BaseModel):
    """Consumption against one metered budget."""

    model_config = ConfigDict(extra="forbid")

    name: str
    used: str
    budget: str
    # Percentage of budget spent, rounded to one place. Present because the
    # question being asked at a glance is "am I close", not "what is the ratio".
    used_percent: float
    period_start: UtcDatetime


class IndexerStatusView(BaseModel):
    """How far behind the event mirror is.

    ``lag_blocks`` is ``None`` -- not zero -- when the head could not be read.
    Zero means caught up, and reporting an unreachable node as caught up is the
    single most misleading thing this endpoint could do.
    """

    model_config = ConfigDict(extra="forbid")

    checkpoint_block: int
    head_block: int | None = None
    lag_blocks: int | None = None
    detail: str | None = None


class SchedulerJobView(BaseModel):
    """One registered background job."""

    model_config = ConfigDict(extra="forbid")

    id: str
    next_run_at: UtcDatetime | None = None
    last_run_at: UtcDatetime | None = None


class ChainStatusView(BaseModel):
    """What the anchoring path can actually do right now."""

    model_config = ConfigDict(extra="forbid")

    # "live" only when a transaction could be sent against a deployed contract.
    # Anything else is "postgres_only", which is the honest name for the state
    # this system runs in today: records are real, anchors are not yet.
    mode: str
    contract_address: str
    chain_id: int
    write_enabled: bool
    signer_configured: bool
    contract_deployed: bool
    rpc_available: bool


class SystemStatusResponse(BaseModel):
    """The one screen worth checking before presenting."""

    model_config = ConfigDict(extra="forbid")

    observed_at: UtcDatetime
    app_env: str
    chain: ChainStatusView
    outbox: list[OutboxDepthView] = Field(default_factory=list)
    outbox_total: int
    dead_letters: int
    dead_letters_unresolved: int
    indexer: IndexerStatusView
    quotas: list[QuotaView] = Field(default_factory=list)
    scheduler_enabled: bool
    scheduler_running: bool
    jobs: list[SchedulerJobView] = Field(default_factory=list)
