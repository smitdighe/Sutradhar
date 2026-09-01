"""The trust ladder, enumerated over every attestor combination that matters.

The matrix is the point. A trust level is the one number a consumer actually
reads, and the ways to get it wrong are all quiet: counting the registrant's own
attestation, counting the same person twice, letting an unverified account claim
authority, letting a flagged officer be outvoted. None of those raise an
exception. Each of them makes the number say more than the evidence supports.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.attestation import service
from app.attestation.reputation import flag_actor, raise_dispute
from app.attestation.trust import TrustLevel, assess, assess_many
from app.core.crypto_shred import new_salt
from app.db.models.attestation import Attestation
from app.db.models.catalog import Item
from app.db.models.enums import DisputeSource, UserRole, UserStatus
from app.db.models.user import User
from tests.fakes.chain_harness import make_category, seed_item

pytestmark = [pytest.mark.integration, pytest.mark.chain]

PASSWORD = "correct-horse-battery-staple"


async def make_actor(
    session: AsyncSession,
    role: UserRole,
    status: UserStatus = UserStatus.ACTIVE,
    fraud_flagged: bool = False,
) -> User:
    from app.auth.password import hash_password
    from app.core.clock import now

    user = User(
        email=f"{role.lower()}-{uuid.uuid4().hex[:10]}@example.com",
        password_hash=hash_password(PASSWORD),
        display_name=f"Test {role}",
        role=role,
        status=status,
        identity_salt=new_salt(),
        fraud_flagged_at=now() if fraud_flagged else None,
    )
    session.add(user)
    await session.flush()
    return user


async def attest(
    session: AsyncSession, item: Item, actor: User, note: str = "seen at the loom"
) -> Attestation:
    return await service.create_attestation(session, item.id, {"note": note}, actor)


@pytest.fixture
async def fixture_item(session: AsyncSession) -> tuple[Item, User]:
    """One PENDING item and the weaver who registered it."""
    weaver = await make_actor(session, UserRole.WEAVER)
    category = await make_category(session)
    item = await seed_item(session, weaver, category, enqueue=False)
    await session.commit()
    return item, weaver


class TestTrustMatrix:
    """Every combination of attestors, and the level each one must produce."""

    async def test_no_attestations_is_self_declared(
        self, session: AsyncSession, fixture_item: Any
    ) -> None:
        item, _ = fixture_item

        assessment = await assess(session, item)

        # The honest floor. A registration is a claim, and nothing more has
        # been said about it by anyone.
        assert assessment.level is TrustLevel.SELF_DECLARED
        assert assessment.attestation_count == 0
        assert assessment.contributing_roles == ()

    async def test_the_registrant_attesting_to_their_own_item_raises_nothing(
        self, session: AsyncSession, fixture_item: Any
    ) -> None:
        item, weaver = fixture_item
        await attest(session, item, weaver)
        await session.commit()

        assessment = await assess(session, item)

        # A person vouching for their own work is exactly the claim already
        # being made. It is recorded, and it moves nothing.
        assert assessment.level is TrustLevel.SELF_DECLARED
        assert assessment.attestation_count == 1
        assert assessment.distinct_attestor_count == 1

    async def test_another_weaver_does_not_raise_the_level(
        self, session: AsyncSession, fixture_item: Any
    ) -> None:
        item, _ = fixture_item
        peer = await make_actor(session, UserRole.WEAVER)
        await attest(session, item, peer)
        await session.commit()

        assessment = await assess(session, item)

        # Peer endorsement between weavers is not the independent check that a
        # co-op or an inspector represents.
        assert assessment.level is TrustLevel.SELF_DECLARED
        assert assessment.attestation_count == 1

    async def test_an_independent_coop_officer_raises_to_co_op_attested(
        self, session: AsyncSession, fixture_item: Any
    ) -> None:
        item, _ = fixture_item
        officer = await make_actor(session, UserRole.COOP_OFFICER)
        await attest(session, item, officer)
        await session.commit()

        assessment = await assess(session, item)

        assert assessment.level is TrustLevel.CO_OP_ATTESTED
        assert assessment.contributing_roles == (UserRole.COOP_OFFICER,)

    async def test_an_independent_inspector_raises_to_inspected(
        self, session: AsyncSession, fixture_item: Any
    ) -> None:
        item, _ = fixture_item
        inspector = await make_actor(session, UserRole.INSPECTOR)
        await attest(session, item, inspector)
        await session.commit()

        assessment = await assess(session, item)

        assert assessment.level is TrustLevel.INSPECTED
        assert assessment.contributing_roles == (UserRole.INSPECTOR,)

    async def test_both_roles_reach_inspected_and_report_both(
        self, session: AsyncSession, fixture_item: Any
    ) -> None:
        item, weaver = fixture_item
        await attest(session, item, weaver)
        await attest(session, item, await make_actor(session, UserRole.COOP_OFFICER))
        await attest(session, item, await make_actor(session, UserRole.INSPECTOR))
        await session.commit()

        assessment = await assess(session, item)

        assert assessment.level is TrustLevel.INSPECTED
        # Both are reported, so a reader can see the record cleared two
        # independent checks rather than only the higher one.
        assert assessment.contributing_roles == (UserRole.COOP_OFFICER, UserRole.INSPECTOR)
        assert assessment.attestation_count == 3
        assert assessment.distinct_attestor_count == 3

    async def test_an_inspector_alone_outranks_a_coop_officer_alone(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        weaver = await make_actor(session, UserRole.WEAVER)
        category = await make_category(session)
        first = await seed_item(session, weaver, category, quantity="1.0000", enqueue=False)
        second = await seed_item(session, weaver, category, quantity="2.0000", enqueue=False)
        await session.commit()

        await attest(session, first, await make_actor(session, UserRole.COOP_OFFICER))
        await attest(session, second, await make_actor(session, UserRole.INSPECTOR))
        await session.commit()

        levels = await assess_many(session, [first, second])

        assert levels[first.id].level is TrustLevel.CO_OP_ATTESTED
        assert levels[second.id].level is TrustLevel.INSPECTED

    async def test_a_registrant_who_is_a_coop_officer_does_not_raise_their_own_item(
        self, session: AsyncSession
    ) -> None:
        officer = await make_actor(session, UserRole.COOP_OFFICER)
        category = await make_category(session)
        item = await seed_item(session, officer, category, enqueue=False)
        await session.commit()

        await attest(session, item, officer)
        await session.commit()

        assessment = await assess(session, item)

        # Independence is about the person, not the badge. An officer signing
        # off on their own registration corroborates nothing.
        assert assessment.level is TrustLevel.SELF_DECLARED

    async def test_a_pending_coop_officer_cannot_raise_the_level(
        self, session: AsyncSession, fixture_item: Any
    ) -> None:
        item, _ = fixture_item
        unverified = await make_actor(
            session, UserRole.COOP_OFFICER, status=UserStatus.PENDING_VERIFICATION
        )
        await attest(session, item, unverified)
        await session.commit()

        assessment = await assess(session, item)

        # Otherwise the ladder is self-service: register, claim COOP_OFFICER,
        # attest, look corroborated.
        assert assessment.level is TrustLevel.SELF_DECLARED
        assert assessment.attestation_count == 1

    async def test_a_pending_weaver_may_still_self_declare(
        self, session: AsyncSession
    ) -> None:
        weaver = await make_actor(
            session, UserRole.WEAVER, status=UserStatus.PENDING_VERIFICATION
        )
        category = await make_category(session)
        item = await seed_item(session, weaver, category, enqueue=False)
        await session.commit()

        await attest(session, item, weaver)
        await session.commit()

        assessment = await assess(session, item)

        # Refusing this would leave a new weaver unable to record their own
        # work, which is exactly what SELF_DECLARED is for.
        assert assessment.level is TrustLevel.SELF_DECLARED
        assert assessment.attestation_count == 1


class TestIndependence:
    async def test_a_second_attestation_by_the_same_actor_is_refused(
        self, session: AsyncSession, fixture_item: Any
    ) -> None:
        from app.core.errors import ConflictError, ErrorCode

        item, _ = fixture_item
        officer = await make_actor(session, UserRole.COOP_OFFICER)
        await attest(session, item, officer)
        await session.commit()

        with pytest.raises(ConflictError) as caught:
            await attest(session, item, officer, note="on reflection, still fine")

        # From the database constraint, not from an application check: a
        # check-then-write has a gap and two concurrent requests both pass it.
        assert caught.value.code is ErrorCode.DUPLICATE_ATTESTATION
        assert caught.value.status == 409

    async def test_two_distinct_officers_still_read_as_one_step_on_the_ladder(
        self, session: AsyncSession, fixture_item: Any
    ) -> None:
        item, _ = fixture_item
        await attest(session, item, await make_actor(session, UserRole.COOP_OFFICER))
        await attest(session, item, await make_actor(session, UserRole.COOP_OFFICER))
        await session.commit()

        assessment = await assess(session, item)

        assert assessment.level is TrustLevel.CO_OP_ATTESTED
        # The count is surfaced separately, so "two officers agreed" is visible
        # without inventing a rung on the ladder for it.
        assert assessment.distinct_attestor_count == 2


class TestDisputeOverride:
    async def test_a_flagged_attestor_disputes_the_whole_record(
        self, session: AsyncSession, fixture_item: Any
    ) -> None:
        item, _ = fixture_item
        officer = await make_actor(session, UserRole.COOP_OFFICER)
        inspector = await make_actor(session, UserRole.INSPECTOR)
        await attest(session, item, officer)
        await attest(session, item, inspector)
        await session.commit()
        assert (await assess(session, item)).level is TrustLevel.INSPECTED

        await flag_actor(session, officer.id, "signing off without visiting", None)
        await session.commit()

        assessment = await assess(session, item)

        # Not averaged away. A compromised officer must not be outvoted into
        # looking fine -- that is precisely when a reader needs telling.
        assert assessment.level is TrustLevel.DISPUTED
        assert assessment.flagged_attestor_count == 1
        assert assessment.contributing_roles == ()

    async def test_an_explicit_dispute_overrides_a_fully_corroborated_record(
        self, session: AsyncSession, fixture_item: Any
    ) -> None:
        item, _ = fixture_item
        await attest(session, item, await make_actor(session, UserRole.INSPECTOR))
        await session.commit()
        assert (await assess(session, item)).level is TrustLevel.INSPECTED

        await raise_dispute(
            session,
            item.id,
            DisputeSource.INSPECTION,
            "fibre analysis inconsistent with the declared weave",
            None,
        )
        await session.commit()

        assessment = await assess(session, item)

        assert assessment.level is TrustLevel.DISPUTED
        assert assessment.dispute_reason is not None
        assert "fibre analysis" in assessment.dispute_reason

    async def test_the_level_recovers_when_the_flag_is_lifted(
        self, session: AsyncSession, fixture_item: Any
    ) -> None:
        from app.attestation.reputation import clear_fraud_flag

        item, _ = fixture_item
        officer = await make_actor(session, UserRole.COOP_OFFICER)
        await attest(session, item, officer)
        await session.commit()

        await flag_actor(session, officer.id, "under investigation", None)
        await session.commit()
        assert (await assess(session, item)).level is TrustLevel.DISPUTED

        await clear_fraud_flag(session, officer.id, None, "investigation closed")
        await session.commit()

        # Derived, so the recovery needs no backfill either.
        assert (await assess(session, item)).level is TrustLevel.CO_OP_ATTESTED


class TestBatchAssessment:
    async def test_many_items_are_assessed_together(
        self, session: AsyncSession
    ) -> None:
        weaver = await make_actor(session, UserRole.WEAVER)
        category = await make_category(session)
        items = [
            await seed_item(session, weaver, category, quantity=f"{n + 1}.0000", enqueue=False)
            for n in range(5)
        ]
        await session.commit()

        officer = await make_actor(session, UserRole.COOP_OFFICER)
        await attest(session, items[0], officer)
        await attest(session, items[1], await make_actor(session, UserRole.INSPECTOR))
        await session.commit()

        levels = await assess_many(session, items)

        assert levels[items[0].id].level is TrustLevel.CO_OP_ATTESTED
        assert levels[items[1].id].level is TrustLevel.INSPECTED
        for item in items[2:]:
            assert levels[item.id].level is TrustLevel.SELF_DECLARED

    async def test_an_empty_list_assesses_to_nothing(self, session: AsyncSession) -> None:
        assert await assess_many(session, []) == {}


class TestTrustIsNotStored:
    async def test_the_item_table_has_no_trust_column(self) -> None:
        columns = {column.name for column in Item.__table__.columns}

        assert not any("trust" in name for name in columns)

    async def test_the_level_changes_with_no_write_to_the_item(
        self, session: AsyncSession, fixture_item: Any
    ) -> None:
        item, _ = fixture_item
        before = (
            await session.execute(select(Item.updated_at).where(Item.id == item.id))
        ).scalar_one()

        await attest(session, item, await make_actor(session, UserRole.INSPECTOR))
        await session.commit()

        after = (
            await session.execute(select(Item.updated_at).where(Item.id == item.id))
        ).scalar_one()

        assert (await assess(session, item)).level is TrustLevel.INSPECTED
        # The level moved and the item row did not. There is nothing to go
        # stale, because there is nothing stored.
        assert before == after
