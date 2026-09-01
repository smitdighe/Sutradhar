"""Request and response models for media.

The resolution payload carries *every* readable tier rather than one URL. A
frontend that gets a single link and finds it dead has to come back and ask
again, at exactly the moment the API is least likely to help -- and the tier most
likely to be dead is the one a CDN serves, which is the one a browser hits
first. Handing over the whole fallback chain up front means a failed image load
retries client-side, with no round trip.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict

from app.core.clock import UtcDatetime
from app.db.models.enums import MediaKind, PinStatus
from app.media.mirror import Tier

__all__ = [
    "AttachMediaRequest",
    "ItemMediaResponse",
    "MediaDetail",
    "MediaSummary",
    "TierResponse",
]


class TierResponse(BaseModel):
    """One place these bytes can be fetched from."""

    tier: Tier
    url: str
    # False for IPFS (a pin is only as durable as whoever pays for it) and for
    # the mirror (wiped by every redeploy on an ephemeral filesystem). Exposed
    # so a caller can tell three copies from three *durable* copies.
    durable: bool


class MediaSummary(BaseModel):
    """A media row, without its bytes."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sha256: str
    byte_size: int
    content_type: str
    # Null until a pin succeeds, and that is a normal steady state, not an
    # error: the SHA-256 above is the integrity proof and it does not need one.
    cid: str | None
    pin_status: PinStatus
    created_at: UtcDatetime


class MediaDetail(MediaSummary):
    """A media row plus every way to read it."""

    tiers: list[TierResponse]
    # The first entry of `tiers`, repeated for callers that only want one.
    primary_tier: Tier | None = None
    # True when a copy exists that survives a redeploy. False means the file is
    # living on borrowed time in a cache and a third-party pin.
    durable: bool = False


class AttachMediaRequest(BaseModel):
    """Link an already-uploaded file to an item."""

    model_config = ConfigDict(extra="forbid")

    media_id: uuid.UUID
    # WEAVE_MACRO is stored for the textile-fingerprinting roadmap: a macro shot
    # of the weave is what a future matcher would compare against. Nothing in
    # this phase does that -- the kind is recorded so the corpus exists when
    # something can use it.
    kind: MediaKind


class ItemMediaResponse(BaseModel):
    """One media file as it appears on an item."""

    media: MediaSummary
    kind: MediaKind
