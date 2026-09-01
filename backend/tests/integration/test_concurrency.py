"""Two requests, one row, at the same instant.

**Sequential calls pretending to be parallel prove nothing.** Every scenario
here dispatches with :func:`asyncio.gather` over independent sessions, so the
statements genuinely interleave inside PostgreSQL and the losing request meets
the winner's row rather than its own earlier read. A test that awaits one call
and then the other exercises the happy path twice and calls it a race.

**The correct answer is almost never "both succeed".** For most of these there
is one object -- one tag, one claim, one account -- and exactly one request may
have it. What matters as much as the count is *how the loser is told*: a 409
naming the winner is a usable answer, a 500 from a unique-violation escaping
into the generic handler is not, and the second is what an unguarded read-then-
write produces under exactly this load.

Where the guarantee is the database's, the test says so. ``claims.item_id`` is
a primary key, ``uq_attestations_item_attestor`` is a unique constraint,
``items.tag_code IS NULL`` is a predicate on an UPDATE. None of these can be
lost by application code, which is the reason they are where they are.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.oauth.google import reset_jwks_cache
from app.config import get_settings
from app.db.models.catalog import Item, ItemEvent
from app.db.models.chain import ChainTx
from app.db.models.enums import (
    ItemEventType,
    OutboxJobType,
    OutboxStatus,
    UserRole,
)
from app.db.models.ops import IdempotencyKey
from app.db.models.outbox import Outbox
from app.db.models.scan import Claim
from app.db.models.user import RefreshToken, User
from tests.fakes.chain_harness import (
    build_harness,
    make_category,
    make_weaver,
    seed_item,
)
from tests.fakes.fake_google import FakeGoogle, fake_google
from tests.integration.helpers import (
    API,
    GUJARAT,
    PASSWORD,
    PATOLA,
    auth_headers,
    idempotency,
    load_catalogue,
    make_user,
    register_item,
    tag_code_of,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

OAUTH = f"{API}/auth/oauth"
CLIENT_ID = "test-client-id.apps.googleusercontent.com"


# ------------------------------------------------------------------ machinery


async def in_parallel(*calls: Awaitable[Any]) -> list[Any]:
    """Dispatch every call at once and collect results, exceptions included.

    ``return_exceptions=True`` on purpose: half the point is to see what the
    loser got, and a raising gather would hide it behind the first failure.
    """
    return list(await asyncio.gather(*calls, return_exceptions=True))


def statuses(results: list[Any]) -> list[int]:
    """HTTP status codes from a gather of responses, exceptions as 0."""
    return [
        result.status_code if isinstance(result, httpx.Response) else 0
        for result in results
    ]


def codes(results: list[Any]) -> list[str | None]:
    """The ``error.code`` of each failed response, or ``None`` when it succeeded."""
    read: list[str | None] = []
    for result in results:
        if not isinstance(result, httpx.Response) or result.status_code < 400:
            read.append(None)
            continue
        try:
            read.append(str(result.json()["error"]["code"]))
        except Exception:  # noqa: BLE001 - a body without an envelope is the finding
            read.append("<no error envelope>")
    return read


def assert_no_500(results: list[Any], scenario: str) -> None:
    """No race may surface as an internal error.

    A unique violation reaching the generic handler is the signature of a
    read-then-write, and it is a 500 to the caller: nothing they can act on and
    nothing that tells them somebody else won.
    """
    for result in results:
        assert not isinstance(result, BaseException), (
            f"{scenario}: a request raised instead of returning: {result!r}"
        )
        assert result.status_code != 500, (
            f"{scenario}: a concurrent request returned 500.\n{result.text}"
        )


async def count_of(session: AsyncSession, model: Any, *where: Any) -> int:
    statement = select(func.count()).select_from(model)
    if where:
        statement = statement.where(*where)
    return int((await session.execute(statement)).scalar_one())


@pytest_asyncio.fixture
async def clients(
    engine: Any, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[Callable[[], httpx.AsyncClient]]:
    """A factory for independent ASGI clients sharing one application.

    One application so the routes and app state are the same; separate clients
    so nothing is serialised by a shared connection pool on the client side.
    """
    from app.db.session import get_session
    from app.main import create_app

    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as db_session:
            yield db_session

    application.dependency_overrides[get_session] = override_session
    opened: list[httpx.AsyncClient] = []

    def build() -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        made = httpx.AsyncClient(transport=transport, base_url="http://testserver")
        opened.append(made)
        return made

    yield build

    for made in opened:
        await made.aclose()
    application.dependency_overrides.clear()


# ------------------------------------------------------------------ idempotency


class TestSameIdempotencyKey:
    """One key, two simultaneous registrations. One item, one outbox row."""

    async def test_two_registrations_produce_one_item(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await load_catalogue(session)
        weaver = await make_user(session, UserRole.WEAVER)
        headers = await auth_headers(client, weaver)
        shared = idempotency()

        body = {
            "category_slug": "patola-silk",
            "attributes": PATOLA,
            "quantity": "12.0000",
            "quantity_unit": "metre",
        }
        # Five rather than two. Two requests on one event loop can serialise by
        # luck -- the first finishes before the second reaches its claim -- and a
        # race test that only fires sometimes is a race test that will pass on
        # the day the bug returns. Five reliably overlaps.
        results = await in_parallel(
            *[
                client.post(f"{API}/items", json=body, headers={**headers, **shared})
                for _ in range(5)
            ]
        )

        assert_no_500(results, "parallel registration, same Idempotency-Key")
        assert await count_of(session, Item) == 1, (
            "one idempotency key produced two items: the key claim is not atomic"
        )
        assert await count_of(session, Outbox) == 1, (
            "one registration produced two anchoring jobs, which is two "
            "transactions and two gas payments for one bolt"
        )
        # Both callers get an answer they can use: either the 201, or the
        # recorded response replayed. Neither gets a 500.
        assert all(status in (200, 201) for status in statuses(results)), statuses(results)

    async def test_claiming_one_key_from_two_transactions_does_not_raise(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The primitive, driven directly, because the API cannot reach this.

        Two HTTP requests through the in-process ASGI transport tend to
        serialise: the first finishes its whole transaction before the second
        reaches its claim, so the interesting interleaving never happens. Real
        clients on real connections have no such courtesy. So this drives
        ``begin`` from two *open, uncommitted* transactions -- the exact state
        two simultaneous retries are in -- where a read-then-insert has both
        sessions see no row, both insert, and the second take a unique violation
        into the 500 handler.
        """
        from app.core import idempotency

        user = await make_user(session, UserRole.WEAVER)
        key = uuid.uuid4().hex
        payload = {"category_slug": "patola-silk", "quantity": "12.0000"}

        async def claim() -> Any:
            async with session_factory() as own:
                outcome = await idempotency.begin(own, user.id, key, payload)
                await own.commit()
                return outcome

        results = await in_parallel(claim(), claim())

        for result in results:
            assert not isinstance(result, BaseException), (
                "claiming one idempotency key from two transactions raised "
                f"{type(result).__name__}: {result}. Through the API this is a "
                "500 on a plain client retry."
            )

        rows = await count_of(session, IdempotencyKey)
        assert rows == 1, f"{rows} rows for one (user, key) pair"

    async def test_the_same_key_for_a_different_body_is_still_a_409(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """The conflict-handling rewrite must not lose the misuse check."""
        await load_catalogue(session)
        weaver = await make_user(session, UserRole.WEAVER)
        headers = await auth_headers(client, weaver)
        shared = idempotency()

        first = await client.post(
            f"{API}/items",
            json={
                "category_slug": "patola-silk",
                "attributes": PATOLA,
                "quantity": "12.0000",
                "quantity_unit": "metre",
            },
            headers={**headers, **shared},
        )
        assert first.status_code == 201, first.text

        second = await client.post(
            f"{API}/items",
            json={
                "category_slug": "patola-silk",
                "attributes": PATOLA,
                "quantity": "9.0000",
                "quantity_unit": "metre",
            },
            headers={**headers, **shared},
        )
        assert second.status_code == 409, second.text
        assert second.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


# ------------------------------------------------------------------ tag issuance


class TestTagIssuance:
    """One item, one tag. The predicate on the UPDATE is what decides."""

    async def test_two_issuances_leave_one_code_and_one_409(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await load_catalogue(session)
        weaver = await make_user(session, UserRole.WEAVER)
        headers = await auth_headers(client, weaver)
        item_id = await register_item(client, headers)

        results = await in_parallel(
            client.post(f"{API}/items/{item_id}/tag", headers={**headers, **idempotency()}),
            client.post(f"{API}/items/{item_id}/tag", headers={**headers, **idempotency()}),
        )

        assert_no_500(results, "parallel tag issuance, same item")
        observed = statuses(results)
        assert sorted(observed) == [201, 409], (
            f"expected one issuance and one conflict, got {observed}. Two 201s "
            "means the second UPDATE overwrote the first item's tag code, and "
            "whichever label was already printed now resolves to nothing."
        )
        assert "TAG_ALREADY_ISSUED" in codes(results)

        # One code on the row, and it is the one the winner was told to print.
        winner = next(
            result
            for result in results
            if isinstance(result, httpx.Response) and result.status_code == 201
        )
        assert await tag_code_of(session, item_id) == winner.json()["tag_code"]

    async def test_only_one_tag_issued_event_is_written(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """A second event would be an issuance history for a code nobody holds."""
        await load_catalogue(session)
        weaver = await make_user(session, UserRole.WEAVER)
        headers = await auth_headers(client, weaver)
        item_id = await register_item(client, headers)

        await in_parallel(
            client.post(f"{API}/items/{item_id}/tag", headers={**headers, **idempotency()}),
            client.post(f"{API}/items/{item_id}/tag", headers={**headers, **idempotency()}),
        )

        issued = await count_of(
            session,
            ItemEvent,
            ItemEvent.item_id == item_id,
            ItemEvent.event_type == ItemEventType.TAG_ISSUED,
        )
        assert issued == 1, f"{issued} TAG_ISSUED events for one item"

    async def test_five_at_once_still_leaves_one_code(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """Two is a race; five is the same race with more chances to lose it."""
        await load_catalogue(session)
        weaver = await make_user(session, UserRole.WEAVER)
        headers = await auth_headers(client, weaver)
        item_id = await register_item(client, headers)

        results = await in_parallel(
            *[
                client.post(
                    f"{API}/items/{item_id}/tag", headers={**headers, **idempotency()}
                )
                for _ in range(5)
            ]
        )

        assert_no_500(results, "five parallel tag issuances")
        observed = statuses(results)
        assert observed.count(201) == 1, observed
        assert observed.count(409) == 4, observed


# ------------------------------------------------------------------ attestations


class TestAttestations:
    """One actor may vouch for one item once. Enforced by a unique constraint."""

    async def test_the_same_actor_twice_at_once_is_one_row_and_one_409(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await load_catalogue(session)
        weaver = await make_user(session, UserRole.WEAVER)
        weaver_headers = await auth_headers(client, weaver)
        item_id = await register_item(client, weaver_headers)

        officer = await make_user(session, UserRole.COOP_OFFICER)
        officer_headers = await auth_headers(client, officer)

        body = {"statement": {"inspected": True, "note": "loom visited"}}
        results = await in_parallel(
            client.post(
                f"{API}/items/{item_id}/attestations", json=body, headers=officer_headers
            ),
            client.post(
                f"{API}/items/{item_id}/attestations", json=body, headers=officer_headers
            ),
        )

        assert_no_500(results, "parallel attestation, same actor and item")
        observed = statuses(results)
        assert sorted(observed) == [201, 409], observed
        assert "DUPLICATE_ATTESTATION" in codes(results)

        from app.db.models.attestation import Attestation

        assert await count_of(session, Attestation) == 1


# ------------------------------------------------------------------ splitting


class TestSplitting:
    """Mass balance under contention: children may never exceed the parent."""

    async def test_two_splits_cannot_over_allocate(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """Each split is legal alone; together they allocate more than exists.

        A 12 metre bolt cut into 8 and 8 is 16 metres of cloth from 12, which is
        the arithmetic a provenance system exists to make impossible. The row
        lock in the split path is what stops the second transaction reading the
        parent before the first has committed its children.
        """
        await load_catalogue(session)
        weaver = await make_user(session, UserRole.WEAVER)
        headers = await auth_headers(client, weaver)
        parent_id = await register_item(client, headers, quantity="12.0000")

        def split(size: str) -> Awaitable[httpx.Response]:
            return client.post(
                f"{API}/items/{parent_id}/split",
                json={"children": [{"quantity": size, "attributes": PATOLA}]},
                headers={**headers, **idempotency()},
            )

        results = await in_parallel(split("8.0000"), split("8.0000"))
        assert_no_500(results, "parallel splits of one parent")

        allocated = (
            await session.execute(
                select(func.coalesce(func.sum(Item.quantity), 0)).where(
                    Item.parent_id == parent_id
                )
            )
        ).scalar_one()
        assert Decimal(allocated) <= Decimal("12.0000"), (
            f"children total {allocated} metres from a 12 metre parent"
        )
        assert "MASS_BALANCE_EXCEEDED" in codes(results), codes(results)


# ------------------------------------------------------------------ refresh


class TestRefreshRotation:
    """One refresh token, two holders. Exactly one 200, and the family dies."""

    async def test_one_winner_and_the_family_is_revoked(
        self,
        clients: Callable[[], httpx.AsyncClient],
        client: httpx.AsyncClient,
        session: AsyncSession,
    ) -> None:
        user = await make_user(session, UserRole.CONSUMER)
        login = await client.post(
            f"{API}/auth/login", json={"email": user.email, "password": PASSWORD}
        )
        assert login.status_code == 200, login.text
        token = login.cookies[get_settings().refresh_cookie_name]

        # Separate clients so each carries the token itself rather than sharing
        # a cookie jar that the first rotation would rewrite mid-flight.
        first, second = clients(), clients()
        results = await in_parallel(
            first.post(f"{API}/auth/refresh", json={"refresh_token": token}),
            second.post(f"{API}/auth/refresh", json={"refresh_token": token}),
        )

        assert_no_500(results, "parallel refresh of one token")
        observed = statuses(results)
        assert observed.count(200) == 1, (
            f"expected exactly one successful rotation, got {observed}. Two "
            "means both holders now have live sessions from one token, which is "
            "indistinguishable from a stolen credential being honoured."
        )
        assert observed.count(401) == 1, observed
        assert "REFRESH_TOKEN_REUSED" in codes(results)

        # Reuse kills the whole family, including the successor the winner was
        # just issued. Two parties holding one chain is theft until proven
        # otherwise, and there is no way to tell which one was the thief.
        live = await count_of(session, RefreshToken, RefreshToken.revoked_at.is_(None))
        assert live == 0, f"{live} refresh tokens still live after a detected reuse"


# ------------------------------------------------------------------ claiming


class TestClaiming:
    """One tag, one claim. ``claims.item_id`` is a primary key."""

    async def test_simultaneous_first_scans_produce_one_claim_row(
        self,
        clients: Callable[[], httpx.AsyncClient],
        client: httpx.AsyncClient,
        session: AsyncSession,
    ) -> None:
        from tests.integration.helpers import issue_tag

        await load_catalogue(session)
        weaver = await make_user(session, UserRole.WEAVER, region="Gujarat")
        headers = await auth_headers(client, weaver)
        item_id = await register_item(client, headers)
        code = await issue_tag(client, headers, item_id)

        shoppers = [clients() for _ in range(5)]
        results = await in_parallel(
            *[
                shopper.post(
                    f"/v/{code}/scan",
                    json={"device_fingerprint": f"phone-{index}"},
                    headers=GUJARAT,
                )
                for index, shopper in enumerate(shoppers)
            ]
        )

        assert_no_500(results, "five simultaneous first scans")
        assert await count_of(session, Claim) == 1, "one tag produced more than one claim"

        payloads = [
            result.json()
            for result in results
            if isinstance(result, httpx.Response) and result.status_code < 400
        ]
        mine = [payload for payload in payloads if payload["claim"]["is_your_claim"]]
        assert len(mine) == 1, (
            f"{len(mine)} devices were each told the claim was theirs"
        )

        # And the ones that lost are told a fact, never an accusation.
        for payload in payloads:
            if payload["claim"]["is_your_claim"]:
                continue
            assert payload["claim"]["status"] == "ALREADY_CLAIMED"
            message = (payload["claim"]["message"] or "").lower()
            for forbidden in ("fake", "counterfeit", "stolen", "duplicate", "fraud"):
                assert forbidden not in message, f"accusatory wording: {message!r}"


# ------------------------------------------------------------------ oauth


class TestOAuthCompletion:
    """One pending token, two completions. One account."""

    @pytest.fixture
    def google_enabled(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
        get_settings.cache_clear()
        reset_jwks_cache()
        yield CLIENT_ID
        get_settings.cache_clear()
        reset_jwks_cache()

    @pytest.fixture
    def provider(self, google_enabled: str) -> FakeGoogle:
        return fake_google(google_enabled)

    async def test_one_pending_token_creates_exactly_one_user(
        self,
        clients: Callable[[], httpx.AsyncClient],
        client: httpx.AsyncClient,
        session: AsyncSession,
        provider: FakeGoogle,
    ) -> None:
        router = respx.mock(assert_all_called=False, assert_all_mocked=True)
        router.route(host="testserver").pass_through()
        provider.install(router)

        with router:
            start = await client.get(f"{OAUTH}/google/start")
            state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
            callback = await client.get(
                f"{OAUTH}/google/callback",
                params={"code": "fake-auth-code", "state": state},
            )
            assert callback.status_code == 302, callback.text
            token = parse_qs(urlsplit(callback.headers["location"]).query)[
                "pending_token"
            ][0]

            body = {
                "pending_token": token,
                "role": "CONSUMER",
                "display_name": "Completed User",
            }
            first, second = clients(), clients()
            results = await in_parallel(
                first.post(f"{OAUTH}/complete", json=body),
                second.post(f"{OAUTH}/complete", json=body),
            )

        assert_no_500(results, "parallel OAuth completion, one pending token")
        observed = statuses(results)
        assert observed.count(200) == 1, (
            f"expected one completion, got {observed}. Two would be two accounts "
            "for one verified Google identity."
        )
        assert await count_of(session, User) == 1

        loser = codes(results)
        assert "PENDING_TOKEN_CONSUMED" in loser, loser


# ------------------------------------------------------------------ outbox


class TestOutboxDrains:
    """Two workers on one queue. Disjoint claims, one nonce each, no gaps."""

    async def test_parallel_drains_do_not_double_send(
        self, session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
    ) -> None:
        """``FOR UPDATE SKIP LOCKED`` is the whole mechanism.

        Two drains against one queue must split it, not fight over its head. A
        job sent twice is two transactions and two gas payments anchoring one
        hash, and on a real chain the second is money spent to learn nothing.
        """
        harness = build_harness(session_factory)

        weaver = await make_weaver(session)
        category = await make_category(session)
        for _ in range(12):
            await seed_item(session, weaver, category, quantity="12.0000")
        await session.commit()

        # Two independent repositories, exactly as two processes would have.
        from app.chain.outbox import OutboxRepository

        left = OutboxRepository(session_factory, harness.settings, worker_id="worker-a")
        right = OutboxRepository(session_factory, harness.settings, worker_id="worker-b")

        claimed = await in_parallel(left.claim(limit=12), right.claim(limit=12))
        for batch in claimed:
            assert not isinstance(batch, BaseException), batch

        ids = [job.id for batch in claimed for job in batch]
        assert len(ids) == len(set(ids)), (
            "the same outbox row was leased by two workers at once"
        )
        assert len(ids) == 12, f"{len(ids)} of 12 jobs were claimed"

        in_flight = await count_of(
            session, Outbox, Outbox.status == OutboxStatus.IN_FLIGHT
        )
        assert in_flight == 12

    async def test_parallel_sends_allocate_distinct_nonces(
        self, session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
    ) -> None:
        """Two transactions at one nonce means one silently replaces the other."""
        harness = build_harness(session_factory)

        weaver = await make_weaver(session)
        category = await make_category(session)
        items = [
            await seed_item(session, weaver, category, quantity="12.0000")
            for _ in range(6)
        ]
        await session.commit()

        jobs = await harness.outbox.claim(limit=6)
        assert len(jobs) == 6

        from app.workers.jobs import _handle_job

        await in_parallel(*[_handle_job(harness.runtime, job) for job in jobs])

        nonces = list(
            (await session.execute(select(ChainTx.nonce).order_by(ChainTx.nonce)))
            .scalars()
            .all()
        )
        assert len(nonces) == len(items), f"{len(nonces)} transactions for {len(items)} jobs"
        assert len(set(nonces)) == len(nonces), f"duplicate nonce allocated: {nonces}"
        assert nonces == list(range(min(nonces), min(nonces) + len(nonces))), (
            f"nonce sequence has a gap: {nonces}"
        )

    async def test_a_job_type_is_only_claimed_by_a_drain_that_understands_it(
        self, session_factory: async_sessionmaker[AsyncSession], session: AsyncSession
    ) -> None:
        """A chain drain must not lease a pin job and kill it as unsupported."""
        from app.chain.outbox import OutboxRepository, enqueue_job
        from app.workers.jobs import CHAIN_JOB_TYPES

        harness = build_harness(session_factory)
        await enqueue_job(
            session,
            job_type=OutboxJobType.PIN_MEDIA,
            payload={"media_id": str(uuid.uuid4())},
            dedupe_key=f"pin:{uuid.uuid4().hex}",
        )
        await session.commit()

        chain_drain = OutboxRepository(session_factory, harness.settings, "chain")
        claimed = await chain_drain.claim(limit=10, job_types=CHAIN_JOB_TYPES)
        assert claimed == [], "the chain drain leased a media pinning job"
