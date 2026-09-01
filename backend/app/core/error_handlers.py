"""One error envelope for every failure the API can produce.

Every error response, whatever caused it, looks like::

    {"error": {"code": "...", "message": "...", "details": ... , "request_id": "..."}}

Uniformity matters more than expressiveness here: a client writes one error
parser, and the ``request_id`` in the body is what a user quotes when reporting
a failure.

Unhandled exceptions log a full traceback server-side and return a generic
``INTERNAL_ERROR``. Nothing about the internals -- exception type, message,
stack, SQL -- reaches the client, because an unexpected exception is exactly
the case where the message is most likely to contain something private.

Handlers are matched along the exception's MRO, most specific first, so the
connection-level database handler below wins over the catch-all without either
of them knowing about the other.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError, ErrorCode, RateLimitError
from app.core.logging import get_logger, request_id_ctx

__all__ = ["error_response", "register_error_handlers"]

logger = get_logger(__name__)

_STATUS_TO_CODE = {
    400: ErrorCode.VALIDATION_FAILED,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    409: ErrorCode.CONFLICT,
    422: ErrorCode.VALIDATION_FAILED,
    429: ErrorCode.RATE_LIMITED,
    503: ErrorCode.SERVICE_UNAVAILABLE,
}


def error_response(
    status: int,
    code: ErrorCode,
    message: str,
    details: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build the canonical error envelope."""
    return JSONResponse(
        status_code=status,
        headers=headers,
        content={
            "error": {
                "code": str(code),
                "message": message,
                "details": details,
                "request_id": request_id_ctx.get(),
            }
        },
    )


def register_error_handlers(app: FastAPI) -> None:
    """Attach every handler to *app*."""

    @app.exception_handler(AppError)
    async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        headers: dict[str, str] | None = None
        if isinstance(exc, RateLimitError):
            headers = {"Retry-After": str(exc.retry_after)}
        # Expected failures: recorded at info, not error. A 404 is not an
        # incident, and logging it as one trains people to ignore the channel.
        logger.info("app_error", code=str(exc.code), status=exc.status)
        return error_response(exc.status, exc.code, exc.message, exc.details, headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Pydantic's error list is JSON-safe apart from `ctx`, which can hold
        # arbitrary objects and occasionally the offending input itself.
        details = [
            {
                "loc": [str(part) for part in error.get("loc", ())],
                "msg": error.get("msg", ""),
                "type": error.get("type", ""),
            }
            for error in exc.errors()
        ]
        logger.info("validation_error", error_count=len(details))
        return error_response(
            422, ErrorCode.VALIDATION_FAILED, "request validation failed", details
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = _STATUS_TO_CODE.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        message = str(exc.detail) if exc.detail else str(code).replace("_", " ").lower()
        return error_response(exc.status_code, code, message)

    @app.exception_handler(OperationalError)
    @app.exception_handler(InterfaceError)
    async def handle_database_unavailable(
        _request: Request, exc: Exception
    ) -> JSONResponse:
        """A database that cannot be reached is a 503, not a 500.

        A backstop, not the main mechanism: :mod:`app.db.session` translates
        these where the connection is made, because the commonest case -- a
        refused connect -- never becomes a SQLAlchemy exception at all and so
        could never be caught here. This covers a connection that breaks inside
        a code path holding its own session.

        Registered on the two connection-level classes specifically, not on
        ``DBAPIError``: an ``IntegrityError`` is a constraint doing its job and
        several call sites depend on catching it themselves.

        The message is fixed. ``str(exc)`` on a connection failure contains the
        DSN, and the DSN contains a password.
        """
        logger.error("database_unavailable", error_type=type(exc).__name__)
        return error_response(
            503,
            ErrorCode.SERVICE_UNAVAILABLE,
            "the database is unavailable; this request was not processed",
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        # Full traceback server-side, nothing but a generic message to the client.
        logger.error("unhandled_exception", exc_info=exc)
        return error_response(
            500,
            ErrorCode.INTERNAL_ERROR,
            "an internal error occurred",
        )
