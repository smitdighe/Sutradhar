"""FastAPI application factory: middleware, lifespan, and router mounting."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.api import health
from app.api.router import api_router
from app.catalog.registry import assert_single_instance
from app.config import get_settings
from app.core.error_handlers import register_error_handlers
from app.core.logging import (
    REQUEST_ID_HEADER,
    RequestContextMiddleware,
    configure_logging,
    get_logger,
    request_id_ctx,
)
from app.db.session import SessionLocal, dispose_engine, engine
from app.verification.router import router as public_router
from app.workers.jobs import ChainRuntime, shutdown_chain_workers, start_chain_workers
from app.workers.scheduler import shutdown_scheduler, start_scheduler

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start background workers on boot, release resources on shutdown."""
    settings = get_settings()
    scheduler = start_scheduler()
    # States the single-instance assumption the category registry rests on,
    # where somebody scaling out will actually see it.
    assert_single_instance()

    runtime: ChainRuntime | None = None
    if scheduler is not None:
        # Deliberately not gated on the chain being reachable. An unreachable
        # RPC endpoint leaves the outbox filling and items honestly PENDING;
        # refusing to boot would turn a degraded dependency into an outage.
        runtime = await start_chain_workers(scheduler, SessionLocal, engine, settings)
    app.state.chain_runtime = runtime

    logger.info(
        "startup",
        app_env=settings.app_env,
        google_oauth_enabled=settings.google_oauth_enabled,
        pinata_enabled=settings.pinata_enabled,
        chain_write_enabled=settings.chain_write_enabled,
        chain_available=runtime.client.available if runtime else None,
        chain_signer_configured=settings.chain_signer_configured,
    )
    try:
        yield
    finally:
        shutdown_scheduler()
        await shutdown_chain_workers(runtime)
        await dispose_engine()
        logger.info("shutdown")


def create_app() -> FastAPI:
    """Build and configure the ASGI application."""
    settings = get_settings()
    configure_logging()

    app = FastAPI(
        title="Sutradhar API",
        version="0.1.0",
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # ETag drives the public page's conditional reads, and X-Scan-Recorded
        # is how a browser client tells a fresh scan from a deduplicated retry.
        expose_headers=[REQUEST_ID_HEADER, "ETag", "X-Scan-Recorded"],
    )
    app.add_middleware(RequestContextMiddleware)

    @app.middleware("http")
    async def access_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """One structured line per request, correlated by request id.

        ``user_id`` is present only when the request authenticated, and it is
        the id alone -- ``app.auth.guards`` publishes it on ``request.state``
        after resolving the token, so this middleware never parses a credential
        and never sees an email address.
        """
        started = time.perf_counter()
        response = await call_next(request)
        user_id = getattr(request.state, "user_id", None)
        logger.info(
            "request",
            request_id=request_id_ctx.get(),
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            user_id=str(user_id) if user_id is not None else None,
        )
        return response

    register_error_handlers(app)

    # Probes stay unprefixed so orchestrators do not track the API version.
    app.include_router(health.router)
    # The public verification surface is unprefixed for a different reason:
    # its path is printed on cloth. `/v/{tag_code}` is what a QR resolves to,
    # and a version segment in it could never be changed afterwards.
    app.include_router(public_router)
    app.include_router(api_router, prefix=settings.api_prefix)

    return app


app = create_app()

__all__ = ["app", "create_app"]
