"""Fraud-flag propagation: what a flag touches, what it does not, and how fast.

The ten-thousand-item test is not about speed for its own sake. A flag applied
with a per-row loop takes long enough that whoever ran it gives up and kills the
process, and a half-applied fraud flag is the worst state in the system: the
actor is flagged, some of their records are disputed, and the rest are still
being shown to consumers as if nothing happened.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.attestation import service
from app.attestation.reputation import clear_fraud_flag, flag_actor, raise_dispute
from app.attestation.trust import TrustLevel, assess
from app.core.crypto_shred import new_salt
from app.db.models.attestation import ItemDispute
from app.db.models.catalog import Item, ItemEvent
from app.db.models.enums import (
    AuthEventType,
    DisputeSource,
    DisputeStatus,
    ItemEventType,
    UserRole,
    UserStatus,
)
from app.db.models.user import AuthEvent, User
from tests.fakes.chain_harness import make_category, seed_item

pytestmark = [pytest.mark.integration, pytest.mark.chain]


async def make_actor(session: AsyncSession, role: UserRole) -> User:
    from app.auth.password import hash_password

    user = User(
        email=f"{role.lower()}-{uuid.uuid4().hex[:10]}@example.com",
        password_hash=hash_password("correct-horse-battery-staple"),
        display_name=f"Test {role}",
        role=role,
        status=UserStatus.ACTIVE,
        identity_salt=new_salt(),
    )
    session.add(user)
    await session.flush()
    return user


class TestFlagPropagation:
    async def test_flagging_disputes_every_item_the_actor_registered(
        self, session: AsyncSession
    ) -> None:
        weaver = await make_actor(session, UserRole.WEAVER)
        category = await make_category(session)
        items = [
            await seed_item(session, weaver, category, quantity=f"{n + 1}.0000", enqueue=False)
            for n in range(6)
        ]
        await session.commit()

        outcome = await flag_actor(session, weaver.id, "loom capacity does not add up", None)
        await session.commit()

        assert outcome.items_affected == 6
        for item in items:
            await session.refresh(item)
            assert item.dispute_status is DisputeStatus.DISPUTED

    async def test_a_flag_writes_the_audit_event_and_the_dispute_rows(
        self, session: AsyncSession
    ) -> None:
        weaver = await make_actor(session, UserRole.WEAVER)
        admin = await make_actor(session, UserRole.ADMIN)
        category = await make_category(session)
        await seed_item(session, weaver, category, enqueue=False)
        await session.commit()

        await flag_actor(
            session, weaver.id, "volumes inconsistent with declared capacity", admin.id
        )
        await session.commit()

        await session.refresh(weaver)
        assert weaver.fraud_flagged_at is not None

        auth_event = (
            await session.execute(
                select(AuthEvent).where(AuthEvent.event_type == AuthEventType.FRAUD_FLAG)
            )
        ).scalar_one()
        assert auth_event.user_id == weaver.id
        assert auth_event.detail is not None
        assert auth_event.detail["flagged_by"] == str(admin.id)

        dispute = (await session.execute(select(ItemDispute))).scalar_one()
        assert dispute.source is DisputeSource.FRAUD_FLAG
        # triggered_by is what makes the reversal selective later.
        assert dispute.triggered_by == weaver.id
        assert dispute.raised_by == admin.id
        assert dispute.cleared_at is None

    async def test_each_disputed_item_gets_its_own_provenance_event(
        self, session: AsyncSession
    ) -> None:
        weaver = await make_actor(session, UserRole.WEAVER)
        category = await make_category(session)
        for n in range(4):
            await seed_item(session, weaver, category, quantity=f"{n + 1}.0000", enqueue=False)
        await session.commit()

        await flag_actor(session, weaver.id, "records do not reconcile", None)
        await session.commit()

        events = (
            (
                await session.execute(
                    select(ItemEvent).where(ItemEvent.event_type == ItemEventType.DISPUTED)
                )
            )
            .scalars()
            .all()
        )
        # The status must not shift under a reader with no event explaining it.
        assert len(events) == 4
        assert all(e.payload["source"] == DisputeSource.FRAUD_FLAG.value for e in events)

    async def test_flagging_is_idempotent(self, session: AsyncSession) -> None:
        weaver = await make_actor(session, UserRole.WEAVER)
        category = await make_category(session)
        await seed_item(session, weaver, category, enqueue=False)
        await session.commit()

        first = await flag_actor(session, weaver.id, "first look", None)
        await session.commit()
        second = await flag_actor(session, weaver.id, "second look", None)
        await session.commit()

        assert first.items_affected == 1
        assert second.already_in_state is True
        assert second.items_affected == 0
        assert len((await session.execute(select(ItemDispute))).scalars().all()) == 1

    async def test_items_the_actor_only_attested_to_are_not_disputed(
        self, session: AsyncSession
    ) -> None:
        weaver = await make_actor(session, UserRole.WEAVER)
        officer = await make_actor(session, UserRole.COOP_OFFICER)
        category = await make_category(session)
        item = await seed_item(session, weaver, category, enqueue=False)
        await session.commit()
        await service.create_attestation(session, item.id, {"note": "visited"}, officer)
        await session.commit()

        await flag_actor(session, officer.id, "sign-offs without site visits", None)
        await session.commit()

        await session.refresh(item)
        # The item's own provenance is not in question -- a different person
        # registered it -- so nothing is written against it.
        assert item.dispute_status is DisputeStatus.NONE
        assert (await session.execute(select(ItemDispute))).first() is None

    async def test_a_flagged_attestors_endorsement_stops_counting_on_the_next_read(
        self, session: AsyncSession
    ) -> None:
        weaver = await make_actor(session, UserRole.WEAVER)
        officer = await make_actor(session, UserRole.COOP_OFFICER)
        category = await make_category(session)
        item = await seed_item(session, weaver, category, enqueue=False)
        await session.commit()
        await service.create_attestation(session, item.id, {"note": "visited"}, officer)
        await session.commit()
        assert (await assess(session, item)).level is TrustLevel.CO_OP_ATTESTED

        await flag_actor(session, officer.id, "sign-offs without site visits", None)
        await session.commit()

        # No cache, no backfill job, no window in which the old level is still
        # being served. The level is recomputed from the evidence every read.
        assert (await assess(session, item)).level is TrustLevel.DISPUTED

    async def test_flagging_an_unknown_actor_is_a_404(self, session: AsyncSession) -> None:
        from app.core.errors import ErrorCode, NotFoundError

        with pytest.raises(NotFoundError) as caught:
            await flag_actor(session, uuid.uuid4(), "who?", None)

        assert caught.value.code is ErrorCode.USER_NOT_FOUND


class TestClearing:
    async def test_clearing_restores_items_the_flag_disputed(
        self, session: AsyncSession
    ) -> None:
        weaver = await make_actor(session, UserRole.WEAVER)
        category = await make_category(session)
        items = [
            await seed_item(session, weaver, category, quantity=f"{n + 1}.0000", enqueue=False)
            for n in range(3)
        ]
        await session.commit()
        await flag_actor(session, weaver.id, "under investigation", None)
        await session.commit()

        outcome = await clear_fraud_flag(session, weaver.id, None, "investigation closed")
        await session.commit()

        assert outcome.items_affected == 3
        await session.refresh(weaver)
        assert weaver.fraud_flagged_at is None
        for item in items:
            await session.refresh(item)
            assert item.dispute_status is DisputeStatus.NONE

    async def test_an_independently_disputed_item_stays_disputed(
        self, session: AsyncSession
    ) -> None:
        weaver = await make_actor(session, UserRole.WEAVER)
        inspector = await make_actor(session, UserRole.INSPECTOR)
        category = await make_category(session)
        clean = await seed_item(session, weaver, category, quantity="1.0000", enqueue=False)
        contested = await seed_item(session, weaver, category, quantity="2.0000", enqueue=False)
        await session.commit()

        # Two independent reasons against `contested`; one against `clean`.
        await raise_dispute(
            session,
            contested.id,
            DisputeSource.INSPECTION,
            "fibre analysis inconsistent with the declared weave",
            inspector.id,
        )
        await session.commit()
        await flag_actor(session, weaver.id, "under investigation", None)
        await session.commit()

        await clear_fraud_flag(session, weaver.id, None, "investigation closed")
        await session.commit()

        await session.refresh(clean)
        await session.refresh(contested)
        assert clean.dispute_status is DisputeStatus.NONE
        # The inspector's finding is not this admin's to lift.
        assert contested.dispute_status is DisputeStatus.DISPUTED

        remaining = (
            (
                await session.execute(
                    select(ItemDispute).where(ItemDispute.cleared_at.is_(None))
                )
            )
            .scalars()
            .all()
        )
        assert len(remaining) == 1
        assert remaining[0].source is DisputeSource.INSPECTION

    async def test_cleared_disputes_are_closed_not_deleted(
        self, session: AsyncSession
    ) -> None:
        weaver = await make_actor(session, UserRole.WEAVER)
        admin = await make_actor(session, UserRole.ADMIN)
        category = await make_category(session)
        await seed_item(session, weaver, category, enqueue=False)
        await session.commit()
        await flag_actor(session, weaver.id, "under investigation", None)
        await session.commit()

        await clear_fraud_flag(session, weaver.id, admin.id, "closed with no finding")
        await session.commit()

        dispute = (await session.execute(select(ItemDispute))).scalar_one()
        # A dispute that vanishes is indistinguishable from one that never
        # happened. It reads as raised-then-lifted, by whom, when.
        assert dispute.cleared_at is not None
        assert dispute.cleared_by == admin.id

    async def test_clearing_writes_a_dispute_cleared_event(
        self, session: AsyncSession
    ) -> None:
        weaver = await make_actor(session, UserRole.WEAVER)
        category = await make_category(session)
        await seed_item(session, weaver, category, enqueue=False)
        await session.commit()
        await flag_actor(session, weaver.id, "under investigation", None)
        await session.commit()

        await clear_fraud_flag(session, weaver.id, None, "closed")
        await session.commit()

        events = (
            (
                await session.execute(
                    select(ItemEvent).where(
                        ItemEvent.event_type == ItemEventType.DISPUTE_CLEARED
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1

    async def test_clearing_an_unflagged_actor_changes_nothing(
        self, session: AsyncSession
    ) -> None:
        weaver = await make_actor(session, UserRole.WEAVER)
        await session.commit()

        outcome = await clear_fraud_flag(session, weaver.id, None, "nothing to clear")
        await session.commit()

        assert outcome.already_in_state is True
        assert outcome.items_affected == 0

    async def test_a_reflag_after_a_clear_opens_a_fresh_dispute(
        self, session: AsyncSession
    ) -> None:
        weaver = await make_actor(session, UserRole.WEAVER)
        category = await make_category(session)
        item = await seed_item(session, weaver, category, enqueue=False)
        await session.commit()

        await flag_actor(session, weaver.id, "first investigation", None)
        await session.commit()
        await clear_fraud_flag(session, weaver.id, None, "closed")
        await session.commit()
        await flag_actor(session, weaver.id, "second investigation", None)
        await session.commit()

        disputes = (
            (await session.execute(select(ItemDispute).order_by(ItemDispute.raised_at)))
            .scalars()
            .all()
        )
        # Each cycle is its own row; the partial unique index only constrains
        # the open one.
        assert len(disputes) == 2
        assert disputes[0].cleared_at is not None
        assert disputes[1].cleared_at is None
        await session.refresh(item)
        assert item.dispute_status is DisputeStatus.DISPUTED


class TestBulkPerformance:
    async def test_ten_thousand_items_flag_in_a_constant_number_of_statements(
        self, session: AsyncSession, session_factory: Any, engine: Any
    ) -> None:
        """The property that matters is set-based, not a literal statement count.

        A per-row loop is the failure mode: ten thousand round trips take long
        enough that the operation gets abandoned half-applied. So this asserts
        the statement count does not grow with the number of items -- measured
        at ten and at ten thousand, and required to be identical.
        """
        weaver = await make_actor(session, UserRole.WEAVER)
        other = await make_actor(session, UserRole.WEAVER)
        category = await make_category(session)
        await session.commit()

        # Ten items for the small measurement, on a different actor.
        for n in range(10):
            await seed_item(session, other, category, quantity=f"{n + 1}.0000", enqueue=False)
        await session.commit()

        # Ten thousand rows, inserted in bulk rather than through the ORM: this
        # test is about the flag, not about how the fixture got there.
        from decimal import Decimal

        from app.core.clock import now
        from app.core.ids import new_uuid

        moment = now()
        rows = [
            {
                "id": new_uuid(),
                "category_id": category.id,
                "category_schema_version": category.schema_version,
                "parent_id": None,
                "registered_by": weaver.id,
                "attributes": {"n": n},
                "quantity": Decimal("1.0000"),
                "quantity_unit": category.quantity_unit,
                "item_hash": f"0x{n:064x}",
                "tag_code": None,
                "created_at": moment,
                "updated_at": moment,
            }
            for n in range(10_000)
        ]
        async with session_factory() as bulk:
            await bulk.execute(Item.__table__.insert(), rows)
            await bulk.commit()

        counts: dict[str, int] = {}

        def counted(label: str) -> Any:
            def before_cursor_execute(conn, cursor, statement, parameters, context, many):
                counts[label] = counts.get(label, 0) + 1

            return before_cursor_execute

        sync_engine = engine.sync_engine

        # --- ten items ---
        handler = counted("small")
        event.listen(sync_engine, "before_cursor_execute", handler)
        async with session_factory() as small_session:
            await flag_actor(small_session, other.id, "small batch check", None)
            await small_session.commit()
        event.remove(sync_engine, "before_cursor_execute", handler)

        # --- ten thousand items ---
        handler = counted("large")
        event.listen(sync_engine, "before_cursor_execute", handler)
        started = time.perf_counter()
        async with session_factory() as big_session:
            outcome = await flag_actor(big_session, weaver.id, "large batch check", None)
            await big_session.commit()
        elapsed = time.perf_counter() - started
        event.remove(sync_engine, "before_cursor_execute", handler)

        assert outcome.items_affected == 10_000
        # A thousandfold more data must not mean a single extra round trip.
        assert counts["large"] == counts["small"], counts
        # Generous, because CI machines vary. A per-row loop would be minutes.
        assert elapsed < 30, f"flagging 10k items took {elapsed:.1f}s"

        async with session_factory() as check:
            disputed = await check.scalar(
                select(func.count())
                .select_from(Item)
                .where(
                    Item.registered_by == weaver.id,
                    Item.dispute_status == DisputeStatus.DISPUTED,
                )
            )
            assert disputed == 10_000

    async def test_the_flag_is_one_transaction(self, session_factory: Any) -> None:
        """A failure part-way through must leave nothing applied."""
        async with session_factory() as setup:
            weaver = await make_actor(setup, UserRole.WEAVER)
            category = await make_category(setup)
            for n in range(5):
                await seed_item(setup, weaver, category, quantity=f"{n + 1}.0000", enqueue=False)
            await setup.commit()
            weaver_id = weaver.id

        async with session_factory() as attempt:
            await flag_actor(attempt, weaver_id, "will be rolled back", None)
            await attempt.rollback()

        async with session_factory() as check:
            actor = await check.get(User, weaver_id)
            assert actor is not None
            # The flag, the disputes and the item statuses all went together.
            assert actor.fraud_flagged_at is None
            assert (await check.execute(select(ItemDispute))).first() is None
            statuses = (
                (
                    await check.execute(
                        select(Item.dispute_status).where(Item.registered_by == weaver_id)
                    )
                )
                .scalars()
                .all()
            )
            assert set(statuses) == {DisputeStatus.NONE}
