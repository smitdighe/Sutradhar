"""Response shapes.

A single resource is returned bare -- there is no ``{"success": true, "data":
...}`` wrapper. The HTTP status line already carries success or failure, and a
wrapper only adds a level of indirection every client has to unwrap.

Collections are wrapped, because they need somewhere to put pagination state:

    {"data": [...], "pagination": {"next_cursor": "...", "limit": 20}}

Errors use the envelope in :mod:`app.core.error_handlers`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, TypedDict

from app.core.pagination import DEFAULT_LIMIT, encode_cursor

__all__ = ["Page", "Pagination", "paginated"]


class Pagination(TypedDict):
    """Keyset pagination state. No total count -- see :mod:`app.core.pagination`."""

    next_cursor: str | None
    limit: int


class Page(TypedDict):
    """A collection response."""

    data: list[Any]
    pagination: Pagination


def paginated[T](
    items: Sequence[T],
    limit: int = DEFAULT_LIMIT,
    cursor_fn: Callable[[T], str] | None = None,
) -> Page:
    """Wrap *items* in a collection envelope.

    *cursor_fn* maps the last item to its opaque cursor. It is omitted when the
    page is short, because a short page is the end of the collection and a
    ``next_cursor`` there would invite a pointless extra round trip.
    """
    rows = list(items)
    next_cursor: str | None = None
    if cursor_fn is not None and len(rows) == limit and rows:
        next_cursor = cursor_fn(rows[-1])
    return Page(data=list(rows), pagination=Pagination(next_cursor=next_cursor, limit=limit))


def cursor_for(sort_key: Any, row_id: Any) -> str:
    """Convenience wrapper around :func:`app.core.pagination.encode_cursor`."""
    return encode_cursor(sort_key, row_id)
