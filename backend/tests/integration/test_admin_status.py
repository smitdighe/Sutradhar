"""The one screen an operator checks before presenting.

Three properties, and the third is the one that matters most.

**It tells the truth about the chain.** ``chain_mode`` is ``postgres_only``
today and must say so. A status page that showed a green tick for a system with
nothing deployed would be worse than no status page, because somebody would
believe it on stage.

**It never 500s.** It is opened precisely when something is broken, so an
unreachable node is reported as a null with a reason rather than as an
exception. A status endpoint that fails when a dependency fails is never there
when it is needed.

**It is admin-only.** Queue depths and quota burn describe the operator's
infrastructure, not anybody's textile.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.chain import IndexerCheckpoint
from app.db.models.enums import OutboxJobType, UserRole
from app.db.models.ops import DeadLetter, QuotaUsage
from tests.integration.helpers import (
    API,
    auth_headers,
    load_catalogue,
    make_user,
    register_item,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

STATUS = f"{API}/admin/system/status"


@pytest.fixture
async def admin_headers(client: httpx.AsyncClient, session: AsyncSession) -> dict[str, str]:
    admin = await make_user(session, UserRole.ADMIN, prefix="status")
    return await auth_headers(client, admin)


class TestChainMode:
    async def test_it_says_postgres_only_in_the_current_configuration(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        """Nothing is deployed, writes are off, there is no signer.

        Every one of those alone is enough. The mode is a conjunction, not a
        vote, because a signer with no contract can no more anchor than a
        contract with no signer.
        """
        response = await client.get(STATUS, headers=admin_headers)

        assert response.status_code == 200, response.text
        chain = response.json()["chain"]
        assert chain["mode"] == "postgres_only", chain
        assert chain["contract_deployed"] is False
        assert chain["write_enabled"] is False
        assert chain["signer_configured"] is False
        assert chain["contract_address"] == "0x" + "0" * 40

    async def test_the_mode_is_only_two_values(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        """No third state. An operator reads this in five seconds under stress."""
        response = await client.get(STATUS, headers=admin_headers)
        assert response.json()["chain"]["mode"] in {"live", "postgres_only"}


class TestQueueDepth:
    async def test_the_outbox_is_reported_by_job_type_and_state(
        self, client: httpx.AsyncClient, session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        """Grouped, not totalled.

        "Eleven queued" and "eleven dead" are opposite situations, and a single
        depth number cannot tell them apart.
        """
        await load_catalogue(session)
        weaver = await make_user(session, UserRole.WEAVER, prefix="status")
        weaver_headers = await auth_headers(client, weaver)
        for _ in range(3):
            await register_item(client, weaver_headers)

        response = await client.get(STATUS, headers=admin_headers)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["outbox_total"] == 3
        rows = {
            (row["job_type"], row["status"]): row["count"] for row in body["outbox"]
        }
        assert rows == {(str(OutboxJobType.ANCHOR_ITEM), "QUEUED"): 3}, rows

    async def test_dead_letters_are_counted_and_split_by_resolution(
        self, client: httpx.AsyncClient, session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        session.add_all(
            [
                DeadLetter(
                    source="chain_outbox",
                    original_payload={"job_id": str(uuid.uuid4())},
                    error_chain="attempt 1: the node was unreachable",
                    attempts=6,
                ),
                DeadLetter(
                    source="chain_outbox",
                    original_payload={"job_id": str(uuid.uuid4())},
                    error_chain="attempt 1: the node was unreachable",
                    attempts=6,
                    resolved_at=datetime.now(UTC),
                ),
            ]
        )
        await session.commit()

        body = (await client.get(STATUS, headers=admin_headers)).json()

        assert body["dead_letters"] == 2
        assert body["dead_letters_unresolved"] == 1, (
            "a resolved dead letter still counted as something needing attention"
        )


class TestIndexerLag:
    async def test_an_unreachable_node_reports_null_lag_and_a_reason(
        self, client: httpx.AsyncClient, session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        """Null, never zero.

        Zero means caught up. Reporting an unreachable node as caught up is the
        single most misleading thing this endpoint could do, because it is the
        answer that stops somebody looking.
        """
        session.add(IndexerCheckpoint(name="sutradhar_anchors", last_block=1234))
        await session.commit()

        indexer = (await client.get(STATUS, headers=admin_headers)).json()["indexer"]

        assert indexer["checkpoint_block"] == 1234
        assert indexer["lag_blocks"] is None
        assert indexer["head_block"] is None
        assert indexer["detail"], "the lag was unknown and nothing said why"

    async def test_no_checkpoint_reads_as_block_zero(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        """Nothing indexed yet is a real state, not a missing one."""
        indexer = (await client.get(STATUS, headers=admin_headers)).json()["indexer"]
        assert indexer["checkpoint_block"] == 0


class TestQuotas:
    async def test_usage_is_reported_with_a_percentage(
        self, client: httpx.AsyncClient, session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        """The question asked at a glance is "am I close", not "what is the ratio"."""
        session.add(
            QuotaUsage(
                name="alchemy_cu",
                period_start=datetime(2026, 8, 1, tzinfo=UTC),
                used=Decimal(75),
                budget=Decimal(100),
            )
        )
        await session.commit()

        quotas = (await client.get(STATUS, headers=admin_headers)).json()["quotas"]

        alchemy = next(row for row in quotas if row["name"] == "alchemy_cu")
        assert alchemy["used_percent"] == 75.0
        # `numeric(28, 4)` comes back from Postgres carrying its scale, and the
        # serialiser stringifies the Decimal it was handed rather than trimming
        # it. Four places is what the column stores and what the wire carries.
        assert alchemy["used"] == "75.0000"
        assert alchemy["budget"] == "100.0000"

    async def test_a_zero_budget_does_not_divide_by_zero(
        self, client: httpx.AsyncClient, session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        """A misconfigured budget is a bad number, not a crashed status page."""
        session.add(
            QuotaUsage(
                name="misconfigured",
                period_start=datetime(2026, 8, 1, tzinfo=UTC),
                used=Decimal(5),
                budget=Decimal(0),
            )
        )
        await session.commit()

        response = await client.get(STATUS, headers=admin_headers)

        assert response.status_code == 200, response.text
        row = next(
            item
            for item in response.json()["quotas"]
            if item["name"] == "misconfigured"
        )
        assert row["used_percent"] == 0.0


class TestScheduler:
    async def test_it_reports_the_scheduler_as_not_running_under_test(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        """Lifespan does not run in these tests, so no jobs are registered.

        Reported honestly rather than inferred from the setting: ``enabled`` is
        configuration and ``running`` is fact, and the interesting failure is
        exactly when they disagree.
        """
        body = (await client.get(STATUS, headers=admin_headers)).json()

        assert body["scheduler_enabled"] is True
        assert body["scheduler_running"] is False
        assert body["jobs"] == []


class TestItNeverFails:
    async def test_an_empty_database_still_answers(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        """No outbox rows, no quotas, no checkpoint, no chain. Still a 200."""
        response = await client.get(STATUS, headers=admin_headers)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["outbox"] == []
        assert body["outbox_total"] == 0
        assert body["dead_letters"] == 0
        assert body["observed_at"]
        assert body["app_env"]

    async def test_no_secret_reaches_the_payload(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        """It names the contract address, which is public. Nothing else is.

        An admin surface is still a surface, and the relayer key is the one
        value on this page's subject matter that would be catastrophic to show.
        """
        from app.config import get_settings

        settings = get_settings()
        raw = (await client.get(STATUS, headers=admin_headers)).text

        for name, value in (
            ("PENDING_TOKEN_SECRET", settings.pending_token_secret),
            ("CURSOR_SECRET", settings.cursor_secret),
        ):
            assert value not in raw, f"{name} appeared in the status payload"
        assert "PRIVATE KEY" not in raw


class TestAccess:
    @pytest.mark.parametrize(
        "role", [UserRole.WEAVER, UserRole.COOP_OFFICER, UserRole.INSPECTOR, UserRole.CONSUMER]
    )
    async def test_every_non_admin_role_is_refused(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        role: UserRole,
    ) -> None:
        """Including the co-op officer, who is admitted to bulk tagging.

        Being trusted with one operator task is not being trusted with the
        infrastructure page.
        """
        actor = await make_user(session, role, prefix="status")
        headers = await auth_headers(client, actor)

        response = await client.get(STATUS, headers=headers)

        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"

    async def test_no_token_is_a_401(self, client: httpx.AsyncClient) -> None:
        response = await client.get(STATUS)
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"
