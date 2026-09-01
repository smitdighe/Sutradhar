"""The drain job end to end: writes disabled, crash recovery, and the API staying up.

``CHAIN_WRITE_ENABLED=false`` is the switch that keeps a demo alive when the RPC
is dead or the relayer is unfunded, so it gets a test rather than a comment.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from seeds.loader import load_categories
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog import registry
from app.config import get_settings
from app.core.clock import now
from app.db.models.catalog import Item
from app.db.models.chain import ChainTx
from app.db.models.enums import ItemStatus, OutboxStatus, UserRole, UserStatus
from app.db.models.outbox import Outbox
from app.db.models.user import User
from app.workers.jobs import _parse_cron, drain_outbox, sweep_confirmations
from tests.fakes.chain_harness import build_harness, make_category, make_weaver, seed_item

pytestmark = [pytest.mark.integration, pytest.mark.chain]

API = get_settings().api_prefix
PASSWORD = "correct-horse-battery-staple"
CONFIRMATIONS = 3

PATOLA: dict[str, Any] = {
    "warp_count": 120,
    "weft_count": 116,
    "dye_type": "natural",
    "double_ikat": True,
    "loom_type": "pit",
    "weave_days": 210,
    "gi_registration_no": "GI-00232",
}


class TestDrain:
    async def test_the_drain_sends_one_transaction_per_queued_item(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness = build_harness(session_factory, chain_confirmations=CONFIRMATIONS)
        weaver = await make_weaver(session)
        category = await make_category(session)
        for index in range(4):
            await seed_item(session, weaver, category, quantity=f"{index + 1}.0000")
        await session.commit()

        handled = await drain_outbox(harness.runtime)

        assert handled == 4
        rows = (await session.execute(select(ChainTx))).scalars().all()
        assert len(rows) == 4
        # Sequential nonces, no gaps, no duplicates.
        assert sorted(row.nonce for row in rows) == [0, 1, 2, 3]

    async def test_a_job_with_an_unusable_payload_dead_letters_rather_than_vanishing(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        """A job the drain cannot process must end up somewhere a human looks.

        Every ``OutboxJobType`` member is implemented, so the drain's
        unsupported-type branch is unreachable -- mypy proves it, and that is
        the guarantee, not something a test can provoke. What *can* still go
        wrong is a payload that does not carry what its job type needs, from a
        rolling deploy or a hand-edited row. That must retry, exhaust, and land
        in ``dead_letters`` with the reason, rather than being retried forever
        or dropped.
        """
        from app.chain.outbox import enqueue_job
        from app.db.models.enums import OutboxJobType
        from app.db.models.ops import DeadLetter

        harness = build_harness(session_factory, outbox_max_attempts=2)
        await enqueue_job(
            session,
            job_type=OutboxJobType.ANCHOR_ATTESTATION,
            # No statement_hash: structurally valid row, unusable content.
            payload={"attestation_id": str(uuid.uuid4())},
            dedupe_key="0x" + "9a" * 32,
        )
        await session.commit()

        for _ in range(2):
            job = (await session.execute(select(Outbox))).scalar_one()
            await session.refresh(job)
            job.next_attempt_at = now()
            await session.commit()
            await drain_outbox(harness.runtime)

        job = (await session.execute(select(Outbox))).scalar_one()
        await session.refresh(job)
        assert job.status == OutboxStatus.DEAD

        letter = (await session.execute(select(DeadLetter))).scalar_one()
        assert "statement_hash" in letter.error_chain
        assert letter.attempts == 2


class TestWritesDisabled:
    async def test_the_queue_fills_and_nothing_is_sent(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness = build_harness(session_factory, chain_write_enabled=False)
        weaver = await make_weaver(session)
        category = await make_category(session)
        item = await seed_item(session, weaver, category)
        await session.commit()

        await drain_outbox(harness.runtime)

        assert (await session.execute(select(ChainTx))).first() is None
        await session.refresh(item)
        assert item.status == ItemStatus.PENDING

        job = (await session.execute(select(Outbox))).scalar_one()
        await session.refresh(job)
        assert job.status == OutboxStatus.QUEUED
        # Released, not failed: an outage must not consume the retry budget of
        # every queued job and dead-letter the lot.
        assert job.attempts == 0

    async def test_registration_still_returns_201_with_writes_disabled(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        from app.auth.password import hash_password

        await load_categories(session)
        await session.commit()
        registry.invalidate()

        email = f"chain-api-{uuid.uuid4().hex[:8]}@example.com"
        from app.core.crypto_shred import new_salt

        session.add(
            User(
                email=email,
                password_hash=hash_password(PASSWORD),
                display_name="Weaver",
                role=UserRole.WEAVER,
                status=UserStatus.ACTIVE,
                identity_salt=new_salt(),
            )
        )
        await session.commit()

        login = await client.post(
            f"{API}/auth/login", json={"email": email, "password": PASSWORD}
        )
        assert login.status_code == 200
        headers = {
            "Authorization": f"Bearer {login.json()['access_token']}",
            "Idempotency-Key": uuid.uuid4().hex,
        }

        response = await client.post(
            f"{API}/items",
            json={
                "category_slug": "patola-silk",
                "attributes": PATOLA,
                "quantity": "12.0000",
                "quantity_unit": "metre",
            },
            headers=headers,
        )

        # The chain is a dependency, not a prerequisite. A dead RPC endpoint
        # must not turn a registration into a 500.
        assert response.status_code == 201, response.text
        assert response.json()["status"] == ItemStatus.PENDING

        item = (await session.execute(select(Item))).scalar_one()
        assert item.status == ItemStatus.PENDING
        job = (await session.execute(select(Outbox))).scalar_one()
        assert job.status == OutboxStatus.QUEUED
        # The issuer digest travels on the job, so the drain reads one table.
        assert job.payload["issuer_hash"].startswith("0x")


class TestCrashRecovery:
    async def test_a_killed_worker_strands_neither_the_job_nor_the_nonce(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness = build_harness(
            session_factory,
            chain_confirmations=CONFIRMATIONS,
            outbox_lock_stale_seconds=600,
            chain_tx_timeout_seconds=1,
        )
        weaver = await make_weaver(session)
        category = await make_category(session)
        item = await seed_item(session, weaver, category)
        await session.commit()

        # A worker claims the job, takes a nonce, and dies before signing.
        claimed = await harness.outbox.claim()
        assert len(claimed) == 1
        stranded = await harness.allocator.allocate()

        job = (await session.execute(select(Outbox))).scalar_one()
        await session.refresh(job)
        assert job.status == OutboxStatus.IN_FLIGHT
        job.locked_at = now().replace(year=now().year - 1)
        await session.commit()

        # Restart: the lease expires, the hole is filled, the job runs.
        await sweep_confirmations(harness.runtime)
        assert await harness.allocator.find_gaps(harness.client) == []

        assert await drain_outbox(harness.runtime) == 1
        harness.chain.mine(CONFIRMATIONS + 1)
        await sweep_confirmations(harness.runtime)

        await session.refresh(item)
        assert item.status == ItemStatus.CONFIRMED

        rows = (await session.execute(select(ChainTx))).scalars().all()
        # The gap fill plus the real anchor. No nonce is left stranded and no
        # nonce is handed out twice.
        assert sorted(row.nonce for row in rows) == [stranded, stranded + 1]

    async def test_a_stale_lease_is_the_only_thing_that_frees_a_claimed_job(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        harness = build_harness(session_factory, outbox_lock_stale_seconds=600)
        weaver = await make_weaver(session)
        category = await make_category(session)
        await seed_item(session, weaver, category)
        await session.commit()

        await harness.outbox.claim()

        # A live claim is not stolen just because another drain came round.
        assert await drain_outbox(harness.runtime) == 0


class TestCronParsing:
    def test_a_five_field_expression_is_split(self) -> None:
        assert _parse_cron("*/30 * * * *") == {
            "minute": "*/30",
            "hour": "*",
            "day": "*",
            "month": "*",
            "day_of_week": "*",
        }

    def test_a_malformed_expression_is_rejected_loudly(self) -> None:
        with pytest.raises(ValueError, match="five fields"):
            _parse_cron("*/30 * *")
