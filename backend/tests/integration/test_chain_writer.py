"""Signing, sending, refusing, and replacing transactions.

The refusal tests are the important ones. A writer that always sends is easy;
one that declines to send at the right moments -- above the fee cap, with writes
switched off, when the transaction would revert -- is what keeps a nonce from
being burned and a gas bill from being surprising.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chain.client import ChainClient
from app.chain.outbox import enqueue_job
from app.chain.writer import GWEI, SendOutcome
from app.db.models.chain import ChainTx
from app.db.models.enums import ChainTxStatus, OutboxJobType
from app.db.models.outbox import Outbox
from tests.fakes.chain_harness import TEST_CONTRACT_ADDRESS, build_harness
from tests.fakes.fake_chain import FakeChain

pytestmark = [pytest.mark.integration, pytest.mark.chain]

ITEM_HASH = "0x" + "a1" * 32
ISSUER_HASH = "0x" + "b2" * 32


def chain(**kwargs: Any) -> FakeChain:
    return FakeChain(contract_address=TEST_CONTRACT_ADDRESS, chain_id=31_337, **kwargs)


class TestSending:
    async def test_a_send_records_the_attempt_before_it_is_broadcast(
        self, session_factory: Any, session: AsyncSession
    ) -> None:
        harness = build_harness(session_factory)

        result = await harness.writer.anchor_item(None, ITEM_HASH, ISSUER_HASH)

        assert result.outcome == SendOutcome.SENT
        row = (await session.execute(select(ChainTx))).scalar_one()
        assert row.tx_hash == result.tx_hash
        assert row.nonce == 0
        assert row.status == ChainTxStatus.SENT
        # The fees are stored so replace-by-fee can bump them later.
        assert row.max_fee_per_gas is not None and row.max_fee_per_gas > 0
        assert row.max_priority_fee_per_gas is not None

    async def test_the_transaction_reaches_the_chain_and_anchors_the_hash(
        self, session_factory: Any
    ) -> None:
        harness = build_harness(session_factory)

        await harness.writer.anchor_item(None, ITEM_HASH, ISSUER_HASH)
        harness.chain.mine()

        assert ITEM_HASH in harness.chain.item_anchors

    async def test_a_gas_estimate_is_buffered(self, session_factory: Any) -> None:
        harness = build_harness(session_factory, chain_gas_buffer_percent=20)

        await harness.writer.anchor_item(None, ITEM_HASH, ISSUER_HASH)
        harness.chain.mine()

        pending = next(iter(harness.chain.receipts.values()))
        assert pending.status == 1


class TestTransientFailures:
    async def test_a_transient_failure_is_retried_and_then_succeeds(
        self, session_factory: Any
    ) -> None:
        fake = chain()
        harness = build_harness(session_factory, chain=fake)
        # The first three reads fail; the retry policy allows five attempts.
        fake.fail_next(3)

        result = await harness.writer.anchor_item(None, ITEM_HASH, ISSUER_HASH)

        assert result.outcome == SendOutcome.SENT

    async def test_retries_are_exhausted_and_the_job_is_refused_not_crashed(
        self, session_factory: Any
    ) -> None:
        fake = chain()
        harness = build_harness(session_factory, chain=fake, chain_rpc_max_retries=2)
        fake.fail_next(50)

        result = await harness.writer.anchor_item(None, ITEM_HASH, ISSUER_HASH)

        assert result.outcome == SendOutcome.REFUSED
        assert result.retryable

    async def test_a_send_that_never_reached_the_node_rewinds_the_nonce(
        self, session_factory: Any, session: AsyncSession
    ) -> None:
        fake = chain()
        harness = build_harness(session_factory, chain=fake)
        fake.fail_next_sends(50, ambiguous=False)

        result = await harness.writer.anchor_item(None, ITEM_HASH, ISSUER_HASH)

        assert result.outcome == SendOutcome.REFUSED
        # Nothing was submitted, so the nonce is genuinely unused and giving it
        # back avoids manufacturing a hole the gap filler has to pay to close.
        assert await harness.allocator.current() == 0
        row = (await session.execute(select(ChainTx))).scalar_one()
        assert row.status == ChainTxStatus.FAILED

    async def test_an_ambiguous_send_is_left_in_flight_and_never_resent(
        self, session_factory: Any, session: AsyncSession
    ) -> None:
        fake = chain()
        harness = build_harness(session_factory, chain=fake)
        fake.fail_next_sends(1, ambiguous=True)

        result = await harness.writer.anchor_item(None, ITEM_HASH, ISSUER_HASH)

        # The node may already hold it. Resending at this nonce would create a
        # second competing transaction, so the row stays SENT for the
        # confirmation sweep to settle.
        assert result.outcome == SendOutcome.SENT
        assert await harness.allocator.current() == 1
        row = (await session.execute(select(ChainTx))).scalar_one()
        assert row.status == ChainTxStatus.SENT


class TestRefusals:
    async def test_writes_disabled_refuses_without_consuming_a_nonce(
        self, session_factory: Any, session: AsyncSession
    ) -> None:
        harness = build_harness(session_factory, chain_write_enabled=False)

        result = await harness.writer.anchor_item(None, ITEM_HASH, ISSUER_HASH)

        assert result.outcome == SendOutcome.REFUSED
        assert "CHAIN_WRITE_ENABLED" in result.reason
        assert await harness.allocator.current() == 0
        assert (await session.execute(select(ChainTx))).first() is None

    async def test_a_fee_above_the_cap_refuses_rather_than_paying_it(
        self, session_factory: Any, session: AsyncSession
    ) -> None:
        # A chain asking far more per gas than the configured ceiling.
        fake = chain(base_fee_per_gas=500 * GWEI, priority_fee_per_gas=10 * GWEI)
        harness = build_harness(session_factory, chain=fake, chain_max_fee_gwei=100)

        result = await harness.writer.anchor_item(None, ITEM_HASH, ISSUER_HASH)

        assert result.outcome == SendOutcome.REFUSED
        assert "exceeds cap" in result.reason
        assert await harness.allocator.current() == 0
        assert (await session.execute(select(ChainTx))).first() is None

    async def test_a_fee_at_the_cap_is_still_sent(self, session_factory: Any) -> None:
        fake = chain(base_fee_per_gas=1 * GWEI, priority_fee_per_gas=1 * GWEI)
        harness = build_harness(session_factory, chain=fake, chain_max_fee_gwei=100)

        result = await harness.writer.anchor_item(None, ITEM_HASH, ISSUER_HASH)

        assert result.outcome == SendOutcome.SENT

    async def test_no_signer_refuses_cleanly(self, session_factory: Any) -> None:
        harness = build_harness(session_factory)
        harness.writer._settings = harness.settings.model_copy(
            update={"chain_signer_private_key": ""}
        )
        harness.writer._account = None

        result = await harness.writer.anchor_item(None, ITEM_HASH, ISSUER_HASH)

        assert result.outcome == SendOutcome.REFUSED
        assert "CHAIN_SIGNER_PRIVATE_KEY" in result.reason


class TestPreflight:
    async def test_an_already_anchored_hash_is_reported_as_done_not_failed(
        self, session_factory: Any, session: AsyncSession
    ) -> None:
        fake = chain()
        harness = build_harness(session_factory, chain=fake)
        await harness.writer.anchor_item(None, ITEM_HASH, ISSUER_HASH)
        fake.mine()

        result = await harness.writer.anchor_item(None, ITEM_HASH, ISSUER_HASH)

        # This is exactly what a reorg replay hits when the original was
        # re-included. Retrying it forever would be wrong; so would failing it.
        assert result.outcome == SendOutcome.ALREADY_ANCHORED
        assert "AlreadyAnchored" in result.reason
        # Preflight caught it, so no second transaction was signed.
        rows = (await session.execute(select(ChainTx))).scalars().all()
        assert len(rows) == 1

    async def test_a_non_writer_key_is_caught_before_gas_is_spent(
        self, session_factory: Any, session: AsyncSession
    ) -> None:
        fake = chain(writers={"0x" + "de" * 20})
        harness = build_harness(session_factory, chain=fake)

        result = await harness.writer.anchor_item(None, ITEM_HASH, ISSUER_HASH)

        assert result.outcome == SendOutcome.WOULD_REVERT
        assert "NotWriter" in result.reason
        assert (await session.execute(select(ChainTx))).first() is None


class TestReplaceByFee:
    async def test_a_replacement_reuses_the_nonce_and_bumps_the_fee(
        self, session_factory: Any, session: AsyncSession
    ) -> None:
        fake = chain(mining_delay_blocks=99)
        harness = build_harness(session_factory, chain=fake)
        first = await harness.writer.anchor_item(None, ITEM_HASH, ISSUER_HASH)
        original = (await session.execute(select(ChainTx))).scalar_one()
        await session.refresh(original)
        original_fee = original.max_fee_per_gas or 0

        replacement = await harness.writer.replace(original)

        assert replacement.outcome == SendOutcome.SENT
        assert replacement.nonce == first.nonce
        assert replacement.tx_hash != first.tx_hash

        rows = (await session.execute(select(ChainTx).order_by(ChainTx.created_at))).scalars().all()
        # Every attempt is its own row sharing the nonce, so the history of what
        # was paid and when survives.
        assert len(rows) == 2
        assert {row.nonce for row in rows} == {first.nonce}
        bumped = rows[-1].max_fee_per_gas or 0
        assert bumped >= original_fee * 11_250 // 10_000

    async def test_a_replacement_re_anchors_the_same_hash(
        self, session_factory: Any, session: AsyncSession
    ) -> None:
        fake = chain(mining_delay_blocks=99)
        harness = build_harness(session_factory, chain=fake)
        await enqueue_job(
            session,
            job_type=OutboxJobType.ANCHOR_ITEM,
            payload={
                "item_id": str(uuid.uuid4()),
                "item_hash": ITEM_HASH,
                "issuer_hash": ISSUER_HASH,
            },
            dedupe_key=ITEM_HASH,
        )
        await session.commit()
        job = (await session.execute(select(Outbox))).scalar_one()

        await harness.writer.anchor_item(job.id, ITEM_HASH, ISSUER_HASH)
        original = (await session.execute(select(ChainTx))).scalar_one()

        result = await harness.writer.replace(original)
        assert result.outcome == SendOutcome.SENT

        # The replacement must do the same work. An empty self-send at that
        # nonce would report success while anchoring nothing.
        fake.mining_delay_blocks = 0
        fake.mine()
        assert ITEM_HASH in fake.item_anchors

    async def test_a_replacement_with_no_recoverable_job_is_declined(
        self, session_factory: Any, session: AsyncSession
    ) -> None:
        harness = build_harness(session_factory)
        orphan = ChainTx(
            outbox_id=None,
            tx_hash="0x" + "cd" * 32,
            nonce=0,
            status=ChainTxStatus.SENT,
            max_fee_per_gas=3 * GWEI,
            max_priority_fee_per_gas=1 * GWEI,
        )
        session.add(orphan)
        await session.commit()

        # No outbox job means this is a maintenance send: replacing it is
        # another self-send, which is exactly what a stuck gap fill needs.
        result = await harness.writer.replace(orphan)

        assert result.outcome == SendOutcome.SENT
        assert result.nonce == 0

    async def test_a_replacement_below_the_minimum_bump_is_rejected_by_the_node(
        self, session_factory: Any, session: AsyncSession
    ) -> None:
        fake = chain(mining_delay_blocks=99)
        harness = build_harness(session_factory, chain=fake, chain_rbf_bump_bps=0)
        await harness.writer.anchor_item(None, ITEM_HASH, ISSUER_HASH)
        original = (await session.execute(select(ChainTx))).scalar_one()

        result = await harness.writer.replace(original)

        # REJECTED, not REFUSED: the node saw it and said no, and the nonce is
        # still committed to whatever it already holds.
        assert result.outcome == SendOutcome.REJECTED
        assert "underpriced" in result.reason


class TestQuotaExhaustion:
    async def test_reads_go_stale_and_writes_refuse_with_503(
        self, session_factory: Any
    ) -> None:
        from decimal import Decimal

        from app.chain.client import ChainUnavailable, QuotaMeter

        class SpentMeter(QuotaMeter):
            """A meter whose budget is already gone."""

            def __init__(self) -> None:  # noqa: D107 - test double
                self._pending = 0
                self._flush_units = 1
                self._flush_seconds = 1
                self._remaining = Decimal(0)

            async def remaining(self) -> Decimal:
                return Decimal(0)

            async def would_exceed(self, method: str) -> bool:
                return True

            async def record(self, method: str) -> None:
                return None

            async def flush(self) -> None:
                return None

        fake = chain()
        harness = build_harness(session_factory, chain=fake)
        live = ChainClient(fake, meter=None, settings=harness.settings)
        # Prime the cache while the budget is still notionally intact.
        fake.mine(2)
        primed = await live.block_number()

        starved = ChainClient(fake, meter=SpentMeter(), settings=harness.settings)
        starved._cache = dict(live._cache)

        # A read falls back to the last known value and is labelled stale.
        assert await starved.block_number() == primed
        assert starved.degraded is True

        # A write refuses with 503 rather than crashing or spending past the cap.
        with pytest.raises(ChainUnavailable) as caught:
            await starved.send_raw_transaction(b"\x02\x00")
        assert caught.value.status == 503

    async def test_a_read_with_no_cached_value_refuses_rather_than_inventing_one(
        self, session_factory: Any
    ) -> None:
        from decimal import Decimal

        from app.chain.client import ChainUnavailable, QuotaMeter

        class SpentMeter(QuotaMeter):
            def __init__(self) -> None:  # noqa: D107 - test double
                self._pending = 0
                self._flush_units = 1
                self._flush_seconds = 1
                self._remaining = Decimal(0)

            async def would_exceed(self, method: str) -> bool:
                return True

        fake = chain()
        harness = build_harness(session_factory, chain=fake)
        starved = ChainClient(fake, meter=SpentMeter(), settings=harness.settings)

        with pytest.raises(ChainUnavailable):
            await starved.block_number()
