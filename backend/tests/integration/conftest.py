"""Shared fixtures.

Integration tests run against a real PostgreSQL database -- ``TEST_DATABASE_URL``,
which is a separate database from the development one. SQLite is not a
substitute here: the things worth testing at this layer are ``ON CONFLICT DO
UPDATE``, native enum types, ``citext`` and ``JSONB``, none of which SQLite has.

The schema is built from ``Base.metadata`` rather than by running migrations, so
a schema bug and a migration bug stay distinguishable. The migration is checked
separately by its own upgrade/downgrade round trip.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings
from app.db.base import Base


def _test_database_url() -> str:
    settings = get_settings()
    if not settings.test_database_url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    return settings.test_database_url


@pytest_asyncio.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    """Session-wide engine against the test database, with the schema built once.

    The pool is deliberately larger than the app's: the concurrency tests open
    many simultaneous sessions on purpose, and a small pool would serialise
    them and quietly turn a race test into a sequential one.
    """
    test_engine = create_async_engine(_test_database_url(), pool_size=25, max_overflow=10)

    async with test_engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS citext"))
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)

    yield test_engine

    async with test_engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await test_engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A sessionmaker for code that opens its own transactions."""
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A single session for tests that drive one transaction themselves."""
    async with session_factory() as db_session:
        yield db_session


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(engine: AsyncEngine) -> AsyncIterator[None]:
    """Truncate every table between tests so ordering cannot matter."""
    yield
    tables = ", ".join(f'"{name}"' for name in Base.metadata.tables)
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture
async def client(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[httpx.AsyncClient]:
    """An ASGI client wired to the test database.

    ``get_session`` is overridden so requests use the test engine, and lifespan
    is not run -- the scheduler and the production engine have no business
    starting inside a test.
    """
    from app.db.session import get_session
    from app.main import create_app

    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as db_session:
            yield db_session

    application.dependency_overrides[get_session] = override_session

    # raise_app_exceptions=False so an unhandled exception comes back as the
    # 500 a real server would return, rather than propagating into the test.
    # That is what makes the error-handler path and the atomicity test
    # observable at all.
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client

    application.dependency_overrides.clear()


@pytest_asyncio.fixture(autouse=True)
async def _limiter_session(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: Any
) -> None:
    """Point the rate limiter and quota tracker at the test database.

    Both open their own sessions on purpose -- counting must survive a rolled
    back request -- so they bypass the dependency override and need redirecting
    separately.
    """
    import app.auth.oauth.router as oauth_router_module
    import app.auth.router as auth_router_module
    import app.core.ratelimit as ratelimit_module
    import app.db.session as session_module
    import app.media.router as media_router_module

    # Every module that imported SessionLocal into its own namespace needs
    # redirecting individually -- patching app.db.session alone would leave
    # these bound to the production engine and silently write to the dev
    # database from a test run.
    #
    # And app.db.session itself, for the opposite reason: the `rate_limit`
    # dependency imports SessionLocal *inside the function body*, so it reads
    # the attribute off app.db.session at call time and never sees a patch
    # applied to app.core.ratelimit. Anything reached through that dependency --
    # every public /v route, among others -- writes its buckets to whichever
    # database this name points at. Hoisted here rather than left to each file
    # to remember, because the failure is silent: the test passes, the rows land
    # in the development database, and the counts survive to rate-limit the next
    # run.
    # And `app.media.router`, which reaches for the factory to meter the storage
    # quotas. It resolves the name lazily now, so patching `app.db.session` above
    # is already enough -- it is named here anyway, because the version that
    # captured it at import failed only in a full run and only when some earlier
    # test had built an application outside these fixtures. Listing it is how the
    # next person learns this module needs the redirection at all.
    for module in (
        session_module,
        ratelimit_module,
        auth_router_module,
        oauth_router_module,
        media_router_module,
    ):
        monkeypatch.setattr(module, "SessionLocal", session_factory, raising=False)
