"""Application error taxonomy and the frozen set of error codes.

Every error the API can emit carries a stable SCREAMING_SNAKE code from
:class:`ErrorCode`. Codes are part of the public contract: clients branch on
them, so they are append-only and never renamed. No module outside this one may
invent a code string -- if a new failure mode needs one, it is added to the
enum here first.

Append-only applies to codes that have ever been *emitted*. Phase 13 removed six
that never were -- ``SCHEMA_VIOLATION``, ``TOKEN_REUSED``,
``ACCOUNT_NOT_VERIFIED``, ``ITEM_ALREADY_CLAIMED``, ``TAG_CODE_TAKEN`` and
``PINNING_UNAVAILABLE`` -- because no code path raised them and no client could
ever have branched on them. A documented code that nothing produces is worse
than an absent one: a frontend writes a handler for it and that handler is dead
the day it is written. Every remaining member is raised somewhere in ``app/``
and triggered by at least one test; ``docs/API_CONTRACT.md`` carries the map.

Note the name collision: :class:`ValidationError` below is *this* project's
error, not :class:`pydantic.ValidationError`. Modules needing both alias
pydantic's at the import site.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

__all__ = [
    "AppError",
    "AuthError",
    "ConflictError",
    "ErrorCode",
    "ForbiddenError",
    "InsufficientStorageError",
    "NotFoundError",
    "RateLimitError",
    "UnavailableError",
    "ValidationError",
]


class ErrorCode(StrEnum):
    """Frozen, append-only registry of every error code the API emits."""

    # --- 400 / 422 validation ---
    VALIDATION_FAILED = "VALIDATION_FAILED"
    INVALID_TAG_CODE = "INVALID_TAG_CODE"
    INVALID_CURSOR = "INVALID_CURSOR"
    # Two distinct failures, deliberately separate codes: a bad *schema*
    # is an operator error at category creation; bad *attributes* are a
    # weaver error at item registration. Collapsing them would send the
    # wrong person looking at the wrong document.
    INVALID_CATEGORY_SCHEMA = "INVALID_CATEGORY_SCHEMA"
    ATTRIBUTE_VALIDATION_FAILED = "ATTRIBUTE_VALIDATION_FAILED"
    QUANTITY_UNIT_MISMATCH = "QUANTITY_UNIT_MISMATCH"
    MAX_DEPTH_EXCEEDED = "MAX_DEPTH_EXCEEDED"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    MEDIA_TOO_LARGE = "MEDIA_TOO_LARGE"
    # A tag binds a physical label to a record. Refusing to bind one to a record
    # whose anchor failed is not the same failure as refusing a malformed
    # request, and an operator staring at a printer needs to know which it was.
    TAG_NOT_ISSUABLE = "TAG_NOT_ISSUABLE"
    BULK_TOO_LARGE = "BULK_TOO_LARGE"

    # --- 401 / 403 ---
    UNAUTHENTICATED = "UNAUTHENTICATED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    TOKEN_INVALID = "TOKEN_INVALID"
    # Refresh tokens are opaque, not JWTs, and fail for different reasons
    # than an access token does. A client distinguishes "log in again" from
    # "your session was stolen" on these.
    INVALID_REFRESH_TOKEN = "INVALID_REFRESH_TOKEN"
    REFRESH_TOKEN_EXPIRED = "REFRESH_TOKEN_EXPIRED"
    REFRESH_TOKEN_REUSED = "REFRESH_TOKEN_REUSED"
    PENDING_TOKEN_CONSUMED = "PENDING_TOKEN_CONSUMED"
    ACCOUNT_SUSPENDED = "ACCOUNT_SUSPENDED"
    FORBIDDEN = "FORBIDDEN"
    INSUFFICIENT_ROLE = "INSUFFICIENT_ROLE"
    CATEGORY_RETIRED = "CATEGORY_RETIRED"
    ROLE_NOT_SELF_ASSIGNABLE = "ROLE_NOT_SELF_ASSIGNABLE"
    # A fraud-flagged actor may not attest. 403 rather than 409: the request is
    # well formed and collides with nothing, the caller is simply not permitted.
    ACTOR_FRAUD_FLAGGED = "ACTOR_FRAUD_FLAGGED"
    # OAuth callback failures. State is 400 (the request is malformed or
    # replayed); an unverified provider email is 400 because the request can
    # never succeed, not 401, since nothing was wrong with the credential.
    OAUTH_STATE_INVALID = "OAUTH_STATE_INVALID"
    PROVIDER_EMAIL_UNVERIFIED = "PROVIDER_EMAIL_UNVERIFIED"

    # --- 404 ---
    NOT_FOUND = "NOT_FOUND"
    ITEM_NOT_FOUND = "ITEM_NOT_FOUND"
    USER_NOT_FOUND = "USER_NOT_FOUND"
    CATEGORY_NOT_FOUND = "CATEGORY_NOT_FOUND"
    CATEGORY_VERSION_NOT_FOUND = "CATEGORY_VERSION_NOT_FOUND"

    # --- 409 ---
    CONFLICT = "CONFLICT"
    EMAIL_ALREADY_REGISTERED = "EMAIL_ALREADY_REGISTERED"
    OAUTH_IDENTITY_LINKED = "OAUTH_IDENTITY_LINKED"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    # A split that would allocate more than the parent holds. 409 rather
    # than 422: the request is well-formed, it collides with state.
    MASS_BALANCE_EXCEEDED = "MASS_BALANCE_EXCEEDED"
    DUPLICATE_ATTESTATION = "DUPLICATE_ATTESTATION"
    # Says the *item* already wears a tag, and the response carries it: the
    # caller has nothing to retry, only something to read. A generated code
    # colliding with somebody else's is not this -- `assign_tag_code` retries
    # past those inside a SAVEPOINT and only gives up as
    # TAG_GENERATION_EXHAUSTED, so a collision never reaches a client.
    TAG_ALREADY_ISSUED = "TAG_ALREADY_ISSUED"
    CATEGORY_SLUG_EXISTS = "CATEGORY_SLUG_EXISTS"

    # --- 429 ---
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"

    # --- 507 ---
    # Distinct from QUOTA_EXCEEDED, which is a rate ceiling that clears on its
    # own. This one does not clear with time: bytes already stored stay stored,
    # and a client retrying later gets the same answer until somebody frees
    # space. Telling the two apart is the difference between "wait" and "stop".
    STORAGE_BUDGET_EXCEEDED = "STORAGE_BUDGET_EXCEEDED"

    # --- 500 / 503 ---
    INTERNAL_ERROR = "INTERNAL_ERROR"
    # Every generation attempt collided. At 53 bits of entropy this cannot
    # happen by chance, so it means the generator is broken -- its own code so
    # the failure reads as a system fault rather than as the caller's problem.
    TAG_GENERATION_EXHAUSTED = "TAG_GENERATION_EXHAUSTED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    CHAIN_UNAVAILABLE = "CHAIN_UNAVAILABLE"
    OAUTH_PROVIDER_UNAVAILABLE = "OAUTH_PROVIDER_UNAVAILABLE"


class AppError(Exception):
    """Base class for every error that maps to an HTTP response.

    Anything raised that is *not* an :class:`AppError` is a bug, and the global
    handler turns it into an opaque ``INTERNAL_ERROR``.
    """

    status: int = 500
    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        code: ErrorCode | None = None,
        message: str | None = None,
        status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code or type(self).code
        self.status = status or type(self).status
        self.message = message or self.code.replace("_", " ").lower()
        self.details = details
        super().__init__(self.message)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, status={self.status})"


class ValidationError(AppError):
    """Request understood but semantically invalid."""

    status = 422
    code = ErrorCode.VALIDATION_FAILED


class AuthError(AppError):
    """Caller is not authenticated, or the credential presented is unusable."""

    status = 401
    code = ErrorCode.UNAUTHENTICATED


class ForbiddenError(AppError):
    """Caller is authenticated but not permitted to do this."""

    status = 403
    code = ErrorCode.FORBIDDEN


class NotFoundError(AppError):
    """Addressed resource does not exist, or is not visible to this caller."""

    status = 404
    code = ErrorCode.NOT_FOUND


class ConflictError(AppError):
    """Request collides with existing state."""

    status = 409
    code = ErrorCode.CONFLICT


class RateLimitError(AppError):
    """Caller exceeded a rate limit or a quota.

    ``details['retry_after']`` carries whole seconds and the handler mirrors it
    into the ``Retry-After`` response header.
    """

    status = 429
    code = ErrorCode.RATE_LIMITED

    def __init__(
        self,
        retry_after: int,
        code: ErrorCode | None = None,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged = {"retry_after": int(retry_after), **(details or {})}
        super().__init__(code=code, message=message, details=merged)
        self.retry_after = int(retry_after)


class UnavailableError(AppError):
    """A dependency this request needed is degraded or down."""

    status = 503
    code = ErrorCode.SERVICE_UNAVAILABLE


class InsufficientStorageError(AppError):
    """No room left in a storage budget. 507, and it will not clear by itself.

    Deliberately not 429. A rate limit says "come back shortly"; this says "the
    space is gone until somebody frees it", and a client that retries on a
    backoff because it read 429 would hammer an endpoint that cannot succeed.
    """

    status = 507
    code = ErrorCode.STORAGE_BUDGET_EXCEEDED
