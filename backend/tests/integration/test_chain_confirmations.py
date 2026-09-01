"""Confirmation depth, reorg demotion, and reverted transactions.

Three questions this file exists to answer, all of which fail silently if got
wrong: is an item ever CONFIRMED too early, does a reorg actually demote it, and
is a mined-but-reverted transaction ever mistaken for an anchor.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.catalog import Item, ItemEvent
from app.db.models.chain import ChainTx
from app.db.models.enums import (
    ChainTxStatus,
    ItemEventType,
    ItemStatus,
    OutboxStatus,
)
from app.db.models.ops import DeadLetter
from app.db.models.outbox import Outbox
from app.workers.jobs import drain_outbox, sweep_confirmations
from tests.fakes.chain_harness import (
    TEST_CONTRACT_ADDRESS,
    ChainHarness,
    build_harness,
    make_category,
    make_weaver,
    seed_item,
)
from tests.fakes.fake_chain import FakeChain

pytestmark = [pytest.mark.integration, pytest.mark.chain]

CONFIRMATIONS = 3


async def anchored_item(
    session: AsyncSession, session_factory: Any, chain: FakeChain | None = None, **overrides: Any
) -> tuple[ChainHarness, Item]:
    """Seed one item, drain it onto the chain, and mine it into a block."""
    harness = build_harness(
        session_factory,
        chain=chain,
        chain_confirmations=CONFIRMATIONS,
        **overrides,
    )
    weaver = await make_weaver(session)
    category = await make_category(session)
    item = await seed_item(session, weaver, category)
    await session.commit()

    assert await drain_outbox(harness.runtime) == 1
    harness.chain.mine()
    return harness, item


async def refresh(session: AsyncSession, item: Item) -> Item:
    await session.refresh(item)
    return item


class TestConfirmationDepth:
    @pytest.mark.parametrize("depth", list(range(CONFIRMATIONS)))
    async def test_an_item_is_never_confirmed_before_the_threshold(
        self, session: AsyncSession, session_factory: Any, depth: int
    ) -> None:
        harness, item = await anchored_item(session, session_factory)

        # `depth` further blocks on top of the one that included the transaction.
        harness.chain.mine(depth)
        await harness.sweep.run()

        await refresh(session, item)
        # A receipt is not a confirmation. Anything else here is a consumer
        # being told their record is on chain before it reliably is.
        assert item.status == ItemStatus.PENDING

    async def test_the_item_is_confirmed_at_exactly_the_threshold(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness, item = await anchored_item(session, session_factory)

        harness.chain.mine(CONFIRMATIONS)
        await harness.sweep.run()

        await refresh(session, item)
        assert item.status == ItemStatus.CONFIRMED

        tx = (await session.execute(select(ChainTx))).scalar_one()
        await session.refresh(tx)
        assert tx.status == ChainTxStatus.CONFIRMED
        assert tx.block_number is not None
        assert tx.block_hash is not None

    async def test_confirmation_writes_an_anchored_event_with_the_block(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness, item = await anchored_item(session, session_factory)
        harness.chain.mine(CONFIRMATIONS)
        await harness.sweep.run()

        events = (
            (
                await session.execute(
                    select(ItemEvent).where(
                        ItemEvent.item_id == item.id,
                        ItemEvent.event_type == ItemEventType.ANCHORED,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["block_number"] is not None
        assert payload["confirmations"] >= CONFIRMATIONS
        assert payload["contract"] == harness.binding.address

    async def test_the_job_is_only_marked_done_once_confirmed(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness, _ = await anchored_item(session, session_factory)
        job = (await session.execute(select(Outbox))).scalar_one()

        await harness.sweep.run()
        await session.refresh(job)
        assert job.status == OutboxStatus.IN_FLIGHT

        harness.chain.mine(CONFIRMATIONS)
        await harness.sweep.run()
        await session.refresh(job)
        assert job.status == OutboxStatus.DONE

    async def test_a_receipt_records_the_mined_block_without_promoting(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness, item = await anchored_item(session, session_factory)

        await harness.sweep.run()

        tx = (await session.execute(select(ChainTx))).scalar_one()
        await session.refresh(tx)
        assert tx.status == ChainTxStatus.MINED
        await refresh(session, item)
        assert item.status == ItemStatus.PENDING


class TestReorg:
    async def test_an_orphaned_block_demotes_the_item_and_requeues_the_job(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness, item = await anchored_item(session, session_factory)
        harness.chain.mine(CONFIRMATIONS)
        await harness.sweep.run()
        await refresh(session, item)
        assert item.status == ItemStatus.CONFIRMED

        tx = (await session.execute(select(ChainTx))).scalar_one()
        await session.refresh(tx)
        mined_at = tx.block_number
        assert mined_at is not None

        # The chain changes its mind. The record was true; now it is not.
        harness.chain.reorg(mined_at)
        await harness.sweep.run()

        await session.refresh(tx)
        assert tx.status == ChainTxStatus.ORPHANED

        await refresh(session, item)
        # Back to the honest state: as far as any verifier is concerned, this
        # hash was never anchored.
        assert item.status == ItemStatus.PENDING

        job = (await session.execute(select(Outbox))).scalar_one()
        await session.refresh(job)
        assert job.status == OutboxStatus.QUEUED
        assert job.attempts == 0

    async def test_a_reorg_records_its_own_event_type(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness, item = await anchored_item(session, session_factory)
        harness.chain.mine(CONFIRMATIONS)
        await harness.sweep.run()
        tx = (await session.execute(select(ChainTx))).scalar_one()
        await session.refresh(tx)

        harness.chain.reorg(int(tx.block_number or 1))
        await harness.sweep.run()

        events = (
            (
                await session.execute(
                    select(ItemEvent).where(
                        ItemEvent.item_id == item.id,
                        ItemEvent.event_type == ItemEventType.REORGED,
                    )
                )
            )
            .scalars()
            .all()
        )
        # A distinct event type, not a flag inside an ANCHORED payload: an item
        # that was anchored and then un-anchored is a different history.
        assert len(events) == 1
        assert events[0].payload["recorded_block_hash"] != events[0].payload.get(
            "observed_block_hash"
        )

    async def test_a_requeued_job_anchors_again_after_the_reorg(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness, item = await anchored_item(session, session_factory)
        harness.chain.mine(CONFIRMATIONS)
        await harness.sweep.run()
        tx = (await session.execute(select(ChainTx))).scalar_one()
        await session.refresh(tx)
        harness.chain.reorg(int(tx.block_number or 1))
        await harness.sweep.run()

        # Un-mining the original rolled the chain's nonce back, so the resend
        # goes out at the next allocated nonce and queues behind a hole. The
        # confirmation job fills that hole; the sweep alone would leave the
        # replacement stuck in the mempool forever.
        assert await drain_outbox(harness.runtime) == 1
        await sweep_confirmations(harness.runtime)

        harness.chain.mine(CONFIRMATIONS + 2)
        await sweep_confirmations(harness.runtime)

        await refresh(session, item)
        assert item.status == ItemStatus.CONFIRMED

    async def test_a_deeply_buried_transaction_is_no_longer_re_checked(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness, _ = await anchored_item(session, session_factory)
        harness.chain.mine(CONFIRMATIONS * 4)
        await harness.sweep.run()

        before = len([call for call in harness.chain.call_log if call == "eth_getBlockByNumber"])
        await harness.sweep.run()
        after = len([call for call in harness.chain.call_log if call == "eth_getBlockByNumber"])

        # Watching every anchor forever would grow the sweep's RPC cost without
        # bound for a vanishing probability.
        assert after == before


class TestReverts:
    async def test_a_reverted_transaction_fails_the_item_and_dead_letters_the_job(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        fake = FakeChain(contract_address=TEST_CONTRACT_ADDRESS, chain_id=31_337)
        harness = build_harness(
            session_factory, chain=fake, chain_confirmations=CONFIRMATIONS
        )
        weaver = await make_weaver(session)
        category = await make_category(session)
        item = await seed_item(session, weaver, category)
        await session.commit()

        await drain_outbox(harness.runtime)
        # The transaction is in the mempool; the chain turns hostile before it
        # is mined, so it lands with status 0.
        fake.force_revert = True
        fake.mine()

        await harness.sweep.run()

        tx = (await session.execute(select(ChainTx))).scalar_one()
        await session.refresh(tx)
        assert tx.status == ChainTxStatus.FAILED

        await refresh(session, item)
        # Mined, gas burned, nothing done. Never an anchor.
        assert item.status == ItemStatus.FAILED

        job = (await session.execute(select(Outbox))).scalar_one()
        await session.refresh(job)
        assert job.status == OutboxStatus.DEAD

        letter = (await session.execute(select(DeadLetter))).scalar_one()
        assert "reverted" in letter.error_chain

    async def test_a_revert_records_an_anchor_failed_event(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        fake = FakeChain(contract_address=TEST_CONTRACT_ADDRESS, chain_id=31_337)
        harness = build_harness(
            session_factory, chain=fake, chain_confirmations=CONFIRMATIONS
        )
        weaver = await make_weaver(session)
        category = await make_category(session)
        item = await seed_item(session, weaver, category)
        await session.commit()

        await drain_outbox(harness.runtime)
        fake.force_revert = True
        fake.mine()
        await harness.sweep.run()

        events = (
            (
                await session.execute(
                    select(ItemEvent).where(
                        ItemEvent.item_id == item.id,
                        ItemEvent.event_type == ItemEventType.ANCHOR_FAILED,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1


class TestReplaceByFeeSweep:
    async def test_a_stuck_transaction_is_bumped_by_the_sweep(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        from app.core.clock import now

        fake = FakeChain(
            contract_address=TEST_CONTRACT_ADDRESS, chain_id=31_337, mining_delay_blocks=99
        )
        harness = build_harness(
            session_factory,
            chain=fake,
            chain_confirmations=CONFIRMATIONS,
            chain_tx_timeout_seconds=1,
        )
        weaver = await make_weaver(session)
        category = await make_category(session)
        await seed_item(session, weaver, category)
        await session.commit()

        await drain_outbox(harness.runtime)
        stuck = (await session.execute(select(ChainTx))).scalar_one()
        stuck.created_at = now().replace(year=now().year - 1)
        await session.commit()

        report = await harness.sweep.run()

        assert report.replaced == 1
        rows = (await session.execute(select(ChainTx))).scalars().all()
        assert len(rows) == 2
        assert len({row.nonce for row in rows}) == 1

    async def test_the_sweep_stops_bumping_after_the_attempt_ceiling(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        from app.core.clock import now

        fake = FakeChain(
            contract_address=TEST_CONTRACT_ADDRESS, chain_id=31_337, mining_delay_blocks=99
        )
        harness = build_harness(
            session_factory,
            chain=fake,
            chain_confirmations=CONFIRMATIONS,
            chain_tx_timeout_seconds=1,
            chain_max_rbf_attempts=2,
        )
        weaver = await make_weaver(session)
        category = await make_category(session)
        await seed_item(session, weaver, category)
        await session.commit()
        await drain_outbox(harness.runtime)

        for _ in range(4):
            for row in (await session.execute(select(ChainTx))).scalars().all():
                row.created_at = now().replace(year=now().year - 1)
            await session.commit()
            await harness.sweep.run()

        rows = (await session.execute(select(ChainTx))).scalars().all()
        # Two attempts then a stop, rather than compounding the fee forever.
        assert len(rows) == 2
