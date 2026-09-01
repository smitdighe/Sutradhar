"""Async engine and session factory backed by ``DATABASE_URL``.

**An unreachable database is a 503, not a 500**, and translating it is this
module's job because this is where the connection is made.

The translation is not optional politeness. A refused TCP connect surfaces from
asyncpg as a bare :class:`ConnectionRefusedError` -- SQLAlchemy does not wrap it
in ``OperationalError``, because there is no connection yet for a dialect to
attach the error to. So a handler registered on the SQLAlchemy exception classes
alone never fires for the most ordinary outage there is, and every request during
it returns ``INTERNAL_ERROR`` with a stack in the log and nothing useful for the
caller. :data:`UNREACHABLE_DATABASE` is the set that actually shows up.
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator

from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings
from app.core.errors import ErrorCode, UnavailableError

_settings = get_settings()

engine: AsyncEngine = create_async_engine(
    _settings.database_url,
    pool_size=_settings.db_pool_size,
    max_overflow=_settings.db_pool_max_overflow,
    pool_pre_ping=True,
    echo=False,
    future=True,
)

SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


# What "the database is unreachable" actually looks like when it reaches Python.
#
# The two SQLAlchemy classes cover a connection that existed and broke mid
# statement. `ConnectionError` and its subclasses -- refused, reset, broken pipe
# -- cover a connection that was never made, which asyncpg raises raw. gaierror
# is a hostname that does not resolve, and TimeoutError is a connect that hung.
#
# Deliberately *not* plain `OSError`: a full disk while writing a media mirror is
# also an OSError, and it is not this. Narrow enough that anything caught here
# really is the database's socket.
UNREACHABLE_DATABASE: tuple[type[BaseException], ...] = (
    OperationalError,
    InterfaceError,
    ConnectionError,
    socket.gaierror,
    TimeoutError,
)


def unavailable(exc: BaseException) -> UnavailableError:
    """The 503 to raise for *exc*.

    The message is fixed and says nothing about the exception. ``str(exc)`` on a
    connection failure quotes the DSN back, and the DSN contains a password.
    """
    return UnavailableError(
        code=ErrorCode.SERVICE_UNAVAILABLE,
        message="the database is unavailable; this request was not processed",
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped :class:`AsyncSession`.

    Failures from the route body arrive here, thrown in at the ``yield``. The
    database ones become a 503; everything else -- an ``IntegrityError`` a caller
    is catching to turn into a 409, most importantly -- passes straight through
    untouched.
    """
    async with SessionLocal() as session:
        try:
            yield session
        except UNREACHABLE_DATABASE as exc:
            raise unavailable(exc) from exc


async def dispose_engine() -> None:
    """Close every pooled connection. Called on application shutdown."""
    await engine.dispose()


__all__ = [
    "UNREACHABLE_DATABASE",
    "SessionLocal",
    "dispose_engine",
    "engine",
    "get_session",
    "unavailable",
]
