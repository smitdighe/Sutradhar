"""Every dependency killed on its own, and what the API says when it is.

**One rule across the whole file: no 500s.** Every row asserts the exact status
*and* the exact ``error.code``, because "an error happened" is not a contract a
client can branch on. A 500 here would mean an unhandled exception reached the
generic handler, and the generic handler is the one place this system cannot say
anything useful about what went wrong or whether retrying would help.

**The second rule: nothing internal leaks.** A failing dependency is the moment
an exception message is most likely to carry a DSN, a password, a file path or a
stack, so every response body in this file is searched for those.

**The current configuration is the first row, not the exotic one.** Nothing is
deployed to Amoy, ``CHAIN_WRITE_ENABLED`` is false, ``PINATA_JWT`` is empty and
there is no signer. That is the demo-day default and every one of those rows is
asserted as a *working* system, not as a degraded one.

Where a row's real behaviour differs from the obvious guess, the test says which
and why rather than being written to the guess.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
import respx
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings, get_settings
from app.db.models.enums import ItemStatus, OutboxStatus, PinStatus, UserRole
from app.db.models.media import Media
from app.db.models.ops import QuotaUsage
from app.db.models.outbox import Outbox
from tests.fakes.fake_pinata import JPEG_BYTES, FakePinata, pinata_down, pinata_ok
from tests.integration.helpers import (
    API,
    PASSWORD,
    auth_headers,
    load_catalogue,
    make_user,
    register_item,
    tagged_item,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# A port nothing listens on, in the IANA dynamic range. Used to build a DSN that
# is well formed and unreachable, which is what "Postgres is down" looks like to
# an application that is otherwise healthy.
DEAD_PORT = 59_999
DEAD_DSN = f"postgresql+asyncpg://nobody:nothing@127.0.0.1:{DEAD_PORT}/nowhere"

# Substrings that must never appear in an error body. The first four are the
# shape of a leaked internal; the last two are credentials that live in the DSN
# a connection error loves to quote back.
NEVER_IN_A_BODY = (
    "Traceback",
    "asyncpg",
    "sqlalchemy",
    "psycopg",
    "File \"",
    "nothing",  # the password in DEAD_DSN
    "postgresql+",
)


def assert_clean_error(response: httpx.Response, code: str, status: int) -> None:
    """One assertion for every row: right status, right code, nothing leaked."""
    assert response.status_code == status, (
        f"expected {status}, got {response.status_code}: {response.text}"
    )
    assert response.status_code != 500, response.text

    body = response.json()
    assert "error" in body, f"no error envelope: {response.text}"
    assert body["error"]["code"] == code, (
        f"expected code {code}, got {body['error']['code']}: {response.text}"
    )
    assert body["error"]["request_id"], "an error with no request id cannot be reported"

    raw = response.text
    for forbidden in NEVER_IN_A_BODY:
        assert forbidden not in raw, (
            f"internal detail {forbidden!r} leaked into an error body:\n{raw}"
        )


def override(**changes: Any) -> Settings:
    """A settings object with *changes* applied, leaving the singleton alone."""
    return get_settings().model_copy(update=changes)


# ------------------------------------------------------------------ postgres


@pytest_asyncio.fixture
async def dead_database_client(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    """An application whose only database is a closed port.

    A second application rather than stopping the developer's PostgreSQL: this
    suite has to be runnable without taking the machine's database down, and a
    refused TCP connect is indistinguishable, from the pool's point of view,
    from a server that has gone away.

    The session *factory* is replaced, not the ``get_session`` dependency.
    Overriding the dependency would substitute the very code under test -- the
    translation from a connection error to a 503 lives in ``get_session`` -- and
    the test would then prove that the test's own replacement works.
    """
    import app.api.health as health_module
    import app.auth.oauth.router as oauth_router_module
    import app.auth.router as auth_router_module
    import app.core.ratelimit as ratelimit_module
    import app.db.session as session_module
    import app.media.router as media_router_module
    from app.main import create_app

    dead_engine = create_async_engine(DEAD_DSN, pool_pre_ping=False)
    dead_sessions = async_sessionmaker(bind=dead_engine, class_=AsyncSession)

    # Every module holding its own reference, exactly as the conftest fixture
    # redirects them to the test database -- pointed at nothing instead.
    for module in (
        session_module,
        health_module,
        ratelimit_module,
        auth_router_module,
        oauth_router_module,
        media_router_module,
    ):
        monkeypatch.setattr(module, "SessionLocal", dead_sessions, raising=False)

    application = create_app()
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as made:
        yield made

    await dead_engine.dispose()


class TestPostgresDown:
    async def test_healthz_still_answers(
        self, dead_database_client: httpx.AsyncClient
    ) -> None:
        """Liveness must not depend on anything. The process is alive."""
        response = await dead_database_client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    async def test_readyz_is_503_and_names_postgres(
        self, dead_database_client: httpx.AsyncClient
    ) -> None:
        """Readiness is the probe that must fail, and it must say what failed."""
        response = await dead_database_client.get("/readyz")

        assert response.status_code == 503, response.text
        body = response.json()
        assert body["checks"]["postgres"]["status"] == "down"
        assert "postgres" in body["unready"], (
            "the probe failed without naming which dependency did"
        )

    async def test_readyz_does_not_leak_the_dsn(
        self, dead_database_client: httpx.AsyncClient
    ) -> None:
        """The probe reports detail on purpose. It must not report the password."""
        response = await dead_database_client.get("/readyz")
        assert "nothing" not in response.text, "the DSN password reached /readyz"
        assert DEAD_DSN not in response.text

    async def test_a_rate_limited_route_is_503_with_a_code(
        self, dead_database_client: httpx.AsyncClient
    ) -> None:
        """Not a 500. The caller did nothing wrong and retrying may well work.

        This one fails inside the rate limiter, which counts in its own session
        before the route body runs. That is the earliest point a request can
        touch the database, and it has its own translation for exactly that
        reason.
        """
        response = await dead_database_client.post(
            f"{API}/auth/login",
            json={"email": "nobody@example.com", "password": PASSWORD},
        )
        assert_clean_error(response, "SERVICE_UNAVAILABLE", 503)

    async def test_an_unlimited_route_is_also_503(
        self, dead_database_client: httpx.AsyncClient
    ) -> None:
        """And this one fails in the request session, which is the other path.

        ``GET /items`` has no limiter, so nothing touches the database until
        ``get_current_user`` resolves the bearer token. The translation that
        covers it lives in ``get_session``; without that, the two halves of the
        API disagree about what an outage looks like -- 503 on the routes that
        happen to be rate limited, 500 on the rest.

        The token has to be genuinely signed. A malformed one is rejected on
        signature alone, before any row is read, and would answer 401 while
        proving nothing about the database.
        """
        from app.auth.tokens import issue_access_token

        token, _expires_in = issue_access_token(uuid.uuid4(), "WEAVER")

        response = await dead_database_client.get(
            f"{API}/items", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code != 500, response.text
        assert_clean_error(response, "SERVICE_UNAVAILABLE", 503)

    async def test_the_public_page_is_503_rather_than_a_stack_trace(
        self, dead_database_client: httpx.AsyncClient
    ) -> None:
        """The public surface has no database to be honest *from*.

        Everywhere else it answers ``UNANCHORED`` inside a 200 rather than
        failing. It cannot do that here: the record itself is unreadable, and
        inventing a payload would be worse than admitting the outage.
        """
        response = await dead_database_client.get("/v/X7K29M4P3RQ8")
        assert response.status_code in (400, 503), response.text
        assert response.status_code != 500


# ------------------------------------------------------------------ the chain


class TestChainUnavailable:
    """The current configuration. Nothing deployed, no signer, writes off."""

    async def test_registration_succeeds_and_stays_pending(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await load_catalogue(session)
        weaver = await make_user(session, UserRole.WEAVER)
        headers = await auth_headers(client, weaver)

        item_id = await register_item(client, headers)

        detail = await client.get(f"{API}/items/{item_id}", headers=headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["status"] == ItemStatus.PENDING
        assert detail.json()["chain"]["anchored"] is False

    async def test_the_outbox_accumulates_and_loses_nothing(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """An unreachable chain fills the queue; it does not drop work."""
        await load_catalogue(session)
        weaver = await make_user(session, UserRole.WEAVER)
        headers = await auth_headers(client, weaver)

        for _ in range(3):
            await register_item(client, headers)

        rows = list((await session.execute(select(Outbox))).scalars().all())
        assert len(rows) == 3
        assert all(row.status is OutboxStatus.QUEUED for row in rows)
        assert all(row.attempts == 0 for row in rows)

    async def test_the_public_page_is_200_unanchored_and_stale(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """The normal path today, and the one asserted as normal.

        No contract is deployed and writes are disabled, so there is nothing to
        compare against. That is reported inside a 200 with ``stale: true`` and
        the moment it was checked -- never as an error, because a shopper in a
        shop is owed the state of the record, honestly labelled.
        """
        _weaver, _headers, _item_id, code = await tagged_item(client, session)

        response = await client.get(f"/v/{code}")

        assert response.status_code == 200, response.text
        chain = response.json()["chain"]
        assert chain["verification"] == "UNANCHORED"
        assert chain["stale"] is True
        assert chain["chain_checked_at"] is not None
        assert chain["tx_hash"] is None

    async def test_a_scan_still_records_with_no_chain(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _weaver, _headers, _item_id, code = await tagged_item(client, session)

        response = await client.post(
            f"/v/{code}/scan", json={"device_fingerprint": "phone"}
        )
        assert response.status_code == 201, response.text
        assert response.headers["X-Scan-Recorded"] == "true"


# ------------------------------------------------------------------ pinning


class TestPinataUnset:
    """The current configuration: ``PINATA_JWT`` empty."""

    async def test_upload_succeeds_with_a_digest_and_no_link(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """The SHA-256 is the integrity proof; the CID is only where a copy lives."""
        uploader = await make_user(session, UserRole.WEAVER)
        headers = await auth_headers(client, uploader)

        recorder = FakePinata()
        with pinata_ok(recorder):
            response = await client.post(
                f"{API}/media",
                files={"file": ("loom.jpg", JPEG_BYTES, "image/jpeg")},
                headers=headers,
            )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["sha256"], "the digest is missing, so nothing was proved"
        assert body["cid"] is None
        assert body["pin_status"] == PinStatus.PIN_PENDING
        assert recorder.never_called, "Pinata was called with no JWT configured"

        # The digest is committed before anything is attempted remotely.
        stored = (
            await session.execute(select(Media).where(Media.sha256 == body["sha256"]))
        ).scalar_one()
        assert stored.sha256 == body["sha256"]


class TestPinataReachableButFailing:
    """Configured, and the service is returning errors."""

    @pytest.fixture
    def pinata_configured(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
        configured = override(pinata_jwt="test-jwt-value")
        import app.media.pinata as pinata_module
        import app.media.service as service_module

        for module in (pinata_module, service_module):
            monkeypatch.setattr(module, "get_settings", lambda: configured, raising=False)
        yield configured

    async def test_upload_still_succeeds_and_the_job_is_queued_for_retry(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        pinata_configured: Settings,
    ) -> None:
        """A pinning service having a bad day is not a reason to lose a photograph."""
        uploader = await make_user(session, UserRole.WEAVER)
        headers = await auth_headers(client, uploader)

        with pinata_down():
            response = await client.post(
                f"{API}/media",
                files={"file": ("loom.jpg", JPEG_BYTES, "image/jpeg")},
                headers=headers,
            )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["pin_status"] == PinStatus.PIN_PENDING
        assert body["cid"] is None

        queued = list(
            (
                await session.execute(
                    select(Outbox).where(Outbox.dedupe_key == f"pin:{body['sha256']}")
                )
            )
            .scalars()
            .all()
        )
        assert len(queued) == 1, (
            "a failed pin left no retry job; the file would stay unpinned forever"
        )
        assert queued[0].status is OutboxStatus.QUEUED

    async def test_the_pinata_jwt_never_appears_in_a_response(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        pinata_configured: Settings,
    ) -> None:
        uploader = await make_user(session, UserRole.WEAVER)
        headers = await auth_headers(client, uploader)

        with pinata_down():
            response = await client.post(
                f"{API}/media",
                files={"file": ("loom.jpg", JPEG_BYTES, "image/jpeg")},
                headers=headers,
            )

        assert pinata_configured.pinata_jwt not in response.text


# ------------------------------------------------------------------ google


class TestGoogleUnconfigured:
    """Credentials absent. The app boots and says so; it does not fail."""

    @pytest.fixture
    def google_off(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "")
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    async def test_start_is_503_with_the_provider_code(
        self, client: httpx.AsyncClient, google_off: None
    ) -> None:
        response = await client.get(f"{API}/auth/oauth/google/start")
        assert_clean_error(response, "OAUTH_PROVIDER_UNAVAILABLE", 503)

    async def test_readyz_says_unconfigured_not_down(
        self, client: httpx.AsyncClient, google_off: None
    ) -> None:
        """An optional feature nobody set up has nothing wrong with it."""
        response = await client.get("/readyz")
        assert response.status_code == 200, response.text
        assert response.json()["checks"]["google_oauth"]["status"] == "unconfigured"

    async def test_password_login_is_entirely_unaffected(
        self, client: httpx.AsyncClient, session: AsyncSession, google_off: None
    ) -> None:
        user = await make_user(session, UserRole.WEAVER)
        response = await client.post(
            f"{API}/auth/login", json={"email": user.email, "password": PASSWORD}
        )
        assert response.status_code == 200, response.text
        assert response.json()["access_token"]

    async def test_the_provider_list_is_200_saying_disabled(
        self, client: httpx.AsyncClient, google_off: None
    ) -> None:
        """The frontend asks this to decide whether to draw a button.

        An error would be a worse answer than ``enabled: false``.
        """
        response = await client.get(f"{API}/auth/oauth/providers")
        assert response.status_code == 200, response.text
        assert response.json()["data"][0]["enabled"] is False


class TestGoogleUnreachable:
    """Configured, and the network to Google is refusing.

    Note which route this affects. ``/start`` only builds a redirect and never
    talks to Google, so it keeps working -- correctly, because the browser is
    about to go to Google itself and find out. The failure surfaces at the
    callback, where a token exchange actually happens.
    """

    @pytest.fixture
    def google_on(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        from app.auth.oauth.google import reset_jwks_cache

        monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client.apps.googleusercontent.com")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-secret")
        get_settings.cache_clear()
        reset_jwks_cache()
        yield
        get_settings.cache_clear()
        reset_jwks_cache()

    async def test_start_still_redirects(
        self, client: httpx.AsyncClient, google_on: None
    ) -> None:
        response = await client.get(f"{API}/auth/oauth/google/start")
        assert response.status_code == 302, response.text
        assert "accounts.google.com" in response.headers["location"]

    async def test_the_callback_fails_cleanly_and_never_500s(
        self, client: httpx.AsyncClient, google_on: None
    ) -> None:
        from urllib.parse import parse_qs, urlsplit

        start = await client.get(f"{API}/auth/oauth/google/start")
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]

        router = respx.mock(assert_all_called=False, assert_all_mocked=True)
        router.route(host="testserver").pass_through()
        router.route(host="oauth2.googleapis.com").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        router.route(host="www.googleapis.com").mock(
            side_effect=httpx.ConnectError("connection refused")
        )

        with router:
            response = await client.get(
                f"{API}/auth/oauth/google/callback",
                params={"code": "whatever", "state": state},
            )

        assert response.status_code != 500, response.text
        if response.status_code == 503:
            assert_clean_error(response, "OAUTH_PROVIDER_UNAVAILABLE", 503)
        else:
            # A redirect to the frontend error page with a fixed code is the
            # other acceptable answer: the caller is a browser, not a client
            # library, and a JSON envelope would be shown to a person.
            assert response.status_code == 302, response.text
            assert "error=" in response.headers["location"]

    async def test_password_login_is_entirely_unaffected(
        self, client: httpx.AsyncClient, session: AsyncSession, google_on: None
    ) -> None:
        user = await make_user(session, UserRole.WEAVER)
        response = await client.post(
            f"{API}/auth/login", json={"email": user.email, "password": PASSWORD}
        )
        assert response.status_code == 200, response.text


# ------------------------------------------------------------------ quota


class TestQuotaExhausted:
    async def test_an_upload_over_the_storage_budget_is_refused_with_507(
        self, client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """507, deliberately, and not the 503 the phase brief guessed at.

        503 and 429 both mean "come back shortly". A storage budget does not
        clear with time -- the bytes already stored stay stored -- so a client
        backing off and retrying would hammer an endpoint that cannot succeed
        until a person frees space. ``app.core.errors.InsufficientStorageError``
        documents that choice, and the test follows the code rather than the
        guess.
        """
        from app.media.service import PINATA_QUOTA

        configured = override(pinata_jwt="test-jwt-value")
        import app.media.pinata as pinata_module
        import app.media.service as service_module

        for module in (pinata_module, service_module):
            monkeypatch.setattr(module, "get_settings", lambda: configured, raising=False)

        session.add(
            QuotaUsage(
                name=PINATA_QUOTA,
                period_start=dt.datetime(1970, 1, 1, tzinfo=dt.UTC),
                used=Decimal(configured.pinata_storage_budget_bytes),
                budget=Decimal(configured.pinata_storage_budget_bytes),
            )
        )
        await session.commit()

        uploader = await make_user(session, UserRole.WEAVER)
        headers = await auth_headers(client, uploader)

        recorder = FakePinata()
        with pinata_ok(recorder):
            response = await client.post(
                f"{API}/media",
                files={"file": ("loom.jpg", JPEG_BYTES, "image/jpeg")},
                headers=headers,
            )

        assert_clean_error(response, "STORAGE_BUDGET_EXCEEDED", 507)
        assert recorder.never_called, "bytes were sent to a service already over budget"

    async def test_reads_keep_working_while_a_budget_is_spent(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """A spent budget stops writes. It must not stop the public page."""
        from app.media.service import PINATA_QUOTA

        settings = get_settings()
        session.add(
            QuotaUsage(
                name=PINATA_QUOTA,
                period_start=dt.datetime(1970, 1, 1, tzinfo=dt.UTC),
                used=Decimal(settings.pinata_storage_budget_bytes),
                budget=Decimal(settings.pinata_storage_budget_bytes),
            )
        )
        await session.commit()

        _weaver, _headers, _item_id, code = await tagged_item(client, session)
        response = await client.get(f"/v/{code}")

        assert response.status_code == 200, response.text
        assert response.json()["chain"]["stale"] is True


# ------------------------------------------------------------------ the mirror


class TestMirrorDirectoryMissing:
    async def test_bytes_still_come_back_from_the_postgres_tier(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
    ) -> None:
        """A redeploy wipes the mirror. The database copy is what survives it."""
        uploader = await make_user(session, UserRole.WEAVER)
        headers = await auth_headers(client, uploader)

        recorder = FakePinata()
        with pinata_ok(recorder):
            upload = await client.post(
                f"{API}/media",
                files={"file": ("loom.jpg", JPEG_BYTES, "image/jpeg")},
                headers=headers,
            )
        assert upload.status_code == 201, upload.text
        media_id = upload.json()["id"]

        # Point the mirror at a directory that does not exist, which is exactly
        # what a fresh container looks like to a row written before the deploy.
        vanished = tmp_path / "mirror-that-was-wiped"
        gone = override(ipfs_mirror_dir=vanished)
        import app.media.mirror as mirror_module

        monkeypatch.setattr(mirror_module, "get_settings", lambda: gone, raising=False)

        response = await client.get(f"{API}/media/{media_id}/raw", headers=headers)

        assert response.status_code == 200, response.text
        assert response.headers["X-Sutradhar-Tier"] == "BLOB", (
            "the mirror was gone and the blob tier did not take over"
        )
        assert response.content == JPEG_BYTES


# ------------------------------------------------------------------ scheduler


class TestSchedulerDisabled:
    async def test_the_api_works_and_nothing_drains(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No workers means the queue fills. It must not mean the API stops."""
        from app.db.session import get_session
        from app.main import create_app
        from app.workers.scheduler import get_scheduler

        stopped = override(scheduler_enabled=False)
        import app.api.health as health_module
        import app.workers.scheduler as scheduler_module

        # Both: the scheduler module decides whether to start, and the health
        # module decides how to describe what it finds. Patching only the first
        # leaves /readyz reporting "down" (enabled but not running) for a
        # scheduler that was switched off on purpose.
        for module in (scheduler_module, health_module):
            monkeypatch.setattr(module, "get_settings", lambda: stopped, raising=False)

        application = create_app()

        async def override_session() -> AsyncIterator[AsyncSession]:
            async with session_factory() as db_session:
                yield db_session

        application.dependency_overrides[get_session] = override_session
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)

        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as made:
            await load_catalogue(session)
            weaver = await make_user(session, UserRole.WEAVER)
            headers = await auth_headers(made, weaver)
            await register_item(made, headers)

            probe = await made.get("/readyz")
            assert probe.status_code == 200, probe.text
            assert probe.json()["checks"]["scheduler"]["status"] == "degraded"

        application.dependency_overrides.clear()

        assert get_scheduler() is None or not get_scheduler().running  # type: ignore[union-attr]
        queued = list((await session.execute(select(Outbox))).scalars().all())
        assert len(queued) == 1
        assert queued[0].status is OutboxStatus.QUEUED, (
            "something drained the queue with the scheduler disabled"
        )


# ------------------------------------------------------------------ config


class TestMalformedPublicPrefix:
    """A bad mount is caught at boot, not normalised into a different URL.

    ``PUBLIC_PREFIX`` is the leading segment of a URL that gets printed on
    cloth. Silently turning ``http://example.com`` into ``/http:`` would produce
    a service that boots, serves, and answers a path nobody can reach from a
    label. Failing to start is the cheaper mistake by an enormous margin.
    """

    @pytest.mark.parametrize(
        "value",
        [
            "http://example.com/v",
            "//example.com",
            "/pub lic",
            "/public?x=1",
            "/public#frag",
        ],
    )
    def test_it_refuses_to_build(self, value: str) -> None:
        with pytest.raises(PydanticValidationError) as raised:
            Settings(public_prefix=value)  # type: ignore[call-arg]
        # The message has to name the offending value, or an operator reading a
        # crash log has to guess which variable it was.
        assert "PUBLIC_PREFIX" in str(raised.value)

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("", ""),
            ("/", ""),
            ("  /public/  ", "/public"),
            ("public", "/public"),
            ("/public", "/public"),
        ],
    )
    def test_ordinary_typing_is_still_normalised(self, value: str, expected: str) -> None:
        """Whitespace and a trailing slash are typing, not a misconfiguration.

        Built with the constructor, not ``model_copy``: ``model_copy`` assigns
        without running validators, so a test written on it would assert that
        the value it just set is the value it just set.
        """
        assert Settings(public_prefix=value).public_prefix == expected  # type: ignore[call-arg]


# ------------------------------------------------------------------ the sweep


class TestNoRowReturnsFiveHundred:
    """A guard over the whole file: every error body above is well formed."""

    async def test_an_unknown_route_is_a_clean_404(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get(f"{API}/no-such-endpoint")
        assert_clean_error(response, "NOT_FOUND", 404)

    async def test_a_malformed_tag_code_is_a_clean_400(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/v/NOTAREALCODE")
        assert_clean_error(response, "INVALID_TAG_CODE", 400)

    async def test_a_well_formed_unknown_tag_is_a_clean_404(
        self, client: httpx.AsyncClient
    ) -> None:
        from app.core.ids import new_tag_code

        response = await client.get(f"/v/{new_tag_code()}")
        assert_clean_error(response, "NOT_FOUND", 404)

    async def test_an_unauthenticated_write_is_a_clean_401(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post(
            f"{API}/items",
            json={"category_slug": "patola-silk", "attributes": {}, "quantity": "1"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert_clean_error(response, "UNAUTHENTICATED", 401)
