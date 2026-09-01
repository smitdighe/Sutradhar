"""Nonce allocation, startup reconciliation, and gap recovery.

The concurrency test here is the reason nonces come from Postgres rather than
from ``eth_getTransactionCount``. It cannot be written against a mock: twenty
simultaneous allocations either serialise on a real row lock or they do not.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chain.nonce import NonceAllocator
from app.core.clock import now
from app.db.models.chain import ChainNonce, ChainTx
from app.db.models.enums import ChainTxStatus
from tests.fakes.chain_harness import build_harness
from tests.fakes.fake_chain import FakeChain

pytestmark = [pytest.mark.integration, pytest.mark.chain]


class TestAllocation:
    async def test_allocations_are_sequential(self, session_factory: Any) -> None:
        allocator = NonceAllocator(session_factory, "0x" + "01" * 20)

        allocated = [await allocator.allocate() for _ in range(5)]

        assert allocated == [0, 1, 2, 3, 4]

    async def test_twenty_concurrent_allocations_have_no_gaps_or_duplicates(
        self, session_factory: Any
    ) -> None:
        allocator = NonceAllocator(session_factory, "0x" + "02" * 20)

        allocated = await asyncio.gather(*(allocator.allocate() for _ in range(20)))

        # Distinct, contiguous, starting at zero. A duplicate here means one
        # anchor silently replaces another on chain and nothing raises.
        assert sorted(allocated) == list(range(20))
        assert len(set(allocated)) == 20
        assert await allocator.current() == 20

    async def test_addresses_are_tracked_independently(self, session_factory: Any) -> None:
        one = NonceAllocator(session_factory, "0x" + "03" * 20)
        two = NonceAllocator(session_factory, "0x" + "04" * 20)

        assert await one.allocate() == 0
        assert await two.allocate() == 0
        assert await one.allocate() == 1


class TestRewind:
    async def test_rewind_returns_the_last_unused_nonce(self, session_factory: Any) -> None:
        allocator = NonceAllocator(session_factory, "0x" + "05" * 20)
        nonce = await allocator.allocate()

        assert await allocator.rewind(nonce) is True
        assert await allocator.current() == nonce

    async def test_rewind_is_declined_once_something_else_allocated(
        self, session_factory: Any
    ) -> None:
        allocator = NonceAllocator(session_factory, "0x" + "06" * 20)
        first = await allocator.allocate()
        await allocator.allocate()

        # Handing out a nonce twice is worse than leaving a hole for the gap
        # filler, so the rewind refuses.
        assert await allocator.rewind(first) is False
        assert await allocator.current() == 2


class TestStartupReconciliation:
    async def test_an_unknown_address_adopts_the_chain_count(
        self, session_factory: Any, session: AsyncSession
    ) -> None:
        harness = build_harness(session_factory)
        harness.chain.mined_nonces[harness.writer.address.lower()] = 7

        result = await harness.allocator.reconcile(harness.client)

        assert result.source == "created"
        assert result.adopted == 7
        assert await harness.allocator.current() == 7

    async def test_chain_ahead_wins_and_is_logged_loudly(
        self, session_factory: Any, session: AsyncSession
    ) -> None:
        harness = build_harness(session_factory)
        await harness.allocator.allocate()  # db next_nonce = 1
        harness.chain.mined_nonces[harness.writer.address.lower()] = 9

        result = await harness.allocator.reconcile(harness.client)

        # Transactions went out that this system did not record. Keeping the
        # lower value would sign every future transaction at a used nonce.
        assert result.source == "chain"
        assert result.adopted == 9
        assert await harness.allocator.current() == 9

    async def test_db_ahead_wins_because_those_are_in_flight(
        self, session_factory: Any
    ) -> None:
        harness = build_harness(session_factory)
        for _ in range(4):
            await harness.allocator.allocate()

        result = await harness.allocator.reconcile(harness.client)

        assert result.source == "db"
        assert result.adopted == 4
        assert await harness.allocator.current() == 4


class TestGapDetection:
    async def test_a_nonce_allocated_but_never_sent_is_reported_as_a_gap(
        self, session_factory: Any
    ) -> None:
        harness = build_harness(session_factory)
        # Allocated, then the process died before anything was signed.
        await harness.allocator.allocate()

        gaps = await harness.allocator.find_gaps(harness.client)

        assert [gap.nonce for gap in gaps] == [0]
        assert "no transaction was ever recorded" in gaps[0].reason

    async def test_no_gap_when_the_node_knows_the_transaction(
        self, session_factory: Any
    ) -> None:
        harness = build_harness(session_factory)
        item_hash = "0x" + "77" * 32
        result = await harness.writer.anchor_item(None, item_hash, "0x" + "88" * 32)
        assert result.succeeded

        assert await harness.allocator.find_gaps(harness.client) == []

    async def test_a_recorded_send_the_node_never_saw_is_a_gap_after_the_timeout(
        self, session_factory: Any, session: AsyncSession
    ) -> None:
        harness = build_harness(session_factory, chain_tx_timeout_seconds=1)
        nonce = await harness.allocator.allocate()

        # A row exists but the node has no such transaction: the classic
        # "died after writing the row, before broadcasting" shape.
        session.add(
            ChainTx(
                outbox_id=None,
                tx_hash="0x" + "99" * 32,
                nonce=nonce,
                status=ChainTxStatus.SENT,
                created_at=now().replace(year=now().year - 1),
            )
        )
        await session.commit()

        gaps = await harness.allocator.find_gaps(harness.client)

        assert [gap.nonce for gap in gaps] == [nonce]
        assert "is known to the node" in gaps[0].reason

    async def test_filling_a_gap_unblocks_the_queue(self, session_factory: Any) -> None:
        chain = FakeChain(contract_address="0x" + "5d" * 20, chain_id=31_337)
        harness = build_harness(session_factory, chain=chain)

        stranded = await harness.allocator.allocate()
        # Everything after the hole queues behind it, unmined, forever.
        later = await harness.writer.anchor_item(
            None, "0x" + "aa" * 32, "0x" + "bb" * 32
        )
        assert later.succeeded
        chain.mine(2)
        assert await harness.client.get_transaction_receipt(str(later.tx_hash)) is None

        fill = await harness.writer.fill_gap(stranded)
        assert fill.succeeded
        chain.mine(2)

        # Once the hole is closed, the transaction behind it mines.
        assert await harness.client.get_transaction_receipt(str(later.tx_hash)) is not None

    async def test_a_filled_gap_is_not_detected_again(self, session_factory: Any) -> None:
        harness = build_harness(session_factory)
        stranded = await harness.allocator.allocate()

        assert len(await harness.allocator.find_gaps(harness.client)) == 1
        await harness.writer.fill_gap(stranded)

        # A recorded fill stops the detector paying to fill the same hole twice.
        assert await harness.allocator.find_gaps(harness.client) == []


class TestRowCreation:
    async def test_the_row_is_created_lazily_at_zero(
        self, session_factory: Any, session: AsyncSession
    ) -> None:
        address = "0x" + "07" * 20
        allocator = NonceAllocator(session_factory, address)

        assert await allocator.current() == 0

        row = (
            await session.execute(select(ChainNonce).where(ChainNonce.address == address))
        ).scalar_one()
        assert row.next_nonce == 0
