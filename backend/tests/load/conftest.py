"""A real server, a real seeded database, and the timing helpers.

**A real uvicorn subprocess, not an in-process ASGI transport.** Two hundred
requests dispatched onto the test's own event loop are two hundred coroutines
taking turns; they measure how fast Python can interleave, which is not a number
anybody deploying this cares about. A subprocess has its own loop, its own
connection pool and its own startup cost -- and the startup cost is the point of
the cold-start measurement, which cannot be taken any other way.

The server is pointed at the test database through the environment, so nothing
here can write to the development one.
"""

from __future__ import annotations

import os
import socket
import statistics
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

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

BACKEND = Path(__file__).resolve().parents[2]

# How long to wait for uvicorn to answer before giving up. Generous: the point
# of the cold-start test is to measure this, not to be defeated by it.
BOOT_TIMEOUT_SECONDS = 60.0


def free_port() -> int:
    """An ephemeral port the OS has just confirmed is unused."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def server_environment() -> dict[str, str]:
    """Environment for the child process, pinned to the test database.

    Both DSNs are overridden and the scheduler is off. A load run must not have
    background workers competing for the same connection pool it is measuring,
    and it must not touch the development database under any circumstance.
    """
    settings = get_settings()
    if not settings.test_database_url:
        pytest.skip("TEST_DATABASE_URL is not configured")

    environment = dict(os.environ)
    environment.update(
        {
            "DATABASE_URL": settings.test_database_url,
            "SCHEDULER_ENABLED": "false",
            # The limiter would refuse most of a 200-request burst, and this
            # measures the read path, not the limiter. The limiter has its own
            # tests in the security sweep.
            "RATE_LIMIT_ENABLED": "false",
            "LOG_LEVEL": "warning",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return environment


@dataclass
class Server:
    """A running uvicorn, and how long it took to answer."""

    base_url: str
    process: subprocess.Popen[bytes]
    boot_seconds: float


def start_server(port: int, probe_path: str = "/healthz") -> Server:
    """Spawn uvicorn and block until *probe_path* answers. Returns the boot time.

    The clock starts before ``Popen`` -- interpreter startup, imports, the
    lifespan and the first connection are all part of what a shopper waits
    through on a free tier that has spun the instance down.
    """
    started = time.perf_counter()
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(BACKEND),
        env=server_environment(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    base_url = f"http://127.0.0.1:{port}"
    deadline = started + BOOT_TIMEOUT_SECONDS
    while time.perf_counter() < deadline:
        if process.poll() is not None:
            stderr = (process.stderr.read() if process.stderr else b"").decode(
                "utf-8", "replace"
            )
            raise RuntimeError(f"uvicorn exited during boot:\n{stderr[-4000:]}")
        try:
            with httpx.Client(timeout=1.0) as probe:
                if probe.get(f"{base_url}{probe_path}").status_code < 500:
                    return Server(
                        base_url=base_url,
                        process=process,
                        boot_seconds=time.perf_counter() - started,
                    )
        except httpx.HTTPError:
            time.sleep(0.05)

    process.terminate()
    raise RuntimeError(f"uvicorn did not answer {probe_path} within {BOOT_TIMEOUT_SECONDS}s")


def stop_server(server: Server) -> None:
    server.process.terminate()
    try:
        server.process.wait(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover - only on a wedged child
        server.process.kill()
        server.process.wait(timeout=5)


@pytest_asyncio.fixture
async def running_server() -> AsyncIterator[Server]:
    """One uvicorn for the duration of a test."""
    server = start_server(free_port())
    try:
        yield server
    finally:
        stop_server(server)


# ------------------------------------------------------------------ timing


@dataclass
class Latencies:
    """A sample of round-trip times, and the percentiles that matter."""

    samples: list[float] = field(default_factory=list)
    statuses: list[int] = field(default_factory=list)

    def record(self, seconds: float, status: int) -> None:
        self.samples.append(seconds * 1000.0)
        self.statuses.append(status)

    @property
    def count(self) -> int:
        return len(self.samples)

    @property
    def errors(self) -> int:
        return sum(1 for status in self.statuses if status >= 400 or status == 0)

    @property
    def error_rate(self) -> float:
        return self.errors / self.count if self.count else 0.0

    def percentile(self, fraction: float) -> float:
        """Nearest-rank percentile in milliseconds.

        Nearest-rank rather than interpolated: with a few hundred samples an
        interpolated p99 invents a value between two real observations, and the
        honest answer to "what did the slowest one percent see" is one of the
        numbers actually measured.
        """
        if not self.samples:
            return 0.0
        ordered = sorted(self.samples)
        rank = max(1, min(len(ordered), round(fraction * len(ordered) + 0.5)))
        return ordered[rank - 1]

    def report(self, label: str) -> str:
        return (
            f"\n{label}\n"
            f"  requests   {self.count}\n"
            f"  errors     {self.errors} ({self.error_rate:.2%})\n"
            f"  p50        {self.percentile(0.50):8.1f} ms\n"
            f"  p95        {self.percentile(0.95):8.1f} ms\n"
            f"  p99        {self.percentile(0.99):8.1f} ms\n"
            f"  mean       {statistics.mean(self.samples):8.1f} ms\n"
            f"  max        {max(self.samples):8.1f} ms\n"
        )


def announce(lines: Sequence[str]) -> None:
    """Print a measurement so ``pytest -s`` shows it.

    These tests exist to produce numbers a person reads, not only to pass, and a
    number that is asserted but never shown is a number nobody checks.
    """
    for line in lines:
        print(line)  # noqa: T201 - the whole point of this module


# ------------------------------------------------------------------ database


@pytest_asyncio.fixture(scope="session")
async def load_engine() -> AsyncIterator[AsyncEngine]:
    """The test database with a fresh schema.

    A copy of the integration fixture rather than a shared one: that lives in
    ``tests/integration/conftest.py`` and is not visible here, and importing
    across conftest boundaries to save fifteen lines would couple two suites
    that are run at different times for different reasons.

    The pool is large because the load tests deliberately open many connections
    at once; a small one would serialise them and measure the pool instead of
    the application.
    """
    settings = get_settings()
    if not settings.test_database_url:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_async_engine(settings.test_database_url, pool_size=30, max_overflow=10)
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS citext"))
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def load_sessions(
    load_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=load_engine, class_=AsyncSession, expire_on_commit=False)
