"""How long a scan waits when the instance was asleep.

Render's free tier spins an idle service down. The next request pays for process
start, imports, the lifespan -- which probes the chain and takes an advisory
lock -- and the first database connection, and that whole cost lands on whoever
scanned the tag.

**This is measured and recorded, not asserted against a threshold.** There is no
number this code could pick that would be right on both a laptop and a free
instance, and a load test that fails on somebody's slow morning teaches people to
skip load tests. The figure goes in the demo checklist, where it answers "how
long do I hold the phone up before it works".

The one thing that *is* asserted is the shape: the public read must not be
gated on a dependency that is not there. If ``/v/{code}`` waited for a chain
that will never answer, the cold start would be an RPC timeout rather than a
boot -- and that is a bug, not a measurement.
"""

from __future__ import annotations

import time
import uuid
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.crypto_shred import new_salt
from app.core.ids import new_tag_code
from app.db.models.catalog import GICategory, Item
from app.db.models.enums import ItemStatus, UserRole, UserStatus
from app.db.models.user import User
from tests.load.conftest import announce, free_port, start_server, stop_server

pytestmark = pytest.mark.load

# Not a pass/fail line -- a shape check. Anything past this is not a slow laptop,
# it is something in the boot path waiting on a dependency that is not coming.
SANITY_CEILING_SECONDS = 45.0


@pytest_asyncio.fixture
async def one_tag(load_engine: object) -> str:
    """A single tagged item, so the cold read has something real to answer."""
    sessions = async_sessionmaker(
        bind=load_engine,  # type: ignore[arg-type]
        class_=AsyncSession,
        expire_on_commit=False,
    )
    code = new_tag_code()
    async with sessions() as session:
        weaver = User(
            email=f"cold-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="x",
            display_name="Cold Start Weaver",
            role=UserRole.WEAVER,
            status=UserStatus.ACTIVE,
            identity_salt=new_salt(),
        )
        category = GICategory(
            slug=f"cold-cloth-{uuid.uuid4().hex[:6]}",
            display_name="Cold Start Cloth",
            is_textile=True,
            attribute_schema={"type": "object", "additionalProperties": True},
            schema_version=1,
            quantity_unit="metre",
            is_active=True,
        )
        session.add_all([weaver, category])
        await session.flush()
        session.add(
            Item(
                category_id=category.id,
                category_schema_version=1,
                registered_by=weaver.id,
                attributes={"warp_count": 120},
                quantity=Decimal("5.0000"),
                quantity_unit="metre",
                item_hash=f"0x{uuid.uuid4().hex}{uuid.uuid4().hex}",
                tag_code=code,
                status=ItemStatus.PENDING,
            )
        )
        await session.commit()
    return code


class TestColdStart:
    def test_process_start_to_first_public_answer(self, one_tag: str) -> None:
        """Spawn, wait for the real endpoint, and report the number in ms.

        The probe is ``/v/{code}`` and not ``/healthz`` deliberately. Liveness
        answers before the database pool has opened a connection, so timing
        against it would report a number nobody experiences.
        """
        port = free_port()
        started = time.perf_counter()
        server = start_server(port, probe_path=f"/v/{one_tag}")
        try:
            first_answer = time.perf_counter() - started

            with httpx.Client(base_url=server.base_url, timeout=10.0) as client:
                confirmation = client.get(f"/v/{one_tag}")
                warm_started = time.perf_counter()
                warm = client.get(f"/v/{one_tag}")
                warm_seconds = time.perf_counter() - warm_started

            assert confirmation.status_code == 200, confirmation.text
            assert warm.status_code == 200, warm.text

            announce(
                [
                    "\nCOLD START -- process spawn to first 200 from /v/{tag_code}",
                    f"  cold      {first_answer * 1000:8.0f} ms",
                    f"  warm      {warm_seconds * 1000:8.1f} ms",
                    f"  ratio     {first_answer / max(warm_seconds, 1e-6):8.0f}x",
                    "  (this is the Render free-tier answer; put it in the "
                    "demo checklist)",
                ]
            )

            assert first_answer < SANITY_CEILING_SECONDS, (
                f"cold start took {first_answer:.1f}s. That is past anything a "
                "slow machine explains, and the usual cause is the boot path "
                "blocking on a dependency that is not there."
            )
        finally:
            stop_server(server)

    def test_the_boot_does_not_wait_for_a_chain_that_is_not_there(
        self, one_tag: str
    ) -> None:
        """Nothing is deployed and no node is listening. Boot must not care.

        The lifespan probes the RPC endpoint on the way up. If that probe
        retried with backoff, a demo on a free tier would spend its first minute
        unreachable every single time -- and the item would still be honestly
        PENDING at the end of it.
        """
        port = free_port()
        started = time.perf_counter()
        server = start_server(port, probe_path=f"/v/{one_tag}")
        try:
            elapsed = time.perf_counter() - started
            with httpx.Client(base_url=server.base_url, timeout=10.0) as client:
                ready = client.get("/readyz")
                public = client.get(f"/v/{one_tag}")

            # Postgres is up, so readiness is 200 whatever the chain is doing.
            assert ready.status_code == 200, ready.text
            assert ready.json()["checks"]["chain_rpc"]["status"] in {"down", "degraded"}
            assert public.status_code == 200, public.text
            assert public.json()["chain"]["verification"] == "UNANCHORED"

            announce(
                [
                    f"\n  boot with no chain reachable: {elapsed * 1000:.0f} ms "
                    "(public read still 200, UNANCHORED)"
                ]
            )
        finally:
            stop_server(server)
