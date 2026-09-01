"""The endpoints, end to end, plus the anchoring path attestations share with items.

The anchoring test drives the real Phase 7 drain against the fake chain rather
than asserting an outbox row exists and calling it proved. An outbox row that
nothing can encode is a job that dead-letters in production and passes in CI.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.attestation.trust import TrustLevel
from app.config import get_settings
from app.core.crypto_shred import new_salt
from app.db.models.attestation import Attestation
from app.db.models.chain import ChainEvent, ChainTx
from app.db.models.enums import (
    DisputeStatus,
    ItemStatus,
    OutboxJobType,
    OutboxStatus,
    UserRole,
    UserStatus,
)
from app.db.models.outbox import Outbox
from app.db.models.user import User
from app.workers.jobs import drain_outbox, run_indexer, sweep_confirmations
from tests.fakes.chain_harness import build_harness, make_category, seed_item

pytestmark = [pytest.mark.integration, pytest.mark.chain]

API = get_settings().api_prefix
PASSWORD = "correct-horse-battery-staple"
CONFIRMATIONS = 3


async def make_actor(
    session: AsyncSession,
    role: UserRole,
    status: UserStatus = UserStatus.ACTIVE,
) -> tuple[User, str]:
    from app.auth.password import hash_password

    email = f"{role.lower()}-{uuid.uuid4().hex[:10]}@example.com"
    user = User(
        email=email,
        password_hash=hash_password(PASSWORD),
        display_name=f"Test {role}",
        role=role,
        status=status,
        identity_salt=new_salt(),
    )
    session.add(user)
    await session.commit()
    return user, email


async def token_for(client: httpx.AsyncClient, email: str) -> dict[str, str]:
    response = await client.post(
        f"{API}/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


class TestCreateAttestation:
    async def test_an_officer_can_attest_and_the_response_hides_the_person(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        weaver, _ = await make_actor(session, UserRole.WEAVER)
        officer, officer_email = await make_actor(session, UserRole.COOP_OFFICER)
        category = await make_category(session)
        item = await seed_item(session, weaver, category, enqueue=False)
        await session.commit()

        headers = await token_for(client, officer_email)
        response = await client.post(
            f"{API}/items/{item.id}/attestations",
            json={"statement": {"visited_on": "2026-08-01", "looms_seen": 4}},
            headers=headers,
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["attestor_role"] == UserRole.COOP_OFFICER
        assert body["statement_hash"].startswith("0x")
        assert len(body["statement_hash"]) == 66
        # A stable pseudonymous reference, and nothing that names anyone.
        assert body["attestor_ref"].startswith("0x")
        serialised = response.text
        assert officer_email not in serialised
        assert str(officer.id) not in serialised
        assert officer.display_name not in serialised

    async def test_a_second_attestation_by_the_same_actor_is_409(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        weaver, _ = await make_actor(session, UserRole.WEAVER)
        _, officer_email = await make_actor(session, UserRole.COOP_OFFICER)
        category = await make_category(session)
        item = await seed_item(session, weaver, category, enqueue=False)
        await session.commit()

        headers = await token_for(client, officer_email)
        body = {"statement": {"note": "visited"}}
        first = await client.post(f"{API}/items/{item.id}/attestations", json=body, headers=headers)
        assert first.status_code == 201

        second = await client.post(
            f"{API}/items/{item.id}/attestations",
            json={"statement": {"note": "visited again"}},
            headers=headers,
        )

        assert second.status_code == 409, second.text
        assert second.json()["error"]["code"] == "DUPLICATE_ATTESTATION"

    async def test_a_consumer_may_not_attest(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        weaver, _ = await make_actor(session, UserRole.WEAVER)
        _, consumer_email = await make_actor(session, UserRole.CONSUMER)
        category = await make_category(session)
        item = await seed_item(session, weaver, category, enqueue=False)
        await session.commit()

        response = await client.post(
            f"{API}/items/{item.id}/attestations",
            json={"statement": {"note": "looks nice"}},
            headers=await token_for(client, consumer_email),
        )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"

    async def test_a_fraud_flagged_actor_is_refused(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        from app.attestation.reputation import flag_actor

        weaver, _ = await make_actor(session, UserRole.WEAVER)
        officer, officer_email = await make_actor(session, UserRole.COOP_OFFICER)
        category = await make_category(session)
        item = await seed_item(session, weaver, category, enqueue=False)
        await session.commit()
        headers = await token_for(client, officer_email)

        await flag_actor(session, officer.id, "sign-offs without site visits", None)
        await session.commit()

        response = await client.post(
            f"{API}/items/{item.id}/attestations",
            json={"statement": {"note": "still fine"}},
            headers=headers,
        )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "ACTOR_FRAUD_FLAGGED"

    async def test_an_empty_statement_is_rejected(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        weaver, _ = await make_actor(session, UserRole.WEAVER)
        _, officer_email = await make_actor(session, UserRole.COOP_OFFICER)
        category = await make_category(session)
        item = await seed_item(session, weaver, category, enqueue=False)
        await session.commit()

        response = await client.post(
            f"{API}/items/{item.id}/attestations",
            json={"statement": {}},
            headers=await token_for(client, officer_email),
        )

        assert response.status_code == 422

    async def test_attesting_to_an_unknown_item_is_404(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _, officer_email = await make_actor(session, UserRole.COOP_OFFICER)

        response = await client.post(
            f"{API}/items/{uuid.uuid4()}/attestations",
            json={"statement": {"note": "x"}},
            headers=await token_for(client, officer_email),
        )

        assert response.status_code == 404


class TestListAndTrust:
    async def test_the_listing_pages_and_hides_identities(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        weaver, weaver_email = await make_actor(session, UserRole.WEAVER)
        category = await make_category(session)
        item = await seed_item(session, weaver, category, enqueue=False)
        await session.commit()

        from app.attestation import service

        actors = []
        for role in (UserRole.COOP_OFFICER, UserRole.INSPECTOR, UserRole.WEAVER):
            actor, _ = await make_actor(session, role)
            actors.append(actor)
            await service.create_attestation(session, item.id, {"note": str(role)}, actor)
        await session.commit()

        headers = await token_for(client, weaver_email)
        page = await client.get(
            f"{API}/items/{item.id}/attestations?limit=2", headers=headers
        )

        assert page.status_code == 200
        body = page.json()
        assert len(body["items"]) == 2
        assert body["next_cursor"] is not None
        for entry in body["items"]:
            assert "email" not in entry
            assert "display_name" not in entry
            assert entry["attestor_ref"].startswith("0x")

        second = await client.get(
            f"{API}/items/{item.id}/attestations?limit=2&cursor={body['next_cursor']}",
            headers=headers,
        )
        assert second.status_code == 200
        assert len(second.json()["items"]) == 1

    async def test_the_trust_endpoint_reports_evidence_not_a_verdict(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        from app.attestation import service

        weaver, weaver_email = await make_actor(session, UserRole.WEAVER)
        officer, _ = await make_actor(session, UserRole.COOP_OFFICER)
        category = await make_category(session)
        item = await seed_item(session, weaver, category, enqueue=False)
        await session.commit()
        await service.create_attestation(session, item.id, {"note": "visited"}, officer)
        await session.commit()

        response = await client.get(
            f"{API}/items/{item.id}/trust", headers=await token_for(client, weaver_email)
        )

        assert response.status_code == 200
        body = response.json()
        assert body["level"] == TrustLevel.CO_OP_ATTESTED
        assert body["contributing_roles"] == [UserRole.COOP_OFFICER]
        assert body["distinct_attestor_count"] == 1
        assert body["dispute_reason"] is None
        # No verdict anywhere in the payload.
        assert "verified" not in body
        assert "authentic" not in response.text.lower()

    async def test_trust_requires_authentication(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        weaver, _ = await make_actor(session, UserRole.WEAVER)
        category = await make_category(session)
        item = await seed_item(session, weaver, category, enqueue=False)
        await session.commit()

        assert (await client.get(f"{API}/items/{item.id}/trust")).status_code == 401


class TestAdminFraudEndpoints:
    async def test_only_an_admin_may_flag(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        weaver, _ = await make_actor(session, UserRole.WEAVER)
        _, officer_email = await make_actor(session, UserRole.COOP_OFFICER)

        response = await client.post(
            f"{API}/admin/actors/{weaver.id}/fraud-flag",
            json={"reason": "I do not like them"},
            headers=await token_for(client, officer_email),
        )

        assert response.status_code == 403

    async def test_flagging_flips_the_items_and_the_trust_level(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        weaver, weaver_email = await make_actor(session, UserRole.WEAVER)
        _, admin_email = await make_actor(session, UserRole.ADMIN)
        category = await make_category(session)
        item = await seed_item(session, weaver, category, enqueue=False)
        await session.commit()

        admin_headers = await token_for(client, admin_email)
        weaver_headers = await token_for(client, weaver_email)

        before = await client.get(f"{API}/items/{item.id}/trust", headers=weaver_headers)
        assert before.json()["level"] == TrustLevel.SELF_DECLARED

        flagged = await client.post(
            f"{API}/admin/actors/{weaver.id}/fraud-flag",
            json={"reason": "declared volumes exceed loom capacity"},
            headers=admin_headers,
        )
        assert flagged.status_code == 200, flagged.text
        assert flagged.json()["items_affected"] == 1

        after = await client.get(f"{API}/items/{item.id}/trust", headers=weaver_headers)
        assert after.json()["level"] == TrustLevel.DISPUTED
        assert "loom capacity" in after.json()["dispute_reason"]

        await session.refresh(item)
        assert item.dispute_status is DisputeStatus.DISPUTED

    async def test_clearing_restores_the_level(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        weaver, weaver_email = await make_actor(session, UserRole.WEAVER)
        _, admin_email = await make_actor(session, UserRole.ADMIN)
        category = await make_category(session)
        item = await seed_item(session, weaver, category, enqueue=False)
        await session.commit()

        admin_headers = await token_for(client, admin_email)
        await client.post(
            f"{API}/admin/actors/{weaver.id}/fraud-flag",
            json={"reason": "declared volumes exceed loom capacity"},
            headers=admin_headers,
        )
        cleared = await client.post(
            f"{API}/admin/actors/{weaver.id}/fraud-clear",
            json={"reason": "capacity records were incomplete, not wrong"},
            headers=admin_headers,
        )

        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["items_affected"] == 1

        trust = await client.get(
            f"{API}/items/{item.id}/trust", headers=await token_for(client, weaver_email)
        )
        assert trust.json()["level"] == TrustLevel.SELF_DECLARED

    async def test_a_short_reason_is_rejected(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        weaver, _ = await make_actor(session, UserRole.WEAVER)
        _, admin_email = await make_actor(session, UserRole.ADMIN)

        response = await client.post(
            f"{API}/admin/actors/{weaver.id}/fraud-flag",
            json={"reason": "bad"},
            headers=await token_for(client, admin_email),
        )

        # The reason is shown on every item the flag touches; "bad" is not one.
        assert response.status_code == 422


class TestAnchoringThroughPhaseSeven:
    async def test_an_attestation_enqueues_exactly_one_job_and_anchors(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        from app.attestation import service

        harness = build_harness(session_factory, chain_confirmations=CONFIRMATIONS)
        weaver = (await make_actor(session, UserRole.WEAVER))[0]
        officer = (await make_actor(session, UserRole.COOP_OFFICER))[0]
        category = await make_category(session)
        item = await seed_item(session, weaver, category, enqueue=False)
        await session.commit()

        attestation = await service.create_attestation(
            session, item.id, {"visited_on": "2026-08-01"}, officer
        )
        await session.commit()

        jobs = (await session.execute(select(Outbox))).scalars().all()
        assert len(jobs) == 1
        assert jobs[0].job_type is OutboxJobType.ANCHOR_ATTESTATION
        assert jobs[0].dedupe_key == attestation.statement_hash

        # The real Phase 7 drain, not a stand-in: the same claim, nonce,
        # writer, receipt polling and confirmation depth an item anchor uses.
        assert await drain_outbox(harness.runtime) == 1
        harness.chain.mine(CONFIRMATIONS + 1)
        await sweep_confirmations(harness.runtime)
        await run_indexer(harness.runtime)

        transaction = (await session.execute(select(ChainTx))).scalar_one()
        await session.refresh(transaction)
        assert transaction.tx_hash is not None

        await session.refresh(jobs[0])
        assert jobs[0].status is OutboxStatus.DONE

        # The statement hash really is the value on chain.
        assert attestation.statement_hash in harness.chain.item_anchors

        event = (await session.execute(select(ChainEvent))).scalar_one()
        assert event.subject_hash == attestation.statement_hash

    async def test_anchoring_an_attestation_leaves_the_item_status_alone(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        from app.attestation import service

        harness = build_harness(session_factory, chain_confirmations=CONFIRMATIONS)
        weaver = (await make_actor(session, UserRole.WEAVER))[0]
        officer = (await make_actor(session, UserRole.COOP_OFFICER))[0]
        category = await make_category(session)
        item = await seed_item(session, weaver, category, enqueue=False)
        await session.commit()
        await service.create_attestation(session, item.id, {"note": "visited"}, officer)
        await session.commit()

        await drain_outbox(harness.runtime)
        harness.chain.mine(CONFIRMATIONS + 1)
        await sweep_confirmations(harness.runtime)

        await session.refresh(item)
        # The item's own hash was never anchored here, so it stays PENDING.
        # An attestation confirming says nothing about the item's own anchor.
        assert item.status is ItemStatus.PENDING

    async def test_reconcile_does_not_report_anchored_attestations_as_drift(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        from app.attestation import service
        from app.chain.reconcile import reconcile

        harness = build_harness(session_factory, chain_confirmations=CONFIRMATIONS)
        weaver = (await make_actor(session, UserRole.WEAVER))[0]
        officer = (await make_actor(session, UserRole.COOP_OFFICER))[0]
        category = await make_category(session)
        item = await seed_item(session, weaver, category)
        await session.commit()
        await service.create_attestation(session, item.id, {"note": "visited"}, officer)
        await session.commit()

        await drain_outbox(harness.runtime)
        harness.chain.mine(CONFIRMATIONS + 1)
        await sweep_confirmations(harness.runtime)
        await run_indexer(harness.runtime)

        report = await reconcile(session_factory, harness.client, harness.settings)

        # Item hashes and statement hashes reach the chain through the same
        # function. Checking only `items` would call every attestation drift.
        assert report.clean, [drift.as_dict() for drift in report.drifts]

    async def test_an_attestation_is_recorded_even_with_writes_disabled(
        self, session: AsyncSession, session_factory: Any
    ) -> None:
        from app.attestation import service

        harness = build_harness(session_factory, chain_write_enabled=False)
        weaver = (await make_actor(session, UserRole.WEAVER))[0]
        officer = (await make_actor(session, UserRole.COOP_OFFICER))[0]
        category = await make_category(session)
        item = await seed_item(session, weaver, category, enqueue=False)
        await session.commit()
        await service.create_attestation(session, item.id, {"note": "visited"}, officer)
        await session.commit()

        await drain_outbox(harness.runtime)

        assert (await session.execute(select(ChainTx))).first() is None
        job = (await session.execute(select(Outbox))).scalar_one()
        await session.refresh(job)
        assert job.status is OutboxStatus.QUEUED
        assert job.attempts == 0
        # The attestation itself is recorded regardless; the chain is a
        # dependency of anchoring, not of vouching.
        assert (await session.execute(select(Attestation))).scalar_one() is not None
