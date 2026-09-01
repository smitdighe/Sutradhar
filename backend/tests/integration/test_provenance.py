"""Registration, splitting, mass balance, and tree queries against real Postgres.

The concurrency test here cannot be written against a mock. Two simultaneous
splits over-allocating a parent is a race between two database transactions, and
only a real one with real row locks will show whether the lock is doing its job.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
from seeds.loader import load_categories
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.catalog import registry
from app.config import get_settings
from app.core.crypto_shred import new_salt
from app.db.models.catalog import GICategory, Item, ItemEvent
from app.db.models.enums import ItemEventType, ItemStatus, UserRole, UserStatus
from app.db.models.outbox import Outbox
from app.db.models.user import User
from app.provenance import tree
from app.provenance.item_hash import quantise
from app.provenance.mass_balance import MAX_TREE_DEPTH

pytestmark = pytest.mark.integration

API = get_settings().api_prefix
ITEMS = f"{API}/items"
PASSWORD = "correct-horse-battery-staple"

PATOLA: dict[str, Any] = {
    "warp_count": 120,
    "weft_count": 116,
    "dye_type": "natural",
    "double_ikat": True,
    "loom_type": "pit",
    "weave_days": 210,
    "gi_registration_no": "GI-00232",
}


def key() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex}


async def make_user(session: AsyncSession, role: UserRole) -> tuple[User, str]:
    from app.auth.password import hash_password

    email = f"prov-{role.lower()}-{uuid.uuid4().hex[:8]}@example.com"
    user = User(
        email=email,
        password_hash=hash_password(PASSWORD),
        display_name=f"Test {role}",
        role=role,
        status=UserStatus.ACTIVE,
        identity_salt=new_salt(),
    )
    session.add(user)
    await session.commit()
    return user, email


async def token_for(client: httpx.AsyncClient, email: str) -> str:
    response = await client.post(
        f"{API}/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


@pytest.fixture
async def seeded(session: AsyncSession) -> None:
    await load_categories(session)
    await session.commit()
    registry.invalidate()


@pytest.fixture
async def weaver(
    client: httpx.AsyncClient, session: AsyncSession, seeded: None
) -> tuple[User, dict[str, str]]:
    user, email = await make_user(session, UserRole.WEAVER)
    return user, {"Authorization": f"Bearer {await token_for(client, email)}"}


def bolt_body(quantity: str = "12.0000", **overrides: Any) -> dict[str, Any]:
    return {
        "category_slug": "patola-silk",
        "attributes": PATOLA,
        "quantity": quantity,
        "quantity_unit": "metre",
        **overrides,
    }


async def register(
    client: httpx.AsyncClient, headers: dict[str, str], **overrides: Any
) -> httpx.Response:
    return await client.post(
        ITEMS, json=bolt_body(**overrides), headers={**headers, **key()}
    )


async def count(session: AsyncSession, model: Any) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


# ---------------------------------------------------------------- registration


class TestRegistration:
    async def test_register_creates_item_event_and_outbox_atomically(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        _, headers = weaver
        response = await register(client, headers)

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == ItemStatus.PENDING
        assert body["item_hash"].startswith("0x")
        assert len(body["item_hash"]) == 66
        # Fixed 4dp string, never a JSON number.
        assert body["quantity"] == "12.0000"

        item_id = uuid.UUID(body["id"])
        assert await count(session, Item) == 1
        events = (
            (await session.execute(select(ItemEvent).where(ItemEvent.item_id == item_id)))
            .scalars()
            .all()
        )
        assert [event.event_type for event in events] == [ItemEventType.REGISTERED]

        outbox = (await session.execute(select(Outbox))).scalars().all()
        assert len(outbox) == 1
        assert outbox[0].dedupe_key == body["item_hash"]
        assert outbox[0].payload["item_id"] == body["id"]

    async def test_status_is_pending_never_optimistically_confirmed(
        self, client: httpx.AsyncClient, weaver: Any
    ) -> None:
        # Nothing has touched a chain yet. Claiming CONFIRMED would be a lie
        # the demo tells, and the whole proposition is that this is trustworthy.
        _, headers = weaver
        assert (await register(client, headers)).json()["status"] == ItemStatus.PENDING

    async def test_the_registered_event_records_the_preimage(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        # A disputed hash must be auditable without re-deriving it from a row
        # that may since have been touched.
        _, headers = weaver
        body = (await register(client, headers)).json()
        event_row = (
            await session.execute(
                select(ItemEvent).where(ItemEvent.item_id == uuid.UUID(body["id"]))
            )
        ).scalar_one()

        preimage = event_row.payload["preimage"]
        assert preimage["v"] == 1
        assert preimage["quantity"] == "12.0000"
        assert preimage["registered_by_hash"].startswith("0x")
        assert event_row.payload["item_hash"] == body["item_hash"]

    async def test_the_preimage_carries_no_personal_data(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        user, headers = weaver
        body = (await register(client, headers)).json()
        event_row = (
            await session.execute(
                select(ItemEvent).where(ItemEvent.item_id == uuid.UUID(body["id"]))
            )
        ).scalar_one()

        blob = str(event_row.payload["preimage"])
        assert user.email not in blob
        assert user.display_name not in blob
        assert str(user.id) not in blob

    async def test_schema_version_is_pinned_at_write_time(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        _, headers = weaver
        body = (await register(client, headers)).json()
        category = (
            await session.execute(
                select(GICategory).where(GICategory.slug == "patola-silk")
            )
        ).scalar_one()
        assert body["category_schema_version"] == category.schema_version

    async def test_bad_attributes_are_422_and_create_nothing(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        _, headers = weaver
        response = await register(client, headers, attributes={"warp_count": "many"})

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "ATTRIBUTE_VALIDATION_FAILED"
        assert await count(session, Item) == 0
        assert await count(session, Outbox) == 0
        assert await count(session, ItemEvent) == 0

    async def test_wrong_quantity_unit_is_422(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        # Pairs of silk. The unit is part of what the hash commits to.
        _, headers = weaver
        response = await register(client, headers, quantity_unit="pair")

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "QUANTITY_UNIT_MISMATCH"
        assert await count(session, Item) == 0

    async def test_unknown_category_is_404(
        self, client: httpx.AsyncClient, weaver: Any
    ) -> None:
        _, headers = weaver
        response = await register(client, headers, category_slug="no-such-category")
        assert response.status_code == 404

    @pytest.mark.parametrize("role", [UserRole.CONSUMER, UserRole.INSPECTOR])
    async def test_only_weavers_and_coop_officers_may_register(
        self, client: httpx.AsyncClient, session: AsyncSession, seeded: None, role: UserRole
    ) -> None:
        # Registration is a claim about physical goods. A consumer scanning a
        # tag has no business making one.
        _, email = await make_user(session, role)
        headers = {"Authorization": f"Bearer {await token_for(client, email)}"}
        response = await register(client, headers)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"

    async def test_coop_officer_may_register(
        self, client: httpx.AsyncClient, session: AsyncSession, seeded: None
    ) -> None:
        _, email = await make_user(session, UserRole.COOP_OFFICER)
        headers = {"Authorization": f"Bearer {await token_for(client, email)}"}
        assert (await register(client, headers)).status_code == 201

    async def test_anonymous_cannot_register(self, client: httpx.AsyncClient) -> None:
        assert (await client.post(ITEMS, json=bolt_body(), headers=key())).status_code == 401


class TestIdempotency:
    async def test_the_idempotency_key_is_required(
        self, client: httpx.AsyncClient, weaver: Any
    ) -> None:
        # A retried POST that created a second item would put two hashes on
        # chain for one bolt.
        _, headers = weaver
        response = await client.post(ITEMS, json=bolt_body(), headers=headers)
        assert response.status_code == 422
        assert "Idempotency-Key" in response.json()["error"]["message"]

    async def test_same_key_replays_the_identical_response(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        _, headers = weaver
        shared = {**headers, "Idempotency-Key": uuid.uuid4().hex}

        first = await client.post(ITEMS, json=bolt_body(), headers=shared)
        second = await client.post(ITEMS, json=bolt_body(), headers=shared)

        assert first.status_code == second.status_code == 201
        assert first.json() == second.json()
        assert await count(session, Item) == 1
        assert await count(session, Outbox) == 1

    async def test_same_key_different_body_is_409(
        self, client: httpx.AsyncClient, weaver: Any
    ) -> None:
        _, headers = weaver
        shared = {**headers, "Idempotency-Key": uuid.uuid4().hex}

        await client.post(ITEMS, json=bolt_body(), headers=shared)
        response = await client.post(ITEMS, json=bolt_body("9.0000"), headers=shared)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


class TestAtomicity:
    async def test_a_failure_after_the_item_insert_persists_nothing(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        weaver: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # An item with no outbox row never gets anchored and looks permanently
        # pending; an outbox row with no item anchors a hash of nothing. Both
        # halves have to commit together or neither does.
        _, headers = weaver

        import app.provenance.service as service_module

        real_enqueue = service_module._enqueue_anchor

        def explode(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("injected failure after the item insert")

        monkeypatch.setattr(service_module, "_enqueue_anchor", explode)

        response = await client.post(ITEMS, json=bolt_body(), headers={**headers, **key()})
        assert response.status_code == 500
        # The generic handler's opaque code. Nothing about the injected
        # RuntimeError reaches the client -- an unexpected exception is exactly
        # where a message is most likely to carry something private.
        assert response.json()["error"]["code"] == "INTERNAL_ERROR"
        assert response.json()["error"]["message"] == "an internal error occurred"

        monkeypatch.setattr(service_module, "_enqueue_anchor", real_enqueue)

        async with session_factory() as fresh:
            assert await count(fresh, Item) == 0
            assert await count(fresh, ItemEvent) == 0
            assert await count(fresh, Outbox) == 0


# ---------------------------------------------------------------- splitting


class TestSplitting:
    async def test_split_twelve_into_two_fives_leaves_one(
        self, client: httpx.AsyncClient, weaver: Any
    ) -> None:
        _, headers = weaver
        parent = (await register(client, headers)).json()

        response = await client.post(
            f"{ITEMS}/{parent['id']}/split",
            json={
                "children": [
                    {"attributes": PATOLA, "quantity": "5.5000"},
                    {"attributes": PATOLA, "quantity": "5.5000"},
                ]
            },
            headers={**headers, **key()},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["children"]) == 2
        assert body["allocated"] == "11.0000"
        assert body["remaining"] == "1.0000"

    async def test_each_child_gets_its_own_hash_and_outbox_row(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        _, headers = weaver
        parent = (await register(client, headers)).json()
        response = await client.post(
            f"{ITEMS}/{parent['id']}/split",
            json={
                "children": [
                    {"attributes": PATOLA, "quantity": "5.5000"},
                    {"attributes": PATOLA, "quantity": "5.5000"},
                ]
            },
            headers={**headers, **key()},
        )
        hashes = {child["item_hash"] for child in response.json()["children"]}
        assert len(hashes) == 2
        assert parent["item_hash"] not in hashes

        # One for the parent, one per child.
        assert await count(session, Outbox) == 3

    async def test_the_split_event_is_written_on_the_parent(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        # The transformation happened to the bolt, and its event log is where
        # somebody auditing the bolt will look.
        _, headers = weaver
        parent = (await register(client, headers)).json()
        await client.post(
            f"{ITEMS}/{parent['id']}/split",
            json={"children": [{"attributes": PATOLA, "quantity": "5.5000"}]},
            headers={**headers, **key()},
        )

        events = (
            (
                await session.execute(
                    select(ItemEvent).where(
                        ItemEvent.item_id == uuid.UUID(parent["id"]),
                        ItemEvent.event_type == ItemEventType.SPLIT,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert len(events[0].payload["children"]) == 1

    async def test_over_allocation_is_409_with_the_remainder(
        self, client: httpx.AsyncClient, weaver: Any
    ) -> None:
        _, headers = weaver
        parent = (await register(client, headers)).json()

        response = await client.post(
            f"{ITEMS}/{parent['id']}/split",
            json={
                "children": [
                    {"attributes": PATOLA, "quantity": "7.0000"},
                    {"attributes": PATOLA, "quantity": "6.0000"},
                ]
            },
            headers={**headers, **key()},
        )
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "MASS_BALANCE_EXCEEDED"
        assert error["details"]["remaining"] == "12.0000"
        assert error["details"]["requested"] == "13.0000"

    async def test_a_refused_split_creates_no_children(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        _, headers = weaver
        parent = (await register(client, headers)).json()
        await client.post(
            f"{ITEMS}/{parent['id']}/split",
            json={"children": [{"attributes": PATOLA, "quantity": "99.0000"}]},
            headers={**headers, **key()},
        )
        assert await count(session, Item) == 1

    async def test_a_second_split_respects_what_is_already_allocated(
        self, client: httpx.AsyncClient, weaver: Any
    ) -> None:
        _, headers = weaver
        parent = (await register(client, headers)).json()

        await client.post(
            f"{ITEMS}/{parent['id']}/split",
            json={"children": [{"attributes": PATOLA, "quantity": "10.0000"}]},
            headers={**headers, **key()},
        )
        response = await client.post(
            f"{ITEMS}/{parent['id']}/split",
            json={"children": [{"attributes": PATOLA, "quantity": "3.0000"}]},
            headers={**headers, **key()},
        )
        assert response.status_code == 409
        assert response.json()["error"]["details"]["remaining"] == "2.0000"

    async def test_exact_allocation_is_allowed(
        self, client: httpx.AsyncClient, weaver: Any
    ) -> None:
        _, headers = weaver
        parent = (await register(client, headers)).json()
        response = await client.post(
            f"{ITEMS}/{parent['id']}/split",
            json={"children": [{"attributes": PATOLA, "quantity": "12.0000"}]},
            headers={**headers, **key()},
        )
        assert response.status_code == 200
        assert response.json()["remaining"] == "0.0000"

    async def test_a_child_cannot_change_unit(
        self, client: httpx.AsyncClient, weaver: Any
    ) -> None:
        # You cannot subtract pairs from metres.
        _, headers = weaver
        parent = (await register(client, headers)).json()
        response = await client.post(
            f"{ITEMS}/{parent['id']}/split",
            json={
                "children": [
                    {"attributes": PATOLA, "quantity": "5.0000", "quantity_unit": "pair"}
                ]
            },
            headers={**headers, **key()},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "QUANTITY_UNIT_MISMATCH"

    async def test_fractional_amounts_do_not_drift(
        self, client: httpx.AsyncClient, weaver: Any
    ) -> None:
        # Three thirds of 0.3 in float would leave a residue. In Decimal it is
        # exactly zero, and that difference is a hole a counterfeiter walks
        # through a few grams at a time.
        _, headers = weaver
        parent = (await register(client, headers, quantity="0.3000")).json()
        response = await client.post(
            f"{ITEMS}/{parent['id']}/split",
            json={
                "children": [
                    {"attributes": PATOLA, "quantity": "0.1000"},
                    {"attributes": PATOLA, "quantity": "0.1000"},
                    {"attributes": PATOLA, "quantity": "0.1000"},
                ]
            },
            headers={**headers, **key()},
        )
        assert response.status_code == 200
        assert response.json()["remaining"] == "0.0000"

    async def test_splitting_an_unknown_parent_is_404(
        self, client: httpx.AsyncClient, weaver: Any
    ) -> None:
        _, headers = weaver
        response = await client.post(
            f"{ITEMS}/{uuid.uuid4()}/split",
            json={"children": [{"attributes": PATOLA, "quantity": "1.0000"}]},
            headers={**headers, **key()},
        )
        assert response.status_code == 404


class TestConcurrentSplits:
    async def test_two_parallel_seven_metre_splits_yield_exactly_one_winner(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        weaver: Any,
    ) -> None:
        # THE test for this phase. Two 7m splits of a 12m bolt: individually
        # fine, together an over-allocation. Without SELECT ... FOR UPDATE on
        # the parent both read "12 remaining" and both commit, and the bolt
        # yields 14 metres of cloth.
        _, headers = weaver
        parent = (await register(client, headers)).json()

        async def attempt() -> httpx.Response:
            return await client.post(
                f"{ITEMS}/{parent['id']}/split",
                json={"children": [{"attributes": PATOLA, "quantity": "7.0000"}]},
                headers={**headers, **key()},
            )

        results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
        statuses = sorted(
            item.status_code for item in results if isinstance(item, httpx.Response)
        )
        assert statuses == [200, 409]

        loser = next(
            item
            for item in results
            if isinstance(item, httpx.Response) and item.status_code == 409
        )
        assert loser.json()["error"]["code"] == "MASS_BALANCE_EXCEEDED"

        async with session_factory() as fresh:
            children = (
                (
                    await fresh.execute(
                        select(Item).where(Item.parent_id == uuid.UUID(parent["id"]))
                    )
                )
                .scalars()
                .all()
            )
            assert len(children) == 1
            allocated = sum(quantise(child.quantity) for child in children)
            assert allocated == Decimal("7.0000")

    async def test_four_parallel_splits_never_over_allocate(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        weaver: Any,
    ) -> None:
        _, headers = weaver
        parent = (await register(client, headers)).json()

        async def attempt() -> httpx.Response:
            return await client.post(
                f"{ITEMS}/{parent['id']}/split",
                json={"children": [{"attributes": PATOLA, "quantity": "5.0000"}]},
                headers={**headers, **key()},
            )

        results = await asyncio.gather(*(attempt() for _ in range(4)), return_exceptions=True)
        succeeded = [
            item
            for item in results
            if isinstance(item, httpx.Response) and item.status_code == 200
        ]
        # 12 / 5 = two fit, whatever the interleaving.
        assert len(succeeded) == 2

        async with session_factory() as fresh:
            total = (
                await fresh.execute(
                    select(func.coalesce(func.sum(Item.quantity), 0)).where(
                        Item.parent_id == uuid.UUID(parent["id"])
                    )
                )
            ).scalar_one()
            assert quantise(Decimal(total)) <= Decimal("12.0000")


class TestDepthLimit:
    async def test_depth_six_is_rejected(
        self, client: httpx.AsyncClient, weaver: Any
    ) -> None:
        # Unbounded recursion is a denial-of-service vector, and the recursive
        # CTEs would walk it.
        _, headers = weaver
        current = (await register(client, headers, quantity="100.0000")).json()

        for level in range(2, MAX_TREE_DEPTH + 1):
            response = await client.post(
                f"{ITEMS}/{current['id']}/split",
                json={"children": [{"attributes": PATOLA, "quantity": "10.0000"}]},
                headers={**headers, **key()},
            )
            assert response.status_code == 200, f"level {level} should be allowed"
            current = response.json()["children"][0]

        response = await client.post(
            f"{ITEMS}/{current['id']}/split",
            json={"children": [{"attributes": PATOLA, "quantity": "1.0000"}]},
            headers={**headers, **key()},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "MAX_DEPTH_EXCEEDED"


# ---------------------------------------------------------------- tree queries


class TestTreeQueries:
    async def _four_level_tree(
        self, client: httpx.AsyncClient, headers: dict[str, str]
    ) -> list[dict[str, Any]]:
        chain = [(await register(client, headers, quantity="100.0000")).json()]
        for _ in range(3):
            response = await client.post(
                f"{ITEMS}/{chain[-1]['id']}/split",
                json={"children": [{"attributes": PATOLA, "quantity": "10.0000"}]},
                headers={**headers, **key()},
            )
            assert response.status_code == 200, response.text
            chain.append(response.json()["children"][0])
        return chain

    async def test_ancestry_is_root_first(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        _, headers = weaver
        chain = await self._four_level_tree(client, headers)

        ancestry = await tree.get_ancestry(session, uuid.UUID(chain[-1]["id"]))
        assert [str(node.id) for node in ancestry] == [node["id"] for node in chain]
        assert ancestry[0].parent_id is None

    async def test_ancestry_issues_exactly_one_statement(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        weaver: Any,
    ) -> None:
        # The obvious implementation walks parent links in a loop and issues one
        # query per level. On Neon's free tier that is four round trips for a
        # page that renders a provenance chain.
        _, headers = weaver
        chain = await self._four_level_tree(client, headers)

        statements: list[str] = []

        async with session_factory() as counting:
            engine = counting.get_bind()

            def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
                statements.append(statement)

            event.listen(engine, "before_cursor_execute", record)
            try:
                ancestry = await tree.get_ancestry(counting, uuid.UUID(chain[-1]["id"]))
            finally:
                event.remove(engine, "before_cursor_execute", record)

        assert len(ancestry) == 4
        assert len(statements) == 1, statements

    async def test_descendants_issues_exactly_one_statement(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        weaver: Any,
    ) -> None:
        _, headers = weaver
        chain = await self._four_level_tree(client, headers)

        statements: list[str] = []
        async with session_factory() as counting:
            engine = counting.get_bind()

            def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
                statements.append(statement)

            event.listen(engine, "before_cursor_execute", record)
            try:
                subtree = await tree.get_descendants(counting, uuid.UUID(chain[0]["id"]))
            finally:
                event.remove(engine, "before_cursor_execute", record)

        assert len(subtree) == 4
        assert len(statements) == 1, statements

    async def test_descendants_are_depth_annotated(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        _, headers = weaver
        chain = await self._four_level_tree(client, headers)
        subtree = await tree.get_descendants(session, uuid.UUID(chain[0]["id"]))
        assert [node.depth for node in subtree] == [1, 2, 3, 4]

    async def test_remaining_quantity(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        _, headers = weaver
        parent = (await register(client, headers)).json()
        await client.post(
            f"{ITEMS}/{parent['id']}/split",
            json={"children": [{"attributes": PATOLA, "quantity": "5.5000"}]},
            headers={**headers, **key()},
        )
        remaining = await tree.get_remaining_quantity(session, uuid.UUID(parent["id"]))
        assert remaining == Decimal("6.5000")

    async def test_remaining_for_a_leaf_is_its_whole_quantity(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        _, headers = weaver
        item = (await register(client, headers)).json()
        assert await tree.get_remaining_quantity(
            session, uuid.UUID(item["id"])
        ) == Decimal("12.0000")

    async def test_an_item_cannot_be_its_own_parent(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        # Enforced by a CHECK, so it is unrepresentable rather than unlikely.
        from sqlalchemy.exc import IntegrityError

        _, headers = weaver
        item = (await register(client, headers)).json()
        row = await session.get(Item, uuid.UUID(item["id"]))
        assert row is not None
        row.parent_id = row.id
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


# ---------------------------------------------------------------- read endpoints


class TestReadEndpoints:
    async def test_item_detail_carries_lineage_and_children(
        self, client: httpx.AsyncClient, weaver: Any
    ) -> None:
        _, headers = weaver
        parent = (await register(client, headers)).json()
        split = await client.post(
            f"{ITEMS}/{parent['id']}/split",
            json={"children": [{"attributes": PATOLA, "quantity": "5.5000"}]},
            headers={**headers, **key()},
        )
        child_id = split.json()["children"][0]["id"]

        detail = await client.get(f"{ITEMS}/{child_id}", headers=headers)
        assert detail.status_code == 200
        body = detail.json()
        assert body["category_slug"] == "patola-silk"
        assert [node["id"] for node in body["ancestry"]] == [parent["id"]]
        assert body["children"] == []
        assert body["chain"]["anchored"] is False
        assert body["chain"]["status"] == ItemStatus.PENDING

        parent_detail = (await client.get(f"{ITEMS}/{parent['id']}", headers=headers)).json()
        assert [node["id"] for node in parent_detail["children"]] == [child_id]
        assert parent_detail["remaining_quantity"] == "6.5000"

    async def test_listing_filters_and_paginates(
        self, client: httpx.AsyncClient, weaver: Any
    ) -> None:
        user, headers = weaver
        for _ in range(3):
            await register(client, headers)

        listing = await client.get(ITEMS, params={"limit": 2}, headers=headers)
        assert listing.status_code == 200
        body = listing.json()
        assert len(body["data"]) == 2
        assert body["pagination"]["next_cursor"]

        page_two = await client.get(
            ITEMS,
            params={"limit": 2, "cursor": body["pagination"]["next_cursor"]},
            headers=headers,
        )
        assert len(page_two.json()["data"]) == 1

        filtered = await client.get(
            ITEMS, params={"registered_by": str(user.id)}, headers=headers
        )
        assert len(filtered.json()["data"]) == 3

        by_category = await client.get(
            ITEMS, params={"category_slug": "patola-silk"}, headers=headers
        )
        assert len(by_category.json()["data"]) == 3

        by_status = await client.get(ITEMS, params={"status": "CONFIRMED"}, headers=headers)
        assert by_status.json()["data"] == []

    async def test_tree_endpoint_returns_the_subtree(
        self, client: httpx.AsyncClient, weaver: Any
    ) -> None:
        _, headers = weaver
        parent = (await register(client, headers)).json()
        await client.post(
            f"{ITEMS}/{parent['id']}/split",
            json={"children": [{"attributes": PATOLA, "quantity": "5.5000"}]},
            headers={**headers, **key()},
        )
        response = await client.get(f"{ITEMS}/{parent['id']}/tree", headers=headers)
        assert response.status_code == 200
        assert [node["depth"] for node in response.json()] == [1, 2]

    async def test_events_endpoint_is_append_only_order(
        self, client: httpx.AsyncClient, weaver: Any
    ) -> None:
        _, headers = weaver
        parent = (await register(client, headers)).json()
        await client.post(
            f"{ITEMS}/{parent['id']}/split",
            json={"children": [{"attributes": PATOLA, "quantity": "5.5000"}]},
            headers={**headers, **key()},
        )
        response = await client.get(f"{ITEMS}/{parent['id']}/events", headers=headers)
        assert response.status_code == 200
        assert [row["event_type"] for row in response.json()["data"]] == [
            ItemEventType.REGISTERED,
            ItemEventType.SPLIT,
        ]

    async def test_reads_require_authentication(
        self, client: httpx.AsyncClient, weaver: Any
    ) -> None:
        # The public verification view is Phase 11 and a different serialiser.
        # These responses carry registrant ids and full attributes.
        _, headers = weaver
        item = (await register(client, headers)).json()

        for path in (ITEMS, f"{ITEMS}/{item['id']}", f"{ITEMS}/{item['id']}/tree"):
            assert (await client.get(path)).status_code == 401

    async def test_unknown_item_is_404(
        self, client: httpx.AsyncClient, weaver: Any
    ) -> None:
        _, headers = weaver
        response = await client.get(f"{ITEMS}/{uuid.uuid4()}", headers=headers)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "ITEM_NOT_FOUND"
