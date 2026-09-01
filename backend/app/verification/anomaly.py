"""Scan-pattern signals, computed from the ``scans`` table and nothing else.

**What this exists to catch.** A tag is a printed number. Anyone who owns one
real object can photograph its tag and reprint it, and every reprint scans
exactly like the original because it *is* the original number. Peeling a label
off one object and sticking it on another is the same problem with no printing
at all: the tag is unaltered, only the object under it changed. Cryptography
cannot see either attack -- the code is correct in both -- so the only remaining
evidence is the *pattern of scans*, which is what this module reads.

**Rules, not a model.** Four thresholds, all from the environment, all
explainable in one sentence to somebody who asks why a tag was flagged. A
classifier would score better on paper and would be unable to answer that
question, which in a room with a regulator is the only thing that matters.

**Nothing here blocks anything.** Every output is a level and a sentence. A
saree bought in Gujarat and carried to Assam is an ordinary gift, and a system
that refused to show its provenance because of that would be wrong far more
often than it was right. The signals are shown to a reader who can weigh them.

**Nothing here identifies anybody.** The inputs are a coarse region code, a
hashed device fingerprint and a hashed network address. There is no GPS, no
city, no coordinates finer than a state centroid, and no raw address anywhere
in the table these read from.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.clock import now
from app.db.models.enums import SuspicionLevel
from app.db.models.scan import Scan

__all__ = [
    "REGION_CENTROIDS",
    "AnomalyVerdict",
    "Signal",
    "SignalCode",
    "assess_scans",
    "centroid_for",
    "evaluate",
    "haversine_km",
    "normalise_region",
]

_DATA = Path(__file__).resolve().parent / "data" / "in_region_centroids.json"
_TABLE = json.loads(_DATA.read_text(encoding="utf-8"))

# ISO 3166-2 subdivision -> (latitude, longitude). See the data file's own
# comment for why these are state centroids and not anything finer.
REGION_CENTROIDS: dict[str, tuple[float, float]] = {
    code: (float(point[0]), float(point[1])) for code, point in _TABLE["regions"].items()
}
COUNTRY_CENTROIDS: dict[str, tuple[float, float]] = {
    code: (float(point[0]), float(point[1])) for code, point in _TABLE["country"].items()
}

EARTH_RADIUS_KM = 6371.0088


class SignalCode(StrEnum):
    """The four rules. Each one is a sentence, and each one is testable alone."""

    GEOGRAPHIC_SPREAD = "GEOGRAPHIC_SPREAD"
    IMPOSSIBLE_VELOCITY = "IMPOSSIBLE_VELOCITY"
    VOLUME = "VOLUME"
    DEVICE_DIVERSITY = "DEVICE_DIVERSITY"


@dataclass(frozen=True, slots=True)
class Signal:
    """One rule that fired, with the numbers that made it fire."""

    code: SignalCode
    # Written for a person holding the object, not for an operator reading logs.
    detail: str


@dataclass(frozen=True, slots=True)
class AnomalyVerdict:
    """The level, the rules behind it, one readable sentence, and the count.

    ``scan_count`` travels with the verdict because it is already known: every
    rule here is computed over the whole scan history, so the number of scans
    was in hand the moment the verdict was. A caller that needed it separately
    would issue a ``SELECT count(*)`` over exactly the rows this was derived
    from.
    """

    level: SuspicionLevel
    signals: tuple[Signal, ...]
    scan_count: int = 0

    @property
    def reason(self) -> str | None:
        """The signals joined into something a shopper can read, or None."""
        if not self.signals:
            return None
        return " ".join(signal.detail for signal in self.signals)

    @property
    def codes(self) -> tuple[SignalCode, ...]:
        return tuple(signal.code for signal in self.signals)


# ---------------------------------------------------------------- geography


def normalise_region(country_code: str | None, region_code: str | None) -> str | None:
    """Fold a header value into a canonical ``IN-GJ`` style subdivision code.

    Edge platforms send the subdivision on its own (``GJ``) alongside the
    country, so the two are recombined here rather than at four call sites.
    An unrecognised code is kept as-is if it is already prefixed, and dropped
    otherwise: storing ``GJ`` with no country would make two different places
    in two different countries compare equal.
    """
    if not region_code:
        return None
    region = region_code.strip().upper().replace("_", "-")
    if not region:
        return None
    if "-" in region:
        return region[:8]
    country = (country_code or "").strip().upper()
    if not country:
        return None
    return f"{country}-{region}"[:8]


def centroid_for(region_code: str | None) -> tuple[float, float] | None:
    """Look up a region's centroid, falling back to the country's own point."""
    if not region_code:
        return None
    known = REGION_CENTROIDS.get(region_code)
    if known is not None:
        return known
    country = region_code.split("-", 1)[0]
    return COUNTRY_CENTROIDS.get(country)


def haversine_km(first: tuple[float, float], second: tuple[float, float]) -> float:
    """Great-circle distance in kilometres between two lat/lon pairs."""
    lat1, lon1 = math.radians(first[0]), math.radians(first[1])
    lat2, lon2 = math.radians(second[0]), math.radians(second[1])
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1
    inner = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(inner))


# ---------------------------------------------------------------- the rules


def _plural(count: int, noun: str) -> str:
    """``1 minute`` / ``3 minutes``. This sentence is read by shoppers."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _spread(scans: list[Scan], limit: int) -> Signal | None:
    regions = {scan.region_code for scan in scans if scan.region_code}
    if len(regions) <= limit:
        return None
    return Signal(
        code=SignalCode.GEOGRAPHIC_SPREAD,
        detail=(
            f"This tag has been scanned in {len(regions)} different regions, "
            f"more than the {limit} this system expects for one object."
        ),
    )


def _velocity(scans: list[Scan], limit_km_per_h: int) -> Signal | None:
    """Fastest implied travel between two consecutive located scans.

    Consecutive rather than all-pairs: two scans a month apart in two states
    say nothing, and the pair that matters is always adjacent in time.
    """
    located: list[tuple[Scan, tuple[float, float]]] = []
    for scan in scans:
        point = centroid_for(scan.region_code)
        if point is not None:
            located.append((scan, point))
    worst: tuple[float, float, float, Scan, Scan] | None = None

    for (earlier, from_point), (later, to_point) in zip(located, located[1:], strict=False):
        if earlier.region_code == later.region_code:
            continue
        distance = haversine_km(from_point, to_point)
        elapsed_seconds = (later.created_at - earlier.created_at).total_seconds()
        # Two places at one instant is not a fast journey but an impossible one,
        # so it is reported at the ceiling rather than divided by zero or
        # quietly skipped.
        speed = (
            float("inf")
            if elapsed_seconds <= 0
            else distance / (elapsed_seconds / 3600.0)
        )
        if speed <= limit_km_per_h:
            continue
        if worst is None or speed > worst[0]:
            worst = (speed, distance, elapsed_seconds, earlier, later)

    if worst is None:
        return None

    speed, distance, elapsed_seconds, earlier, later = worst
    minutes = elapsed_seconds / 60.0
    if minutes >= 1:
        when = f"{_plural(round(minutes), 'minute')} apart"
    elif elapsed_seconds >= 1:
        when = f"{_plural(round(elapsed_seconds), 'second')} apart"
    else:
        # Rounding a sub-second gap down reads as "0 seconds apart", which
        # invites the reader to wonder whether the number is broken.
        when = "less than a second apart"
    speed_text = "instantly" if math.isinf(speed) else f"about {speed:,.0f} km/h"
    return Signal(
        code=SignalCode.IMPOSSIBLE_VELOCITY,
        detail=(
            f"Two scans {round(distance):,} km apart ({earlier.region_code} then "
            f"{later.region_code}) happened {when}, which implies travelling "
            f"{speed_text} -- faster than the {limit_km_per_h:,} km/h this system "
            "treats as possible."
        ),
    )


def _volume(scans: list[Scan], limit: int) -> Signal | None:
    if len(scans) <= limit:
        return None
    return Signal(
        code=SignalCode.VOLUME,
        detail=(
            f"This tag has been scanned {len(scans)} times, more than the {limit} "
            "this system expects for one object."
        ),
    )


def _device_diversity(scans: list[Scan], limit: int, window: timedelta) -> Signal | None:
    cutoff = now() - window
    devices = {
        scan.device_fingerprint
        for scan in scans
        if scan.device_fingerprint and scan.created_at >= cutoff
    }
    if len(devices) <= limit:
        return None
    hours = window.total_seconds() / 3600.0
    span = f"{hours:.0f} hours" if hours >= 1 else f"{window.total_seconds():.0f} seconds"
    return Signal(
        code=SignalCode.DEVICE_DIVERSITY,
        detail=(
            f"{len(devices)} different devices scanned this tag in the last {span}, "
            f"more than the {limit} this system expects for one object."
        ),
    )


def _level(signals: tuple[Signal, ...]) -> SuspicionLevel:
    """NONE, one rule is WATCH, two is SUSPICIOUS -- and velocity alone is too.

    Impossible velocity is promoted on its own because it is the one signal
    that cannot have an innocent reading: the other three describe a tag that
    is being looked at a lot, which a shop window produces honestly. Two places
    at once describes two objects.
    """
    if not signals:
        return SuspicionLevel.NONE
    if len(signals) >= 2 or any(
        signal.code is SignalCode.IMPOSSIBLE_VELOCITY for signal in signals
    ):
        return SuspicionLevel.SUSPICIOUS
    return SuspicionLevel.WATCH


def evaluate(scans: list[Scan], settings: Settings | None = None) -> AnomalyVerdict:
    """Run every rule over one tag's scan history. Pure, synchronous, testable.

    Ordering is by ``created_at`` and is the caller's responsibility for the
    velocity rule to mean anything; :func:`assess_scans` does it in SQL.
    """
    config = settings or get_settings()
    candidates = (
        _spread(scans, config.scan_anomaly_max_regions),
        _velocity(scans, config.scan_anomaly_velocity_km_per_h),
        _volume(scans, config.scan_anomaly_max_scans),
        _device_diversity(
            scans,
            config.scan_anomaly_max_devices,
            timedelta(minutes=config.scan_anomaly_device_window_minutes),
        ),
    )
    signals = tuple(signal for signal in candidates if signal is not None)
    return AnomalyVerdict(level=_level(signals), signals=signals, scan_count=len(scans))


async def assess_scans(
    session: AsyncSession, item_id: uuid.UUID, settings: Settings | None = None
) -> AnomalyVerdict:
    """Load a tag's scan history, oldest first, and evaluate it."""
    scans = list(
        (
            await session.execute(
                select(Scan).where(Scan.item_id == item_id).order_by(Scan.created_at, Scan.id)
            )
        )
        .scalars()
        .all()
    )
    return evaluate(scans, settings)
