"""Merkle batching: one root, one transaction, and a proof per item.

The thousand-item test is the answer to "what does this cost at a lakh sarees".
One transaction covers the whole batch and every item keeps an independently
verifiable proof, so the per-item cost falls by about three orders of magnitude
and verification gets no weaker.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chain.batching import assemble_batch, get_inclusion_proof, verify_inclusion
from app.core.hashing import from_hex, hash_hex, keccak256
from app.core.merkle import build_proof, build_root, verify_proof
from app.db.models.chain import ChainTx, MerkleBatch, MerkleLeaf
from app.db.models.enums import ItemStatus, OutboxJobType, OutboxStatus
from app.db.models.outbox import Outbox
from app.workers.jobs import drain_outbox, sweep_confirmations
from tests.fakes.chain_harness import build_harness, make_category, make_weaver, seed_item

pytestmark = [pytest.mark.integration, pytest.mark.chain]

CONFIRMATIONS = 3


class TestProofMath:
    def test_a_thousand_leaves_produce_one_root_and_verifiable_proofs(self) -> None:
        leaves = [keccak256(f"item-{index}".encode()) for index in range(1000)]

        root = build_root(leaves)

        for index in (0, 1, 499, 998, 999):
            proof = build_proof(leaves, index)
            assert verify_proof(leaves[index], proof, root)

    def test_a_tampered_leaf_fails_its_own_proof(self) -> None:
        leaves = [keccak256(f"item-{index}".encode()) for index in range(1000)]
        root = build_root(leaves)
        proof = build_proof(leaves, 42)

        tampered = keccak256(b"item-42-but-altered")

        assert verify_proof(leaves[42], proof, root)
        assert not verify_proof(tampered, proof, root)

    def test_a_proof_from_one_batch_does_not_verify_against_another_root(self) -> None:
        first = [keccak256(f"a-{i}".encode()) for i in range(64)]
        second = [keccak256(f"b-{i}".encode()) for i in range(64)]

        proof = build_proof(first, 7)

        assert not verify_proof(first[7], proof, build_root(second))

    def test_proof_size_is_logarithmic(self) -> None:
        leaves = [keccak256(f"item-{index}".encode()) for index in range(1000)]

        proof = build_proof(leaves, 500)

        # ceil(log2(1000)) == 10. A proof that grew linearly would make batching
        # pointless: the saving would move from gas to bandwidth.
        assert len(proof) <= 10


class TestAssembly:
    async def test_queued_item_anchors_fold_into_one_batch(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        build_harness(session_factory)
        weaver = await make_weaver(session)
        category = await make_category(session)
        items = [
            await seed_item(session, weaver, category, quantity=f"{i + 1}.0000")
            for i in range(8)
        ]
        await session.commit()

        assembly = await assemble_batch(session_factory)

        assert assembly is not None
        assert assembly.leaf_count == 8

        batch = (await session.execute(select(MerkleBatch))).scalar_one()
        assert batch.root == assembly.root
        leaves = (
            (
                await session.execute(
                    select(MerkleLeaf).order_by(MerkleLeaf.leaf_index)
                )
            )
            .scalars()
            .all()
        )
        assert [leaf.leaf_index for leaf in leaves] == list(range(8))
        assert {leaf.item_id for leaf in leaves} == {item.id for item in items}

        # The stored root is the one an independent verifier would compute from
        # the item hashes alone, in leaf order.
        expected = hash_hex(build_root([from_hex(item.item_hash) for item in items]))
        assert batch.root == expected

    async def test_one_batch_job_replaces_the_individual_item_jobs(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        weaver = await make_weaver(session)
        category = await make_category(session)
        for index in range(5):
            await seed_item(session, weaver, category, quantity=f"{index + 1}.0000")
        await session.commit()

        assembly = await assemble_batch(session_factory)
        assert assembly is not None

        jobs = (await session.execute(select(Outbox))).scalars().all()
        item_jobs = [job for job in jobs if job.job_type == OutboxJobType.ANCHOR_ITEM]
        batch_jobs = [job for job in jobs if job.job_type == OutboxJobType.ANCHOR_BATCH]

        assert len(batch_jobs) == 1
        assert batch_jobs[0].status == OutboxStatus.QUEUED
        assert batch_jobs[0].dedupe_key == assembly.root
        # The item jobs really are done: their anchoring was delegated, and the
        # note records which root absorbed them.
        assert all(job.status == OutboxStatus.DONE for job in item_jobs)
        assert all(job.payload["folded_into_root"] == assembly.root for job in item_jobs)

    async def test_an_empty_queue_assembles_nothing(self, session_factory: Any) -> None:
        assert await assemble_batch(session_factory) is None


class TestAnchoringABatch:
    async def test_one_transaction_anchors_the_whole_batch(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness = build_harness(
            session_factory, chain_confirmations=CONFIRMATIONS, batching_enabled=True
        )
        weaver = await make_weaver(session)
        category = await make_category(session)
        items = [
            await seed_item(session, weaver, category, quantity=f"{i + 1}.0000")
            for i in range(20)
        ]
        await session.commit()

        # The drain assembles the batch and then sends exactly one transaction.
        await drain_outbox(harness.runtime)
        harness.chain.mine(CONFIRMATIONS + 1)
        await sweep_confirmations(harness.runtime)

        transactions = (await session.execute(select(ChainTx))).scalars().all()
        assert len(transactions) == 1

        for item in items:
            await session.refresh(item)
            assert item.status == ItemStatus.CONFIRMED

        batch = (await session.execute(select(MerkleBatch))).scalar_one()
        assert batch.anchored_tx_id == transactions[0].id
        assert batch.root in harness.chain.batch_anchors

    async def test_every_item_gets_an_inclusion_proof_that_verifies(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness = build_harness(
            session_factory, chain_confirmations=CONFIRMATIONS, batching_enabled=True
        )
        weaver = await make_weaver(session)
        category = await make_category(session)
        items = [
            await seed_item(session, weaver, category, quantity=f"{i + 1}.0000")
            for i in range(16)
        ]
        await session.commit()

        await drain_outbox(harness.runtime)
        harness.chain.mine(CONFIRMATIONS + 1)
        await sweep_confirmations(harness.runtime)

        for item in items:
            proof = await get_inclusion_proof(session, item.id)
            assert proof is not None
            assert proof.item_hash == item.item_hash
            assert proof.anchored
            assert verify_inclusion(proof.item_hash, proof.proof, proof.root)
            # And the root really is the one the chain holds.
            assert proof.root in harness.chain.batch_anchors

    async def test_a_tampered_item_hash_fails_its_stored_proof(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness = build_harness(
            session_factory, chain_confirmations=CONFIRMATIONS, batching_enabled=True
        )
        weaver = await make_weaver(session)
        category = await make_category(session)
        item = await seed_item(session, weaver, category)
        for index in range(7):
            await seed_item(session, weaver, category, quantity=f"{index + 2}.0000")
        await session.commit()

        await drain_outbox(harness.runtime)
        proof = await get_inclusion_proof(session, item.id)
        assert proof is not None

        forged = hash_hex(keccak256(b"a saree that was never registered"))

        assert verify_inclusion(proof.item_hash, proof.proof, proof.root)
        assert not verify_inclusion(forged, proof.proof, proof.root)

    async def test_an_unbatched_item_has_no_proof(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        weaver = await make_weaver(session)
        category = await make_category(session)
        item = await seed_item(session, weaver, category)
        await session.commit()

        assert await get_inclusion_proof(session, item.id) is None


class TestBatchReorg:
    async def test_a_reorged_batch_demotes_every_item_it_covered(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness = build_harness(
            session_factory, chain_confirmations=CONFIRMATIONS, batching_enabled=True
        )
        weaver = await make_weaver(session)
        category = await make_category(session)
        items = [
            await seed_item(session, weaver, category, quantity=f"{i + 1}.0000")
            for i in range(6)
        ]
        await session.commit()

        await drain_outbox(harness.runtime)
        harness.chain.mine(CONFIRMATIONS + 1)
        await sweep_confirmations(harness.runtime)

        transaction = (await session.execute(select(ChainTx))).scalar_one()
        await session.refresh(transaction)
        harness.chain.reorg(int(transaction.block_number or 1))
        await sweep_confirmations(harness.runtime)

        for item in items:
            await session.refresh(item)
            # A reorged batch un-anchors every item it covered, not just one.
            assert item.status == ItemStatus.PENDING
