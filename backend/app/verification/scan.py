"""Turning one public HTTP request into one row in ``scans``.

Three things happen here and nowhere else: deciding *where* a scan came from,
deciding *which device* it came from, and deciding whether it is a new scan at
all.

**Location is coarse and second-hand.** The country and subdivision come from
the edge that terminated the connection -- Vercel, Cloudflare and Render all
derive them from the request address and pass them down as headers -- or from a
region the caller states outright. This service never looks at a geo database
and never calls one, so a scan cannot leak a scanner's address to a third party
and an outage at a geo vendor cannot take the public page down. The finest
granularity accepted anywhere in this file is a state.

**The address itself is never kept.** It is hashed with the deployment pepper
before it touches a column. IPv4 is only four billion values, so an unpeppered
digest of an address is a lookup table away from being the address again; the
pepper is what makes the stored value useless to anybody who takes the database.

**A device fingerprint is supplied, or approximated, never demanded.** The
client sends an opaque string. When it does not, one is derived from the user
agent, the accept-language header and the hashed address -- weaker on purpose,
and the response says which was used, because a claim bound to an approximation
should not be presented as though it were bound to a device.

**A retried request is not a second scan.** Same object, same device, same
place, same network, inside the dedupe window: the existing row is returned
untouched. Without that, one shopper double-tapping a button walks the volume
signal up on their own.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from app.config import Settings, get_settings
from app.core.clock import now
from app.core.hashing import sha256_hex
from app.db.models.scan import Scan
from app.verification.anomaly import AnomalyVerdict, assess_scans, normalise_region

__all__ = [
    "COUNTRY_HEADERS",
    "MAX_FINGERPRINT_CHARS",
    "REGION_HEADERS",
    "ScanContext",
    "build_context",
    "record_scan",
]

# Checked in order. Every one of these is set by an edge that saw the real
# connection; none of them is something a browser can choose for itself when the
# deployment sits behind that edge. `X-Geo-*` is the generic pair for a
# reverse proxy that is configured by hand.
COUNTRY_HEADERS = (
    "x-vercel-ip-country",
    "cf-ipcountry",
    "x-geo-country",
)
REGION_HEADERS = (
    "x-vercel-ip-country-region",
    "cf-region-code",
    "x-geo-region",
)

# Long enough for any real fingerprinting library's digest, short enough that
# the column is not a place to park a payload.
MAX_FINGERPRINT_CHARS = 256


@dataclass(frozen=True, slots=True)
class ScanContext:
    """Everything about a scan that is safe to keep, and nothing else."""

    ip_hash: str | None
    country_code: str | None
    region_code: str | None
    # sha256 of whatever identified the device. The raw value is never stored.
    fingerprint_hash: str | None
    # "client" when the caller supplied one, "derived" when this service
    # approximated it, "none" when there was nothing to work with.
    fingerprint_source: str


def _hashed(value: str, settings: Settings) -> str:
    return sha256_hex((settings.identity_hash_pepper + value).encode("utf-8"))


def _header(request: Request, names: tuple[str, ...]) -> str | None:
    for name in names:
        value = request.headers.get(name)
        if value and value.strip():
            return value.strip()
    return None


def build_context(
    request: Request,
    *,
    device_fingerprint: str | None = None,
    region_code: str | None = None,
    settings: Settings | None = None,
) -> ScanContext:
    """Reduce a request to the four values a scan row is allowed to hold.

    ``region_code`` is the body's own declaration and is consulted only when the
    edge said nothing -- a header set by the infrastructure that saw the
    connection outranks a value the caller typed.
    """
    config = settings or get_settings()

    client = request.client
    ip_hash = _hashed(client.host, config) if client is not None else None

    country = _header(request, COUNTRY_HEADERS)
    region = normalise_region(country, _header(request, REGION_HEADERS))
    if region is None and region_code:
        region = normalise_region(country, region_code)
        if region is None:
            # A fully qualified code in the body needs no country to make sense.
            region = normalise_region(region_code.split("-", 1)[0], region_code)
    if country is None and region is not None and "-" in region:
        country = region.split("-", 1)[0]
    country = country[:2].upper() if country else None

    supplied = (device_fingerprint or "").strip()[:MAX_FINGERPRINT_CHARS]
    if supplied:
        return ScanContext(
            ip_hash=ip_hash,
            country_code=country,
            region_code=region,
            fingerprint_hash=_hashed(supplied, config),
            fingerprint_source="client",
        )

    # The weak fallback. Two phones on one network running the same browser
    # collapse into one identity here, which is exactly why the source is
    # reported alongside it rather than hidden.
    material = "|".join(
        (
            request.headers.get("user-agent", ""),
            request.headers.get("accept-language", ""),
            ip_hash or "",
        )
    )
    if not material.strip("|"):
        return ScanContext(
            ip_hash=ip_hash,
            country_code=country,
            region_code=region,
            fingerprint_hash=None,
            fingerprint_source="none",
        )
    return ScanContext(
        ip_hash=ip_hash,
        country_code=country,
        region_code=region,
        fingerprint_hash=_hashed(material, config),
        fingerprint_source="derived",
    )


def _matches(column: InstrumentedAttribute[str | None], value: str | None) -> ColumnElement[bool]:
    """``column = value``, or ``column IS NULL`` when there is no value.

    Written out because ``= NULL`` is never true in SQL, so the three nullable
    parts of a scan identity would each silently fail to match a row that is
    identical in every respect.
    """
    return column.is_(None) if value is None else column == value


async def _recent_duplicate(
    session: AsyncSession, item_id: uuid.UUID, context: ScanContext, window: timedelta
) -> Scan | None:
    """The same scan, already recorded, inside the window."""
    cutoff = now() - window
    return (
        await session.execute(
            select(Scan)
            .where(
                Scan.item_id == item_id,
                Scan.created_at >= cutoff,
                _matches(Scan.ip_hash, context.ip_hash),
                _matches(Scan.device_fingerprint, context.fingerprint_hash),
                _matches(Scan.region_code, context.region_code),
            )
            .order_by(Scan.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def record_scan(
    session: AsyncSession,
    item_id: uuid.UUID,
    tag_code: str,
    context: ScanContext,
    settings: Settings | None = None,
) -> tuple[Scan, bool, AnomalyVerdict]:
    """Record one scan and score the tag's whole history. Caller commits.

    Returns ``(scan, recorded, verdict)`` -- ``recorded`` is False when this
    request was a replay of one already in the table. The verdict is returned
    on both paths so the caller building a response does not reload the same
    scan rows to reach the answer this function already has.

    The row is inserted *before* the history is scored, deliberately: the scan
    being made right now is part of the pattern, and a verdict computed without
    it would always be one scan behind the thing it is meant to notice.
    """
    config = settings or get_settings()
    window = timedelta(seconds=config.scan_dedupe_window_seconds)

    existing = await _recent_duplicate(session, item_id, context, window)
    if existing is not None:
        # A replay writes nothing, but the caller still needs the current
        # verdict, and the stored columns on the old row carry a level without
        # the signal codes a response reports.
        return existing, False, await assess_scans(session, item_id, config)

    scan = Scan(
        item_id=item_id,
        tag_code=tag_code,
        country_code=context.country_code,
        region_code=context.region_code,
        device_fingerprint=context.fingerprint_hash,
        ip_hash=context.ip_hash,
    )
    session.add(scan)
    await session.flush()

    verdict = await assess_scans(session, item_id, config)
    scan.suspicion_level = verdict.level
    scan.reason = verdict.reason
    await session.flush()
    return scan, True, verdict
