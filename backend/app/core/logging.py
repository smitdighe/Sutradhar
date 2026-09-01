"""structlog configuration, request correlation, and outbound redaction.

Every log line carries a ``request_id``. Inbound ``X-Request-ID`` is honoured so
a trace survives a proxy hop; otherwise one is generated, and either way it is
echoed back on the response so a user reporting a failure can quote the id.

Redaction runs as the last processor before rendering, matching on key *name*
anywhere in the event dict, recursively. It is deliberately blunt: a key called
``password`` is redacted whether it holds a password or not. The failure mode of
over-redaction is an unhelpful log line; the failure mode of under-redaction is
a credential in a log aggregator forever.
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.config import get_settings

__all__ = [
    "REDACTED",
    "REDACT_KEYS",
    "REQUEST_ID_HEADER",
    "RequestContextMiddleware",
    "configure_logging",
    "get_logger",
    "redact",
    "request_id_ctx",
]

REQUEST_ID_HEADER = "X-Request-ID"
REDACTED = "[REDACTED]"

# Matched case-insensitively as a substring of the key, so 'client_secret',
# 'X-Auth-Token' and 'refresh_token_hash' are all caught by their stem.
REDACT_KEYS = frozenset(
    {
        "password",
        "token",
        "secret",
        "authorization",
        "cookie",
        "private_key",
        "client_secret",
        "pending_token",
    }
)

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

_MAX_REDACT_DEPTH = 12


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in REDACT_KEYS)


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively replace values under sensitive key names.

    The depth cap stops a cyclic or pathologically nested structure from
    turning a log call into a stack overflow.
    """
    if _depth >= _MAX_REDACT_DEPTH:
        return value
    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if isinstance(key, str) and _is_sensitive(key)
                else redact(item, _depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        rebuilt = [redact(item, _depth + 1) for item in value]
        return type(value)(rebuilt) if isinstance(value, tuple) else rebuilt
    return value


def _redact_processor(
    _logger: Any, _method: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    """Last stop before rendering: scrub the whole event dict."""
    scrubbed: structlog.typing.EventDict = redact(dict(event_dict))
    return scrubbed


def _inject_request_id(
    _logger: Any, _method: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    event_dict.setdefault("request_id", request_id_ctx.get())
    return event_dict


def configure_logging() -> None:
    """Install structlog processors and align stdlib logging with LOG_LEVEL."""
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper())

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.is_production
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _inject_request_id,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _redact_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind a request id for the life of the request and echo it on the response."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        token = request_id_ctx.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_ctx.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
