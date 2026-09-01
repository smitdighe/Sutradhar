"""Background jobs: draining the outbox, chasing confirmations, indexing, reconciling.

Every job is wrapped so that a failure inside it cannot take down the scheduler
or the API process. APScheduler will happily keep a broken job scheduled, and an
unhandled exception in one drain must not stop the next one -- the whole point of
a queue is that a bad minute does not become a bad afternoon. Each wrapper logs
with structure and returns; none of them swallows silently, and none of them
re-raises into the scheduler.

**Single instance is a requirement, and it is checked, not assumed.** The outbox
is safe under concurrency -- ``FOR UPDATE SKIP LOCKED`` sees to that -- but nonce
allocation across two processes is only safe because both would serialise on the
same ``chain_nonce`` row, and the confirmation sweep would duplicate
replace-by-fee work. On Render's free tier there is exactly one instance and it
never scales out, which is what makes this hold today. It is asserted with a
Postgres advisory lock rather than trusted: a second process cannot take the
lock, logs loudly, and registers no jobs.

If that lock is ever lost -- its connection drops, or someone runs a second
worker against the same database with the lock disabled -- the failure is not
loud. Two drains would each claim disjoint jobs correctly, and then both would
allocate nonces, both would send, and one of the two transactions at each nonce
would silently replace the other. That is the failure this lock exists to
prevent, and it is why the log line on losing it is an error rather than a note.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from app.chain.batching import assemble_batch, link_batch_transaction
from app.chain.client import ChainClient, build_client
from app.chain.confirmations import ConfirmationSweep, promote_from_indexed_event
from app.chain.contract import ContractBinding, ContractSurfaceError, load_contract
from app.chain.indexer import EventIndexer
from app.chain.nonce import NonceAllocator
from app.chain.outbox import ClaimedJob, OutboxRepository
from app.chain.reconcile import reconcile
from app.chain.writer import ChainWriter, SendOutcome, SendResult, signer_address
from app.config import Settings, get_settings
from app.core.clock import now
from app.core.logging import get_logger
from app.db.models.catalog import Item
from app.db.models.enums import OutboxJobType, OutboxStatus

__all__ = ["LAST_RUN", "ChainRuntime", "build_runtime", "drain_pin_queue", "register_jobs"]

logger = get_logger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]

# Arbitrary but fixed. Any 64-bit constant works; it only has to be the same
# constant in every process that must not run the scheduler concurrently.
SCHEDULER_ADVISORY_LOCK_KEY = 0x5D7A_D147_0000_0001

# When each scheduled job last finished a run, keyed by the name `_guard` was
# given. In-process and deliberately not a table: it describes *this* instance,
# and a shared row would report whichever instance wrote last, which is the
# opposite of what somebody debugging one instance wants to know. Empty until a
# job has run, which is itself the answer when the scheduler is off.
LAST_RUN: dict[str, datetime] = {}


@dataclass
class ChainRuntime:
    """Everything the background jobs need, assembled once at startup.

    Built even when the chain is unreachable and even when writes are disabled.
    A runtime that refuses to exist without a working RPC endpoint would make the
    chain a prerequisite for serving traffic, which is the thing this design is
    most careful not to do.
    """

    settings: Settings
    session_factory: SessionFactory
    client: ChainClient
    binding: ContractBinding | None
    outbox: OutboxRepository
    allocator: NonceAllocator | None
    writer: ChainWriter | None
    sweep: ConfirmationSweep | None
    indexer: EventIndexer | None
    signer: str | None
    lock_connection: AsyncConnection | None = None

    @property
    def can_write(self) -> bool:
        """Whether a transaction could actually be sent right now."""
        return (
            self.writer is not None
            and self.settings.chain_write_enabled
            and self.settings.chain_signer_configured
        )


def build_runtime(
    session_factory: SessionFactory,
    settings: Settings | None = None,
    client: ChainClient | None = None,
) -> ChainRuntime:
    """Assemble the runtime. Degrades component by component, never raises."""
    resolved = settings or get_settings()
    chain_client = client or build_client(session_factory, resolved)
    outbox = OutboxRepository(session_factory, resolved)

    binding: ContractBinding | None = None
    try:
        binding = load_contract()
    except (ContractSurfaceError, OSError, ValueError) as exc:
        logger.error(
            "chain.contract.unavailable",
            error=str(exc),
            consequence="the outbox fills but nothing can be encoded or sent; "
            "items stay PENDING and the API is unaffected",
        )

    signer = signer_address(resolved)
    allocator = NonceAllocator(session_factory, signer) if signer else None
    writer = (
        ChainWriter(chain_client, binding, allocator, session_factory, resolved)
        if binding is not None and allocator is not None
        else None
    )
    sweep = (
        ConfirmationSweep(
            chain_client, binding, outbox, session_factory, writer=writer, settings=resolved
        )
        if binding is not None
        else None
    )
    indexer = (
        EventIndexer(chain_client, binding, session_factory, resolved)
        if binding is not None
        else None
    )

    if signer is None:
        logger.warning(
            "chain.signer.absent",
            consequence="CHAIN_SIGNER_PRIVATE_KEY is empty; the outbox will queue but never send",
        )

    return ChainRuntime(
        settings=resolved,
        session_factory=session_factory,
        client=chain_client,
        binding=binding,
        outbox=outbox,
        allocator=allocator,
        writer=writer,
        sweep=sweep,
        indexer=indexer,
        signer=signer,
    )


# ------------------------------------------------------------------ the jobs


# The job types this drain understands. Anything else in the outbox belongs to
# another drain and must not be claimed here -- a claimed job it cannot run
# would be killed as unsupported, or released forever when chain writes are off.
CHAIN_JOB_TYPES = (
    OutboxJobType.ANCHOR_ITEM,
    OutboxJobType.ANCHOR_ATTESTATION,
    OutboxJobType.ANCHOR_BATCH,
)


async def drain_outbox(runtime: ChainRuntime) -> int:
    """Claim due anchoring jobs and try to send them. Returns how many were handled."""
    await runtime.outbox.reclaim_stale(CHAIN_JOB_TYPES)

    if runtime.settings.batching_enabled:
        await assemble_batch(runtime.session_factory, runtime.settings)

    jobs = await runtime.outbox.claim(job_types=CHAIN_JOB_TYPES)
    if not jobs:
        return 0

    for job in jobs:
        await _handle_job(runtime, job)
    return len(jobs)


async def _handle_job(runtime: ChainRuntime, job: ClaimedJob) -> None:
    """Run one job, translating every outcome into an outbox state."""
    if runtime.writer is None or runtime.binding is None:
        # Not a failure of the job. Requeued without counting an attempt, so an
        # outage does not dead-letter a queue full of perfectly good work.
        await runtime.outbox.release(
            job.id,
            "chain writer unavailable (no signer, no contract artifact, or writes disabled)",
        )
        return

    if not runtime.settings.chain_write_enabled:
        await runtime.outbox.release(job.id, "CHAIN_WRITE_ENABLED=false")
        return

    try:
        if job.job_type == OutboxJobType.ANCHOR_ITEM:
            result = await _anchor_item(runtime, job)
        elif job.job_type == OutboxJobType.ANCHOR_ATTESTATION:
            result = await _anchor_attestation(runtime, job)
        elif job.job_type == OutboxJobType.ANCHOR_BATCH:
            result = await _anchor_batch(runtime, job)
        else:
            # Defence in depth. `claim(job_types=CHAIN_JOB_TYPES)` means a
            # non-anchoring job should never arrive here at all, so reaching
            # this branch means the filter and this dispatch have drifted apart.
            # Parked rather than retried: no number of retries teaches this
            # worker a job type it does not implement.
            await runtime.outbox.kill(
                job.id, f"unsupported job type {job.job_type} for the chain worker"
            )
            return
    except Exception as exc:  # noqa: BLE001 - one bad job must not stop the drain
        logger.exception(
            "outbox.job_failed", job_id=str(job.id), job_type=str(job.job_type)
        )
        await runtime.outbox.fail(job.id, f"{type(exc).__name__}: {exc}")
        return

    await _apply_result(runtime, job, result)


async def _apply_result(runtime: ChainRuntime, job: ClaimedJob, result: SendResult) -> None:
    """Map a send outcome onto the job's next state."""
    if result.outcome == SendOutcome.SENT:
        # In flight, not anchored. The job stays IN_FLIGHT until the
        # confirmation sweep promotes it, so nothing claims success early.
        logger.info(
            "outbox.job_sent", job_id=str(job.id), tx_hash=result.tx_hash, nonce=result.nonce
        )
        return

    if result.outcome == SendOutcome.ALREADY_ANCHORED:
        promoted = await _try_promote_from_event(runtime, job)
        if promoted:
            await runtime.outbox.complete(job.id, detail="confirmed from the indexed anchor event")
        else:
            await runtime.outbox.release(
                job.id,
                "chain reports AlreadyAnchored; waiting for the indexer to surface the event",
            )
        return

    if result.outcome == SendOutcome.REFUSED:
        await runtime.outbox.release(job.id, result.reason)
        return

    # WOULD_REVERT and REJECTED are failures of the job itself and count against the
    # retry budget, which is what eventually produces a dead letter.
    await runtime.outbox.fail(job.id, f"{result.outcome}: {result.reason}")


async def _anchor_item(runtime: ChainRuntime, job: ClaimedJob) -> SendResult:
    """Send one ``anchorItem``, unless the chain already holds this anchor."""
    item_hash = str(job.payload["item_hash"])
    issuer_hash = str(job.payload.get("issuer_hash") or "")

    if not issuer_hash:
        # Enqueued before issuer_hash was carried on the job. Recovered from the
        # REGISTERED event rather than failed, since the value is recorded there
        # verbatim and re-deriving it from the user would need a salt that may
        # since have been deleted.
        issuer_hash = await _issuer_hash_from_event(runtime, str(job.payload["item_id"]))
        if not issuer_hash:
            return SendResult(
                outcome=SendOutcome.WOULD_REVERT,
                reason="no issuer_hash on the job and none recoverable from the REGISTERED event",
            )

    if await _try_promote_from_event(runtime, job):
        return SendResult(
            outcome=SendOutcome.ALREADY_ANCHORED,
            reason="an indexed ItemAnchored event already covers this hash",
        )

    assert runtime.writer is not None
    return await runtime.writer.anchor_item(job.id, item_hash, issuer_hash)


async def _anchor_attestation(runtime: ChainRuntime, job: ClaimedJob) -> SendResult:
    """Anchor one attestation's statement hash.

    No pre-send lookup in the event mirror, unlike an item anchor. An item can
    already be on chain from a previous deployment or a reorg replay, so it is
    worth a check before paying for a transaction; a statement hash commits to
    the attestor, the item and the instant, so it cannot pre-exist unless this
    system put it there -- and if it did, the contract's own AlreadyAnchored
    revert resolves it for the price of one eth_call in preflight.
    """
    assert runtime.writer is not None
    return await runtime.writer.anchor_attestation(
        job.id,
        str(job.payload["statement_hash"]),
        str(job.payload["issuer_hash"]),
    )


async def _anchor_batch(runtime: ChainRuntime, job: ClaimedJob) -> SendResult:
    """Send one ``anchorBatch`` and link the transaction to the batch row."""
    assert runtime.writer is not None
    root = str(job.payload["root"])
    leaf_count = int(str(job.payload["leaf_count"]))

    result = await runtime.writer.anchor_batch(job.id, root, leaf_count)
    if result.chain_tx_id is not None:
        await link_batch_transaction(runtime.session_factory, root, result.chain_tx_id)
    return result


async def _try_promote_from_event(runtime: ChainRuntime, job: ClaimedJob) -> bool:
    """Confirm an item straight from an indexed event, when one is deep enough.

    Saves a transaction when the anchor is already on chain, and is the path out
    of an ``AlreadyAnchored`` revert. Returns False whenever the evidence is not
    there, in which case the normal send path runs.
    """
    if job.job_type != OutboxJobType.ANCHOR_ITEM or runtime.binding is None:
        return False

    raw_item_id = job.payload.get("item_id")
    if raw_item_id is None:
        return False

    try:
        head = await runtime.client.block_number()
    except Exception as exc:  # noqa: BLE001 - a missing head just means "not yet"
        logger.debug("chain.promote_from_event.no_head", error=str(exc))
        return False

    async with runtime.session_factory() as session:
        item = await session.get(Item, uuid.UUID(str(raw_item_id)))
        if item is None:
            await session.commit()
            return False
        promoted = await promote_from_indexed_event(
            session,
            item,
            head_block=head,
            required_depth=runtime.settings.chain_confirmations,
            contract_address=runtime.binding.address,
            chain_id=runtime.settings.chain_id,
        )
        await session.commit()
    return promoted


async def _issuer_hash_from_event(runtime: ChainRuntime, item_id: str) -> str:
    """Recover the anchorable issuer digest from the item's REGISTERED event."""
    from app.db.models.catalog import ItemEvent
    from app.db.models.enums import ItemEventType

    async with runtime.session_factory() as session:
        event = (
            await session.execute(
                select(ItemEvent)
                .where(
                    ItemEvent.item_id == uuid.UUID(item_id),
                    ItemEvent.event_type == ItemEventType.REGISTERED,
                )
                .order_by(ItemEvent.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        await session.commit()

    if event is None:
        return ""
    preimage = event.payload.get("preimage")
    if not isinstance(preimage, dict):
        return ""
    return str(preimage.get("registered_by_hash") or "")


async def sweep_confirmations(runtime: ChainRuntime) -> None:
    """Receipts, reorg checks, promotions, fee bumps, and nonce gap filling."""
    if runtime.sweep is None:
        return
    await runtime.sweep.run()
    await _fill_nonce_gaps(runtime)


async def _fill_nonce_gaps(runtime: ChainRuntime) -> None:
    """Close any nonce that is blocking the queue."""
    if runtime.allocator is None or runtime.writer is None or not runtime.can_write:
        return
    gaps = await runtime.allocator.find_gaps(runtime.client)
    for gap in gaps:
        result = await runtime.writer.fill_gap(gap.nonce)
        if not result.succeeded:
            logger.warning(
                "chain.nonce.gap_fill_failed",
                nonce=gap.nonce,
                outcome=str(result.outcome),
                reason=result.reason,
            )
            # Stop at the first failure. Nonces are consumed in order, so a hole
            # that cannot be filled blocks everything above it anyway.
            break


# The media drain's lane. Kept disjoint from CHAIN_JOB_TYPES on purpose: a job
# claimed by the wrong drain is a job that gets killed as unsupported, or
# released in a loop because chain writes happen to be off.
MEDIA_JOB_TYPES = (OutboxJobType.PIN_MEDIA,)


async def drain_pin_queue(runtime: ChainRuntime) -> int:
    """Retry pins that have not succeeded yet. Returns how many were handled.

    Deliberately independent of the chain drain. Pinning has nothing to do with
    ``CHAIN_WRITE_ENABLED``, a signer, or a contract artifact, and a media queue
    that stalled because a testnet was unreachable would be a self-inflicted
    outage. The two share a table and a retry mechanism; they share no
    preconditions.

    A file whose pin never succeeds is not lost. It still resolves from the
    mirror and the database blob -- the record simply is not on IPFS, which the
    ``PIN_FAILED`` status says plainly rather than pretending otherwise.
    """
    from app.media.pinata import PinataClient

    await runtime.outbox.reclaim_stale(MEDIA_JOB_TYPES)
    jobs = await runtime.outbox.claim(job_types=MEDIA_JOB_TYPES)
    if not jobs:
        return 0

    client = PinataClient(runtime.settings)
    for job in jobs:
        await _retry_pin(runtime, job, client)
    return len(jobs)


async def _retry_pin(runtime: ChainRuntime, job: ClaimedJob, client: Any) -> None:
    """One pin retry, translating the outcome into an outbox state."""

    from app.db.models.enums import PinStatus
    from app.db.models.media import Media
    from app.media.mirror import MirrorStore
    from app.media.pinata import PinataError
    from app.media.service import pinata_quota

    if not client.enabled:
        # Not a failure of this job. Released without spending an attempt, so a
        # deployment with no JWT does not dead-letter its whole media queue.
        await runtime.outbox.release(job.id, "pinning is disabled (PINATA_JWT unset)")
        return

    media_id = uuid.UUID(str(job.payload["media_id"]))
    async with runtime.session_factory() as session:
        media = await session.get(Media, media_id)
        if media is None:
            await session.commit()
            await runtime.outbox.complete(job.id, detail="media row no longer exists")
            return
        if media.pin_status is PinStatus.PINNED:
            await session.commit()
            await runtime.outbox.complete(job.id, detail="already pinned")
            return

        data = _bytes_for(media, MirrorStore(runtime.settings))
        await session.commit()

    if data is None:
        # Both local tiers are gone, so there is nothing left to upload. Parked
        # immediately: no number of retries will conjure the bytes back.
        await runtime.outbox.kill(
            job.id,
            f"no local copy of {media_id} remains to pin (mirror wiped, no blob)",
        )
        await _mark_pin_failed(runtime, media_id, "no local copy remained to pin")
        return

    budget = pinata_quota(runtime.session_factory, runtime.settings)
    if await budget.would_exceed(len(data)):
        await runtime.outbox.release(job.id, "IPFS storage budget exhausted")
        return

    try:
        result = await client.pin(data, str(job.payload["sha256"]), "application/octet-stream")
    except PinataError as exc:
        outcome = await runtime.outbox.fail(job.id, f"pin failed: {exc}")
        if outcome is OutboxStatus.DEAD:
            await _mark_pin_failed(runtime, media_id, str(exc))
        return

    async with runtime.session_factory() as session:
        media = await session.get(Media, media_id)
        if media is not None:
            media.cid = result.cid
            media.pin_status = PinStatus.PINNED
        await session.commit()

    await budget.consume(len(data))
    await runtime.outbox.complete(job.id, detail=f"pinned as {result.cid}")
    logger.info("media.pin.retried_ok", media_id=str(media_id), cid=result.cid)


def _bytes_for(media: Any, store: Any) -> bytes | None:
    """Local bytes for a media row, mirror first then blob."""
    data = store.read(media.mirror_path)
    if data is not None:
        return bytes(data)
    return bytes(media.blob) if media.blob is not None else None


async def _mark_pin_failed(runtime: ChainRuntime, media_id: uuid.UUID, reason: str) -> None:
    """Record that IPFS is out of reach for this file, without losing the file."""
    from app.db.models.enums import PinStatus
    from app.db.models.media import Media

    async with runtime.session_factory() as session:
        media = await session.get(Media, media_id)
        if media is not None:
            media.pin_status = PinStatus.PIN_FAILED
        await session.commit()

    logger.error(
        "media.pin.failed",
        media_id=str(media_id),
        reason=reason,
        consequence="the file still resolves from the mirror and the database blob; "
        "it is simply not on IPFS",
    )


async def run_indexer(runtime: ChainRuntime) -> None:
    if runtime.indexer is None:
        return
    await runtime.indexer.run()


async def run_reconcile(runtime: ChainRuntime) -> None:
    report = await reconcile(runtime.session_factory, runtime.client, runtime.settings)
    if not report.clean:
        logger.warning(
            "chain.reconcile.drift_present",
            **report.as_log_fields(),
            action="reported only; nothing is auto-corrected, "
            "because a silent correction hides the cause",
        )


# ------------------------------------------------------------- registration


def _guard(name: str, job: Any, runtime: ChainRuntime) -> Any:
    """Wrap a job so a failure is logged and contained, and its run recorded.

    Deliberately catches broadly. A scheduled job that raises takes its next run
    with it in some configurations, and there is no failure inside these jobs
    worth stopping the API for.

    The run is stamped in :data:`LAST_RUN` **after** the body, whether it
    succeeded or not. APScheduler reports the next run, not the last one, and
    "when did the outbox drain last" is the question an operator is actually
    asking five minutes before a demo. Recording it only on success would show a
    job that has been failing every five seconds as one that has never run,
    which points at the scheduler instead of at the job.
    """

    async def wrapped() -> None:
        try:
            await job(runtime)
        except Exception as exc:  # noqa: BLE001 - a job must never kill the scheduler
            logger.exception("worker.job_error", job=name, error=f"{type(exc).__name__}: {exc}")
        finally:
            LAST_RUN[name] = now()

    wrapped.__name__ = f"guarded_{name}"
    return wrapped


async def acquire_scheduler_lock(engine: AsyncEngine) -> AsyncConnection | None:
    """Take the process-wide advisory lock, or return ``None`` if held elsewhere.

    A session-scoped advisory lock on a dedicated connection: it lives exactly as
    long as this process holds that connection, and Postgres releases it if the
    process dies, so a crashed instance does not lock out its replacement.
    """
    connection = await engine.connect()
    held = (
        await connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": SCHEDULER_ADVISORY_LOCK_KEY}
        )
    ).scalar_one()
    if not held:
        await connection.close()
        return None
    return connection


async def start_chain_workers(
    scheduler: Any,
    session_factory: SessionFactory,
    engine: AsyncEngine,
    settings: Settings | None = None,
    client: ChainClient | None = None,
) -> ChainRuntime | None:
    """Assert single-instance, probe the chain, reconcile the nonce, register jobs."""
    resolved = settings or get_settings()
    if not resolved.scheduler_enabled:
        logger.info(
            "worker.disabled",
            reason="SCHEDULER_ENABLED=false",
            effect="the outbox fills and nothing drains it; "
            "required so tests and a second process do not double-run jobs",
        )
        return None

    lock_connection = await acquire_scheduler_lock(engine)
    if lock_connection is None:
        logger.error(
            "worker.single_instance_violated",
            lock_key=hex(SCHEDULER_ADVISORY_LOCK_KEY),
            reason="another process already holds the scheduler advisory lock",
            consequence="this process registers no background jobs; "
            "two schedulers would allocate nonces independently and each anchor "
            "would silently replace the other at the same nonce",
        )
        return None

    logger.info(
        "worker.single_instance_asserted",
        lock_key=hex(SCHEDULER_ADVISORY_LOCK_KEY),
        why="the outbox is concurrency-safe but nonce allocation and replace-by-fee "
        "are only correct with one scheduler; Render's free tier never scales past "
        "one instance, and this lock checks that rather than assuming it",
        if_violated="two schedulers would send competing transactions at the same nonce, "
        "and one anchor of every pair would be lost with no error raised",
    )

    runtime = build_runtime(session_factory, resolved, client=client)
    runtime.lock_connection = lock_connection

    reachable = await runtime.client.connect()
    if reachable and runtime.allocator is not None:
        # Only worth doing against a live node: reconciling against a dead one
        # would just raise, and the drain reconciles again the first time it
        # successfully talks to the chain anyway.
        await runtime.allocator.reconcile(runtime.client)

    register_jobs(scheduler, runtime)
    return runtime


def register_jobs(scheduler: Any, runtime: ChainRuntime) -> None:
    """Attach the four recurring jobs to the scheduler."""
    settings = runtime.settings

    scheduler.add_job(
        _guard("outbox_drain", drain_outbox, runtime),
        "interval",
        seconds=settings.outbox_poll_seconds,
        id="chain_outbox_drain",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _guard("confirmation_sweep", sweep_confirmations, runtime),
        "interval",
        seconds=settings.confirmation_poll_seconds,
        id="chain_confirmation_sweep",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _guard("pin_retry", drain_pin_queue, runtime),
        "interval",
        seconds=settings.pin_retry_poll_seconds,
        id="media_pin_retry",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _guard("indexer", run_indexer, runtime),
        "interval",
        seconds=settings.indexer_poll_seconds,
        id="chain_indexer",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _guard("reconcile", run_reconcile, runtime),
        "cron",
        **_parse_cron(settings.reconcile_cron),
        id="chain_reconcile",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    logger.info(
        "worker.jobs_registered",
        outbox_poll_seconds=settings.outbox_poll_seconds,
        confirmation_poll_seconds=settings.confirmation_poll_seconds,
        indexer_poll_seconds=settings.indexer_poll_seconds,
        pin_retry_poll_seconds=settings.pin_retry_poll_seconds,
        reconcile_cron=settings.reconcile_cron,
        writes_enabled=settings.chain_write_enabled,
        batching_enabled=settings.batching_enabled,
    )


def _parse_cron(expression: str) -> dict[str, str]:
    """Split a five-field cron string into APScheduler keyword arguments."""
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError(
            f"RECONCILE_CRON must have five fields (m h dom mon dow), got {expression!r}"
        )
    minute, hour, day, month, day_of_week = fields
    return {
        "minute": minute,
        "hour": hour,
        "day": day,
        "month": month,
        "day_of_week": day_of_week,
    }


async def shutdown_chain_workers(runtime: ChainRuntime | None) -> None:
    """Flush metered usage and release the single-instance lock."""
    if runtime is None:
        return
    await runtime.client.flush_quota()
    if runtime.lock_connection is not None:
        await runtime.lock_connection.close()
        runtime.lock_connection = None
