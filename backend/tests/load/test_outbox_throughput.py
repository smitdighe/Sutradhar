"""A thousand jobs through the queue, and not one nonce missing at the end.

Throughput is the smaller half of this. The assertion that matters is the nonce
sequence: every transaction this service sends is numbered, the numbers must be
contiguous, and a gap means one send was allocated a nonce and then lost. On a
real chain a gap is worse than a failure -- every later transaction sits unmined
behind the missing one, so one dropped job stalls the whole queue silently and
every symptom points at the node.

Run against the offline EVM from Phase 7. A thousand real transactions on Amoy
would cost real money to learn something an in-memory chain answers exactly.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chain.outbox import enqueue_job
from app.db.models.chain import ChainTx, MerkleBatch
from app.db.models.enums import OutboxJobType, OutboxStatus
from app.db.models.outbox import Outbox
from tests.fakes.chain_harness import build_harness, make_category, make_weaver, seed_item
from tests.load.conftest import announce

pytestmark = pytest.mark.load

TOTAL_JOBS = 1_000
# The four job types, in the proportions the system actually produces: an item
# and its media, attested some of the time, batched rarely.
ITEM_JOBS = 500
ATTESTATION_JOBS = 250
BATCH_JOBS = 50
PIN_JOBS = 200

# Distinct, non-degenerate bases for the synthetic hashes.
_ATTESTATION_BASE = 0x1000_0000
_ISSUER_BASE = 0x2000_0000
_BATCH_BASE = 0x3000_0000


class TestOutboxThroughput:
    async def test_a_thousand_jobs_drain_with_no_nonce_gaps(
        self, load_sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        harness = build_harness(load_sessions, outbox_batch_size=100)

        async with load_sessions() as session:
            weaver = await make_weaver(session)
            category = await make_category(session, slug=f"load-{uuid.uuid4().hex[:6]}")

            for _ in range(ITEM_JOBS):
                await seed_item(session, weaver, category, quantity="12.0000")

            # Hashes are offset well away from zero. The all-zero bytes32 is
            # not a hash anybody can produce and the registry refuses it, so a
            # synthetic job numbered from 0 dead-letters on its first attempt
            # and looks like a throughput failure.
            for index in range(ATTESTATION_JOBS):
                await enqueue_job(
                    session,
                    job_type=OutboxJobType.ANCHOR_ATTESTATION,
                    payload={
                        "attestation_id": str(uuid.uuid4()),
                        "item_id": str(uuid.uuid4()),
                        "statement_hash": f"0x{_ATTESTATION_BASE + index:064x}",
                        "issuer_hash": f"0x{_ISSUER_BASE + index:064x}",
                    },
                    dedupe_key=f"attestation-{index}",
                )

            # Real batch rows, because the handler links the transaction back to
            # one after the send. A root with no row behind it is a job the
            # drain cannot finish, which would show up as missing throughput
            # rather than as the fixture gap it is.
            for index in range(BATCH_JOBS):
                root = f"0x{_BATCH_BASE + index:064x}"
                session.add(MerkleBatch(root=root, leaf_count=8))
                await enqueue_job(
                    session,
                    job_type=OutboxJobType.ANCHOR_BATCH,
                    payload={
                        "batch_id": str(uuid.uuid4()),
                        "root": root,
                        "leaf_count": 8,
                    },
                    dedupe_key=f"batch-{index}",
                )

            for index in range(PIN_JOBS):
                await enqueue_job(
                    session,
                    job_type=OutboxJobType.PIN_MEDIA,
                    payload={"media_id": str(uuid.uuid4()), "sha256": f"{index:064x}"},
                    dedupe_key=f"pin:{index:064x}",
                )

            await session.commit()

        queued = await _count(load_sessions, Outbox)
        assert queued == TOTAL_JOBS, f"{queued} jobs enqueued, expected {TOTAL_JOBS}"

        # Drain only what the chain drain understands. The pin jobs belong to a
        # different drain and must still be sitting there afterwards -- a chain
        # worker that claimed one would kill it as unsupported.
        from app.workers.jobs import CHAIN_JOB_TYPES, drain_outbox

        chain_jobs = ITEM_JOBS + ATTESTATION_JOBS + BATCH_JOBS
        started = time.perf_counter()
        handled = 0
        while handled < chain_jobs:
            drained = await drain_outbox(harness.runtime)
            if drained == 0:
                break
            handled += drained
        elapsed = time.perf_counter() - started

        announce(
            [
                "\nOUTBOX THROUGHPUT",
                f"  jobs enqueued   {TOTAL_JOBS}",
                f"  chain jobs      {chain_jobs}",
                f"  drained         {handled}",
                f"  elapsed         {elapsed:8.2f} s",
                f"  rate            {handled / max(elapsed, 1e-6):8.1f} jobs/s",
            ]
        )

        assert handled == chain_jobs, (
            f"the drain handled {handled} of {chain_jobs} chain jobs before "
            "stalling; jobs left in the queue with no error are jobs lost"
        )

        # -- the assertion this test exists for --------------------------------
        async with load_sessions() as session:
            nonces = list(
                (
                    await session.execute(select(ChainTx.nonce).order_by(ChainTx.nonce))
                )
                .scalars()
                .all()
            )

        assert len(nonces) == chain_jobs, (
            f"{len(nonces)} transactions for {chain_jobs} jobs"
        )
        assert len(set(nonces)) == len(nonces), (
            "a nonce was allocated twice, which on a real chain means one "
            "anchor silently replaced another"
        )
        assert nonces == list(range(nonces[0], nonces[0] + len(nonces))), (
            f"the nonce sequence has a gap: it runs {nonces[0]}..{nonces[-1]} "
            f"with {len(nonces)} entries. Every transaction after a gap sits "
            "unmined behind the missing one."
        )

        # A clean drain against a healthy chain parks nothing. Asserted here
        # rather than in its own test because it is a fact about *this* drain,
        # and a separate test would be reading state left behind by this one.
        from app.db.models.ops import DeadLetter

        parked = await _count(load_sessions, DeadLetter)
        assert parked == 0, f"{parked} jobs were dead-lettered during a clean drain"

        # The pin jobs were never touched by the chain drain.
        async with load_sessions() as session:
            untouched = (
                await session.execute(
                    select(func.count())
                    .select_from(Outbox)
                    .where(
                        Outbox.job_type == OutboxJobType.PIN_MEDIA,
                        Outbox.status == OutboxStatus.QUEUED,
                    )
                )
            ).scalar_one()
        assert untouched == PIN_JOBS, (
            f"{PIN_JOBS - untouched} media pinning jobs were claimed by the "
            f"chain drain, which understands only {[str(t) for t in CHAIN_JOB_TYPES]}"
        )



async def _count(
    sessions: async_sessionmaker[AsyncSession], model: Any
) -> int:
    async with sessions() as session:
        return int(
            (await session.execute(select(func.count()).select_from(model))).scalar_one()
        )
