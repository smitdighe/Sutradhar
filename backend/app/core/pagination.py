"""Signed opaque keyset cursors.

Every collection in this API paginates on ``(created_at DESC, id DESC)`` using
a keyset, never ``OFFSET``. ``OFFSET`` makes the database walk and discard every
skipped row, so page 500 of the scans table costs 500 pages of work, and a row
inserted mid-scroll shifts every subsequent page. Keyset pagination is O(1) per
page and stable under concurrent inserts.

There is deliberately **no total count**. ``COUNT(*)`` on Postgres is a full
scan, and the scan table is the one that grows without bound.

The cursor is base64url of ``{"k": <sort key>, "id": <uuid>}``, HMAC-SHA256
signed with ``CURSOR_SECRET``. Signing is not about confidentiality -- the
contents are boring -- it is so a client cannot hand-craft a cursor to probe
for rows or to inject a value into the WHERE clause. A tampered cursor is
rejected rather than silently coerced.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.config import get_settings
from app.core.clock import from_rfc3339, to_rfc3339
from app.core.errors import ErrorCode, ValidationError

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "Cursor",
    "clamp_limit",
    "decode_cursor",
    "encode_cursor",
]

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
_SIGNATURE_BYTES = 16


@dataclass(frozen=True, slots=True)
class Cursor:
    """Decoded keyset position: the sort key and the tie-breaking row id."""

    key: Any
    id: UUID


def clamp_limit(limit: int | None) -> int:
    """Clamp a client-supplied limit into range, silently.

    Silently rather than with a 422: a client asking for 1000 rows wants as
    many as it can get, and failing the request teaches it nothing useful.
    """
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(payload: bytes) -> bytes:
    secret = get_settings().cursor_secret.encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).digest()[:_SIGNATURE_BYTES]


def _encode_key(key: Any) -> Any:
    if isinstance(key, datetime):
        return to_rfc3339(key)
    if isinstance(key, UUID):
        return str(key)
    return key


def encode_cursor(sort_key: Any, row_id: UUID | str) -> str:
    """Serialise a keyset position into a signed opaque cursor."""
    payload = json.dumps(
        {"k": _encode_key(sort_key), "id": str(row_id)},
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    return _b64encode(_sign(payload) + payload)


def decode_cursor(cursor: str, key_is_datetime: bool = True) -> Cursor:
    """Verify and decode a cursor.

    Raises :class:`~app.core.errors.ValidationError` with ``INVALID_CURSOR`` for
    anything malformed, truncated, or wrongly signed -- all indistinguishable
    to the caller, so a forged cursor leaks nothing about why it failed.
    """
    invalid = ValidationError(code=ErrorCode.INVALID_CURSOR, message="cursor is not valid")
    try:
        raw = _b64decode(cursor)
    except (ValueError, TypeError) as exc:
        raise invalid from exc
    if len(raw) <= _SIGNATURE_BYTES:
        raise invalid

    signature, payload = raw[:_SIGNATURE_BYTES], raw[_SIGNATURE_BYTES:]
    if not hmac.compare_digest(signature, _sign(payload)):
        raise invalid

    try:
        decoded = json.loads(payload.decode("utf-8"))
        key = decoded["k"]
        row_id = UUID(decoded["id"])
        if key_is_datetime and isinstance(key, str):
            key = from_rfc3339(key)
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise invalid from exc

    return Cursor(key=key, id=row_id)
