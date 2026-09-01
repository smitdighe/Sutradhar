"""Password hashing and policy.

argon2id, with parameters from config. bcrypt is not used: it silently truncates
at 72 bytes and has no memory-hardness, so a GPU farm attacks it far more
cheaply. A bare SHA-256 of a password is not hashing at all.

A server-side pepper from ``PASSWORD_PEPPER`` is mixed in before hashing. The
pepper lives in the environment and never in the database, so a stolen database
dump alone does not permit an offline dictionary attack -- the attacker needs
the application host as well.

Timing: :func:`verify_password` is called on the login path even when no such
user exists, against :data:`DUMMY_HASH`. Skipping the hash for an unknown email
would make "no such account" measurably faster than "wrong password", which
turns login into a user-enumeration oracle.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

from app.config import get_settings
from app.core.errors import ErrorCode, ValidationError

__all__ = [
    "DUMMY_HASH",
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "hash_password",
    "needs_rehash",
    "validate_password_policy",
    "verify_password",
]

MIN_PASSWORD_LENGTH = 12
# argon2 cost is a function of input size as well as parameters, so an
# unbounded password is a cheap way to make the server do expensive work.
MAX_PASSWORD_LENGTH = 128


def _hasher() -> PasswordHasher:
    settings = get_settings()
    return PasswordHasher(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_cost_kib,
        parallelism=settings.argon2_parallelism,
        type=Type.ID,
    )


def _peppered(password: str) -> bytes:
    return (password + get_settings().password_pepper).encode("utf-8")


def hash_password(password: str) -> str:
    """Return an argon2id hash of *password* with the configured parameters."""
    return _hasher().hash(_peppered(password))


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-work verification. Returns False rather than raising."""
    try:
        return _hasher().verify(password_hash, _peppered(password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when *password_hash* was made with weaker parameters than current."""
    try:
        return _hasher().check_needs_rehash(password_hash)
    except InvalidHashError:
        # Unparseable hash: treat as stale so a successful login replaces it.
        return True


def validate_password_policy(password: str, email: str | None = None) -> None:
    """Enforce length, and reject a password equal to the email local-part.

    Length and that one obvious reuse, nothing more. Composition rules
    ("one uppercase, one digit, one symbol") push people toward
    ``Password1!`` and measurably reduce entropy.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(
            code=ErrorCode.VALIDATION_FAILED,
            message=f"password must be at least {MIN_PASSWORD_LENGTH} characters",
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValidationError(
            code=ErrorCode.VALIDATION_FAILED,
            message=f"password must be at most {MAX_PASSWORD_LENGTH} characters",
        )
    if email:
        local_part = email.split("@", 1)[0]
        if local_part and password.casefold() == local_part.casefold():
            raise ValidationError(
                code=ErrorCode.VALIDATION_FAILED,
                message="password must not be the same as the email local-part",
            )


# Generated once at import against a value nobody can present, so the
# unknown-email path spends the same argon2 work as the wrong-password path.
DUMMY_HASH: str = hash_password("sutradhar-timing-equaliser-never-a-real-password")
