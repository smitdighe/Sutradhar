"""The single source of current time for the entire codebase.

Nothing outside this module may call :func:`datetime.now` or
``datetime.utcnow``. Tests monkeypatch :func:`now` and nothing else, which is
only sound while that rule holds — a CI grep enforces it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from pydantic import PlainSerializer, WithJsonSchema

__all__ = ["UtcDatetime", "from_rfc3339", "now", "to_rfc3339"]

RFC3339_FRACTIONAL_DIGITS = 6


def now() -> datetime:
    """Return the current instant as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def to_rfc3339(dt: datetime) -> str:
    """Render *dt* as RFC 3339 UTC with exactly six fractional digits.

    Naive datetimes are assumed to already be UTC; aware ones are converted.
    The output always ends in ``Z``, never ``+00:00``::

        2026-08-26T11:04:22.481920Z
    """
    aware = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return aware.strftime("%Y-%m-%dT%H:%M:%S.") + f"{aware.microsecond:06d}Z"


def from_rfc3339(value: str) -> datetime:
    """Parse an RFC 3339 timestamp into a timezone-aware UTC datetime."""
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def seconds_from_now(seconds: int) -> datetime:
    """Return the UTC instant *seconds* in the future, per :func:`now`."""
    return now() + timedelta(seconds=seconds)


# Every ``datetime`` on a response model is annotated with this, so one rule
# governs the whole wire format.
#
# Pydantic's own JSON rendering of a datetime is *nearly* the same string and
# differs in exactly one place: it omits the fractional part when the instant
# lands on a whole second. That is rare and therefore worse than common --
# ``quota_usage.period_start`` is pinned to the Unix epoch and hits it on every
# request, while every other timestamp in the system comes from a clock and hits
# it about one time in a million. A frontend that only ever sees the six-digit
# form in testing and meets the other form in production has a parser bug nobody
# wrote a test for. Fixed here rather than documented as a caveat, so
# ``docs/API_CONTRACT.md`` can state one format and be exactly right.
#
# ``WithJsonSchema`` is not decoration. ``PlainSerializer(return_type=str)``
# rewrites the generated schema to a bare ``{"type": "string"}`` and drops the
# ``format: date-time`` that was there before, so a client generated from
# ``/openapi.json`` would type every timestamp as a plain string. The annotation
# below puts the format back, and the format is the truth: these are RFC 3339.
UtcDatetime = Annotated[
    datetime,
    PlainSerializer(to_rfc3339, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "format": "date-time"}),
]
