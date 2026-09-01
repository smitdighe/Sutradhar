"""Event indexing, reconciliation, and rebuilding the index from the chain alone.

The replay test is the one that matters. If the index can be thrown away and
reconstructed from chain events, then the index is a cache and the chain is the
record. If it cannot, the index is the record and the chain is decoration.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chain.reconcile import (
    HASH_MISMATCH,
    IN_DB_NOT_ON_CHAIN,
    ON_CHAIN_NOT_IN_DB,
    STATUS_DISAGREEMENT,
    reconcile,
)
from app.db.models.catalog import Item
from app.db.models.chain import ChainEvent, IndexerCheckpoint
from app.db.models.enums import ItemStatus
from app.workers.jobs import drain_outbox, run_indexer, sweep_confirmations
from tests.fakes.chain_harness import (
    ChainHarness,
    build_harness,
    make_category,
    make_weaver,
    seed_item,
)

pytestmark = [pytest.mark.integration, pytest.mark.chain]

CONFIRMATIONS = 3


async def anchor_items(
    session: AsyncSession, session_factory: Any, count: int = 3, **overrides: Any
) -> tuple[ChainHarness, list[Item]]:
    """Seed *count* items, anchor them, confirm them, and index the events."""
    harness = build_harness(
        session_factory, chain_confirmations=CONFIRMATIONS, **overrides
    )
    weaver = await make_weaver(session)
    category = await make_category(session)
    items = [
        await seed_item(session, weaver, category, quantity=f"{index + 1}.0000")
        for index in range(count)
    ]
    await session.commit()

    await drain_outbox(harness.runtime)
    harness.chain.mine(CONFIRMATIONS + 1)
    await sweep_confirmations(harness.runtime)
    await run_indexer(harness.runtime)
    return harness, items


class TestIndexing:
    async def test_anchored_events_are_mirrored_into_postgres(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness, items = await anchor_items(session, session_factory, count=3)

        events = (await session.execute(select(ChainEvent))).scalars().all()

        assert len(events) == 3
        assert {event.subject_hash for event in events} == {item.item_hash for item in items}
        for event in events:
            assert event.event_name == "ItemAnchored"
            assert event.issuer_address.lower() == harness.writer.address.lower()
            assert event.block_number > 0
            assert event.block_hash.startswith("0x")

    async def test_the_checkpoint_advances_only_after_the_batch_commits(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness, _ = await anchor_items(session, session_factory, count=1)

        checkpoint = (await session.execute(select(IndexerCheckpoint))).scalar_one()
        assert checkpoint.last_block == harness.chain.head.number

    async def test_replaying_the_same_range_is_idempotent(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness, _ = await anchor_items(session, session_factory, count=4)
        before = (await session.execute(select(ChainEvent))).scalars().all()

        await harness.indexer.reset_checkpoint(0)
        await run_indexer(harness.runtime)
        await run_indexer(harness.runtime)

        after = (await session.execute(select(ChainEvent))).scalars().all()
        # Keyed on (tx_hash, log_index): a window processed twice converges
        # rather than duplicating.
        assert len(after) == len(before) == 4

    async def test_the_window_size_is_respected(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness, _ = await anchor_items(
            session, session_factory, count=1, indexer_block_range=2
        )
        harness.chain.mine(10)
        await harness.indexer.reset_checkpoint(0)

        report = await harness.indexer.run()

        assert report.windows >= 5
        assert report.to_block == harness.chain.head.number

    async def test_a_reorg_removes_the_orphaned_events_from_the_mirror(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness, _ = await anchor_items(session, session_factory, count=2)
        assert len((await session.execute(select(ChainEvent))).scalars().all()) == 2

        event = (await session.execute(select(ChainEvent))).scalars().first()
        assert event is not None
        harness.chain.reorg(event.block_number)
        await harness.indexer.reset_checkpoint(0)
        await run_indexer(harness.runtime)

        # An orphaned block's logs must not linger as an assertion about a chain
        # that no longer exists.
        assert (await session.execute(select(ChainEvent))).scalars().all() == []


class TestReconcile:
    async def test_a_healthy_system_reports_zero_drift(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness, _ = await anchor_items(session, session_factory, count=3)

        report = await reconcile(session_factory, harness.client, harness.settings)

        assert report.clean, [drift.as_dict() for drift in report.drifts]
        assert report.items_checked == 3
        assert report.events_checked == 3

    async def test_a_confirmed_item_with_no_event_is_the_worst_category(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness, items = await anchor_items(session, session_factory, count=1)
        await session.execute(delete(ChainEvent))
        await session.commit()

        report = await reconcile(session_factory, harness.client, harness.settings)

        # This is the shape of a lie told to a consumer: the database says
        # anchored and the chain has nothing to back it.
        assert len(report.in_db_not_on_chain) == 1
        assert report.in_db_not_on_chain[0].kind == IN_DB_NOT_ON_CHAIN
        assert report.in_db_not_on_chain[0].item_id == items[0].id

    async def test_an_anchor_with_no_matching_item_is_reported(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness, _ = await anchor_items(session, session_factory, count=1)
        event = (await session.execute(select(ChainEvent))).scalar_one()
        event.subject_hash = "0x" + "fe" * 32
        await session.commit()

        report = await reconcile(session_factory, harness.client, harness.settings)

        assert any(drift.kind == ON_CHAIN_NOT_IN_DB for drift in report.drifts)

    async def test_a_row_edited_after_anchoring_is_a_hash_mismatch(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness, items = await anchor_items(session, session_factory, count=1)
        item = items[0]
        # The digest still verifies against the recorded preimage; the row no
        # longer matches it. Only comparing the two shows that.
        item.quantity = item.quantity + 1
        await session.commit()

        report = await reconcile(session_factory, harness.client, harness.settings)

        assert len(report.hash_mismatch) == 1
        assert report.hash_mismatch[0].kind == HASH_MISMATCH
        assert "quantity" in report.hash_mismatch[0].detail

    async def test_a_pending_item_with_a_deep_event_is_a_status_disagreement(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness, items = await anchor_items(session, session_factory, count=1)
        # Refresh first: the sweep confirmed this row in another session, so the
        # in-memory instance still believes it is PENDING and assigning PENDING
        # to it would emit no UPDATE at all.
        await session.refresh(items[0])
        items[0].status = ItemStatus.PENDING
        await session.commit()

        report = await reconcile(session_factory, harness.client, harness.settings)

        assert len(report.status_disagreement) == 1
        assert report.status_disagreement[0].kind == STATUS_DISAGREEMENT

    async def test_reconcile_never_corrects_what_it_reports(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness, items = await anchor_items(session, session_factory, count=1)
        await session.refresh(items[0])
        items[0].status = ItemStatus.PENDING
        await session.commit()

        await reconcile(session_factory, harness.client, harness.settings)

        await session.refresh(items[0])
        # A silent auto-correction hides the bug that caused the drift.
        assert items[0].status == ItemStatus.PENDING

    async def test_reconcile_runs_without_a_chain_client(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness, _ = await anchor_items(session, session_factory, count=2)

        report = await reconcile(session_factory, None, harness.settings)

        # Drift is most likely exactly when the chain is unreachable, so
        # reconciliation has to stay useful without it.
        assert report.clean


class TestReplay:
    async def test_the_index_rebuilds_from_chain_events_alone_with_zero_drift(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness, items = await anchor_items(session, session_factory, count=5)
        original = {
            (event.tx_hash, event.log_index): event.subject_hash
            for event in (await session.execute(select(ChainEvent))).scalars().all()
        }

        # Empty the index, exactly as scripts/replay_chain.py --into-empty does.
        # Business rows stay: an item's attributes were never on chain and
        # cannot be recovered from it.
        await session.execute(delete(ChainEvent))
        await session.commit()
        await harness.indexer.reset_checkpoint(0)
        assert (await session.execute(select(ChainEvent))).scalars().all() == []

        report = await harness.indexer.run()
        assert report.events_written == 5
        assert report.errors == []

        rebuilt = {
            (event.tx_hash, event.log_index): event.subject_hash
            for event in (await session.execute(select(ChainEvent))).scalars().all()
        }
        assert rebuilt == original

        drift = await reconcile(session_factory, harness.client, harness.settings)
        assert drift.clean, [entry.as_dict() for entry in drift.drifts]
        assert len(items) == drift.items_checked
