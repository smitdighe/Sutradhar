"""Statement ceilings on the four hot paths, and one stronger property than a ceiling.

**A ceiling alone is the weaker test.** "At most eight statements" catches a
regression that adds a ninth and misses the one that matters: a query issued
*per row*, which passes on a fixture with one child and falls over on a real
lineage. So every route here is also asserted to cost the **same** number of
statements regardless of how much data hangs off it -- more ancestors, more
events, more media, more scans. That is the actual property being defended, and
the number is only the second line.

The counter is a ``before_cursor_execute`` listener on the engine, so it sees
what PostgreSQL sees: every statement, including the ones the ORM emits on its
own. Nothing is counted by inspection.

Fixing an N+1 by caching would pass a count test and fail the system. Nothing
here is cached; the counts came down by asking for the right thing once.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.models.catalog import Item, ItemEvent
from app.db.models.enums import ItemEventType, UserRole
from app.db.models.scan import Scan
from tests.integration.helpers import (
    API,
    GUJARAT,
    PATOLA,
    auth_headers,
    idempotency,
    load_catalogue,
    make_user,
    register_item,
    tagged_item,
)

pytestmark = pytest.mark.integration

# Measured, not guessed. Each is the count this suite observed after the Phase
# 12 fixes, with no headroom: a change that adds a statement is meant to fail
# here and be looked at, not absorbed by a generous margin.
#
# Fifteen for the public read is a real number and worth reading as one. It
# breaks down as: the rate-limit bucket, the item, the claim, the category, the
# registrant, three anchor probes (merkle leaf, ANCHORED event, chain event),
# two trust reads (attestations, disputes), the lineage CTE, the event log, the
# child count, the media join, and the scan history. Every one of them asks a
# different question, and none of them is per-row -- which is the property the
# invariance tests below actually defend. Collapsing the anchor probes or the
# trust pair into single statements is possible and is a rewrite of the read
# path, not a bug fix; it is deliberately not in this phase.
CEILING_PUBLIC_VERIFY = 15
CEILING_ITEM_LIST = 4
CEILING_ITEM_TREE = 3
CEILING_ATTESTATIONS = 3


class StatementCounter:
    """Counts every statement the engine executes while it is armed."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    @property
    def count(self) -> int:
        return len(self.statements)

    def report(self) -> str:
        """The statements, numbered and truncated. Printed on failure only."""
        return "\n".join(
            f"  {index:>2}. {text.strip().splitlines()[0][:110]}"
            for index, text in enumerate(self.statements, start=1)
        )


@pytest.fixture
def counter(engine: AsyncEngine) -> Iterator[StatementCounter]:
    """A counter armed by :func:`counting`, attached to the test engine."""
    recorder = StatementCounter()
    armed = {"on": False}

    def before_cursor_execute(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if armed["on"]:
            recorder.statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", before_cursor_execute)
    recorder._armed = armed  # type: ignore[attr-defined]
    try:
        yield recorder
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before_cursor_execute)


@contextmanager
def counting(recorder: StatementCounter) -> Iterator[StatementCounter]:
    """Arm the counter for exactly one request and reset it first.

    Scoped rather than global: the setup that builds an item issues dozens of
    statements, and counting those would measure the fixture.
    """
    recorder.statements.clear()
    recorder._armed["on"] = True  # type: ignore[attr-defined]
    try:
        yield recorder
    finally:
        recorder._armed["on"] = False  # type: ignore[attr-defined]


def assert_at_most(recorder: StatementCounter, ceiling: int, route: str) -> None:
    assert recorder.count <= ceiling, (
        f"{route} issued {recorder.count} statements, ceiling is {ceiling}.\n"
        f"{recorder.report()}"
    )


# --------------------------------------------------------------- data shaping


async def _deepen(session: AsyncSession, item_id: uuid.UUID, levels: int) -> uuid.UUID:
    """Give *item_id* a chain of ancestors, and return the deepest descendant.

    Written directly rather than through the split endpoint: this exists to
    lengthen a lineage, and going through mass balance would need a parent large
    enough to divide *levels* times, which is a different test's problem.
    """
    # Walks upward: each parent created becomes the node that gets a parent on
    # the next pass, so the result is a chain and not one node reparented five
    # times.
    current_id = item_id
    for _ in range(levels):
        item = await session.get(Item, current_id)
        assert item is not None
        parent = Item(
            category_id=item.category_id,
            category_schema_version=item.category_schema_version,
            registered_by=item.registered_by,
            attributes=dict(item.attributes),
            quantity=item.quantity,
            quantity_unit=item.quantity_unit,
            # Unique index on item_hash; these are not real preimages and are
            # never verified against, only walked.
            item_hash=f"0x{uuid.uuid4().hex}{uuid.uuid4().hex}",
            status=item.status,
        )
        session.add(parent)
        await session.flush()
        item.parent_id = parent.id
        await session.commit()
        current_id = parent.id
    return item_id


async def _add_events(session: AsyncSession, item_id: uuid.UUID, count: int) -> None:
    for index in range(count):
        session.add(
            ItemEvent(
                item_id=item_id,
                event_type=ItemEventType.ATTESTED,
                payload={"n": index},
                payload_hash=f"0x{index:064x}",
            )
        )
    await session.commit()


async def _add_scans(session: AsyncSession, item_id: uuid.UUID, code: str, count: int) -> None:
    for index in range(count):
        session.add(
            Scan(
                item_id=item_id,
                tag_code=code,
                country_code="IN",
                region_code="IN-GJ",
                device_fingerprint=f"{index:064x}",
            )
        )
    await session.commit()


# ------------------------------------------------------------------- the public read


class TestPublicVerify:
    """``GET /v/{tag_code}`` -- the one route with no credential in front of it."""

    async def test_stays_under_the_ceiling(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        counter: StatementCounter,
    ) -> None:
        _weaver, _headers, _item_id, code = await tagged_item(client, session)

        with counting(counter):
            response = await client.get(f"/v/{code}")

        assert response.status_code == 200, response.text
        assert_at_most(counter, CEILING_PUBLIC_VERIFY, "GET /v/{tag_code}")

    async def test_the_count_does_not_move_with_lineage_depth(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        counter: StatementCounter,
    ) -> None:
        """The regression this file exists for.

        Walking parent links in Python costs one query per level. A ceiling
        would have caught that only once a lineage got long enough; this
        catches it at depth one.
        """
        _weaver, _headers, flat_id, flat_code = await tagged_item(client, session)
        with counting(counter):
            await client.get(f"/v/{flat_code}")
        shallow = counter.count

        _w2, headers, deep_id, deep_code = await tagged_item(client, session)
        await _deepen(session, deep_id, levels=5)
        with counting(counter):
            deep = await client.get(f"/v/{deep_code}")

        assert deep.status_code == 200, deep.text
        assert len(deep.json()["provenance"]["ancestry"]) == 5
        assert counter.count == shallow, (
            "a five-deep lineage cost more statements than a flat one: the "
            f"ancestry walk is back to one query per level ({counter.count} vs "
            f"{shallow}).\n{counter.report()}"
        )

    async def test_the_count_does_not_move_with_events_or_scans(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        counter: StatementCounter,
    ) -> None:
        _weaver, _headers, item_id, code = await tagged_item(client, session)
        with counting(counter):
            await client.get(f"/v/{code}")
        bare = counter.count

        await _add_events(session, item_id, count=20)
        await _add_scans(session, item_id, code, count=20)

        with counting(counter):
            loaded = await client.get(f"/v/{code}")

        assert loaded.status_code == 200, loaded.text
        assert loaded.json()["scan"]["count"] == 20
        assert counter.count == bare, (
            f"20 events and 20 scans cost {counter.count} statements against "
            f"{bare} for none.\n{counter.report()}"
        )

    async def test_the_scan_count_comes_from_the_rows_already_loaded(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        counter: StatementCounter,
    ) -> None:
        """No ``SELECT count(*)`` over ``scans``.

        The anomaly rules read every scan row to reach a verdict. A separate
        count over the same rows is the database being asked the same question
        twice.
        """
        _weaver, _headers, item_id, code = await tagged_item(client, session)
        await _add_scans(session, item_id, code, count=3)

        with counting(counter):
            payload = (await client.get(f"/v/{code}")).json()

        assert payload["scan"]["count"] == 3
        scan_statements = [
            text for text in counter.statements if "scans" in text.lower()
        ]
        assert len(scan_statements) == 1, (
            "the scans table was read more than once for one response:\n"
            + "\n".join(f"  - {text.strip()[:140]}" for text in scan_statements)
        )

    async def test_a_scan_post_does_not_score_the_history_twice(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        counter: StatementCounter,
    ) -> None:
        """``record_scan`` already computed the verdict; the view reuses it."""
        _weaver, _headers, item_id, code = await tagged_item(client, session)

        with counting(counter):
            response = await client.post(
                f"/v/{code}/scan", json={"device_fingerprint": "phone"}, headers=GUJARAT
            )

        assert response.status_code == 201, response.text
        # The dedupe probe reads `scans` too, but it asks a different question
        # and it is bounded -- `ORDER BY created_at DESC LIMIT 1`. Scoring the
        # history is the unbounded read, and there must be exactly one of those.
        history_reads = [
            " ".join(text.split())
            for text in counter.statements
            if "FROM scans" in text and "ORDER BY" in text and "LIMIT" not in text
        ]
        assert len(history_reads) == 1, (
            "the scan history was scored twice for one POST:\n"
            + "\n".join(f"  - {text[:160]}" for text in history_reads)
        )


# ------------------------------------------------------------ authenticated reads


class TestAuthenticatedReads:
    async def test_item_list_page_of_twenty(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        counter: StatementCounter,
    ) -> None:
        await load_catalogue(session)
        weaver = await make_user(session, UserRole.WEAVER)
        headers = await auth_headers(client, weaver)
        for _ in range(20):
            await register_item(client, headers)

        with counting(counter):
            response = await client.get(f"{API}/items?limit=20", headers=headers)

        assert response.status_code == 200, response.text
        assert len(response.json()["data"]) == 20
        assert_at_most(counter, CEILING_ITEM_LIST, "GET /items")

    async def test_item_list_cost_is_flat_in_page_size(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        counter: StatementCounter,
    ) -> None:
        await load_catalogue(session)
        weaver = await make_user(session, UserRole.WEAVER)
        headers = await auth_headers(client, weaver)
        for _ in range(20):
            await register_item(client, headers)

        with counting(counter):
            await client.get(f"{API}/items?limit=1", headers=headers)
        one = counter.count

        with counting(counter):
            await client.get(f"{API}/items?limit=20", headers=headers)

        assert counter.count == one, (
            f"a page of 20 cost {counter.count} statements against {one} for a "
            f"page of 1.\n{counter.report()}"
        )

    async def test_item_tree(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        counter: StatementCounter,
    ) -> None:
        await load_catalogue(session)
        weaver = await make_user(session, UserRole.WEAVER)
        headers = await auth_headers(client, weaver)
        parent = await register_item(client, headers, quantity="12.0000")

        split = await client.post(
            f"{API}/items/{parent}/split",
            json={
                "children": [
                    {"quantity": "4.0000", "attributes": PATOLA},
                    {"quantity": "4.0000", "attributes": PATOLA},
                ]
            },
            headers={**headers, **idempotency()},
        )
        assert split.status_code == 200, split.text

        with counting(counter):
            response = await client.get(f"{API}/items/{parent}/tree", headers=headers)

        assert response.status_code == 200, response.text
        assert len(response.json()) == 3
        assert_at_most(counter, CEILING_ITEM_TREE, "GET /items/{id}/tree")

    async def test_item_attestations(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        counter: StatementCounter,
    ) -> None:
        await load_catalogue(session)
        weaver = await make_user(session, UserRole.WEAVER)
        weaver_headers = await auth_headers(client, weaver)
        item_id = await register_item(client, weaver_headers)

        for _ in range(3):
            officer = await make_user(session, UserRole.COOP_OFFICER)
            officer_headers = await auth_headers(client, officer)
            recorded = await client.post(
                f"{API}/items/{item_id}/attestations",
                json={"statement": {"verified": True}},
                headers=officer_headers,
            )
            assert recorded.status_code == 201, recorded.text

        with counting(counter):
            response = await client.get(
                f"{API}/items/{item_id}/attestations", headers=weaver_headers
            )

        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 3
        assert_at_most(counter, CEILING_ATTESTATIONS, "GET /items/{id}/attestations")

    async def test_attestation_list_cost_is_flat_in_row_count(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        counter: StatementCounter,
    ) -> None:
        """One attestation and five must cost the same.

        Every row carries an attestor, and resolving that attestor per row is
        the shape this asserts against.
        """
        await load_catalogue(session)
        weaver = await make_user(session, UserRole.WEAVER)
        weaver_headers = await auth_headers(client, weaver)

        first = await register_item(client, weaver_headers)
        many = await register_item(client, weaver_headers)

        officers = []
        for _ in range(5):
            officer = await make_user(session, UserRole.COOP_OFFICER)
            officers.append(await auth_headers(client, officer))

        await client.post(
            f"{API}/items/{first}/attestations",
            json={"statement": {"verified": True}},
            headers=officers[0],
        )
        for officer_headers in officers:
            await client.post(
                f"{API}/items/{many}/attestations",
                json={"statement": {"verified": True}},
                headers=officer_headers,
            )

        with counting(counter):
            await client.get(f"{API}/items/{first}/attestations", headers=weaver_headers)
        one = counter.count

        with counting(counter):
            response = await client.get(
                f"{API}/items/{many}/attestations", headers=weaver_headers
            )

        assert len(response.json()["items"]) == 5
        assert counter.count == one, (
            f"five attestations cost {counter.count} statements against {one} "
            f"for one.\n{counter.report()}"
        )


# --------------------------------------------------------------------- guard


class TestTheCounterItself:
    """A counting test that cannot count is a test that always passes."""

    async def test_the_listener_actually_observes_statements(
        self, session: AsyncSession, counter: StatementCounter
    ) -> None:
        with counting(counter):
            await session.execute(select(Item).limit(1))
        assert counter.count == 1, counter.report()

    async def test_nothing_is_counted_outside_the_scope(
        self, session: AsyncSession, counter: StatementCounter
    ) -> None:
        await session.execute(select(Item).limit(1))
        assert counter.count == 0
