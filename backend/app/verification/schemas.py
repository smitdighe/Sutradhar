"""The public payload. Every field here is readable by anyone with a tag code.

**The allowlist is the design.** These models are hand-written projections, not
``from_attributes`` views over ORM rows, and that is deliberate: a serialiser
that reflects a model grows a field the moment somebody adds a column, and the
first time that happens on the one unauthenticated surface in the system, it
happens in public. Nothing reaches a reader here unless a field below names it.

**No identifiers of people, and no internal identifiers either.** No email, no
phone, no address, no legal name, no user id, no item id. The maker appears as a
display handle they chose and a state they work in, and only while they have not
withdrawn it. Internal ids are absent for a different reason: they are not
private, but publishing them turns one tag code into a way to walk the item
graph.

**Nothing here is a verdict.** The chain block reports whether a recomputed hash
matches what was anchored. The trust block reports who vouched. The scan block
reports a pattern. None of them says anything about whether an object is what it
claims to be, because none of them can know that.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.clock import UtcDatetime

__all__ = [
    "CategoryView",
    "ChainView",
    "ClaimBlock",
    "InclusionProofView",
    "MediaView",
    "ProvenanceView",
    "PublicEventView",
    "PublicItemView",
    "ScanBlock",
    "ScanRequest",
    "StoryView",
    "TreeStepView",
    "TrustView",
    "VerificationResult",
]


class VerificationResult(StrEnum):
    """The outcome of recomputing the hash and comparing it to the chain."""

    # The hash recomputed from the database is the hash the chain carries.
    MATCH = "MATCH"
    # The database no longer produces the hash that was anchored. Something in
    # the record changed after it was written.
    MISMATCH = "MISMATCH"
    # Nothing has been anchored for this item yet, so there is nothing to
    # compare against. Not a failure -- the ordinary state of a new record, and
    # the ordinary state of the whole system while no registry is deployed.
    UNANCHORED = "UNANCHORED"


class CategoryView(BaseModel):
    """The GI category, as a reader would name it."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    display_name: str
    schema_version: int


class InclusionProofView(BaseModel):
    """A batched item's proof that its hash is under the anchored root.

    Published because it is checkable without this service: a reader with the
    root, the leaf and these siblings can run the same computation offline and
    does not have to take the answer on trust.
    """

    model_config = ConfigDict(extra="forbid")

    root: str
    leaf_index: int
    leaf_count: int
    proof: list[str]


class ChainView(BaseModel):
    """What the chain says, when it was asked, and whether the answer is fresh."""

    model_config = ConfigDict(extra="forbid")

    status: str
    tx_hash: str | None = None
    block_number: int | None = None
    confirmations: int = 0
    anchored_at: UtcDatetime | None = None
    verification: VerificationResult
    # True when this answer came from the indexed mirror rather than from a live
    # call. Cached data presented as live would be the one dishonest field in
    # the payload, so it is labelled instead.
    stale: bool
    chain_checked_at: UtcDatetime
    inclusion_proof: InclusionProofView | None = None


class TrustView(BaseModel):
    """Who vouched, in what capacity, and how many times. Not a score."""

    model_config = ConfigDict(extra="forbid")

    level: str
    contributing_roles: list[str] = Field(default_factory=list)
    attestation_count: int = 0
    disputed: bool = False


class TreeStepView(BaseModel):
    """One step of the lineage, without naming the item it refers to.

    Depth and quantity are what a reader needs -- "this two-metre piece was cut
    from a twelve-metre bolt". The ancestor's id and its own tag code are left
    out: they identify other objects, which are not this reader's business.
    """

    model_config = ConfigDict(extra="forbid")

    depth: int
    quantity: Decimal
    quantity_unit: str
    status: str


class PublicEventView(BaseModel):
    """One provenance event, reduced to its type and its timing.

    Payloads are not published. They carry preimages and hashes that belong in
    an audit, not on a shop floor, and an event log that publishes its payloads
    is a slow leak waiting for somebody to add a field to one.
    """

    model_config = ConfigDict(extra="forbid")

    type: str
    at: UtcDatetime
    tx_hash: str | None = None
    block_number: int | None = None


class ProvenanceView(BaseModel):
    """Lineage and history."""

    model_config = ConfigDict(extra="forbid")

    ancestry: list[TreeStepView] = Field(default_factory=list)
    events: list[PublicEventView] = Field(default_factory=list)
    child_count: int = 0


class MediaView(BaseModel):
    """One piece of media, addressed by content.

    ``gateway_url`` is null until the file is pinned. The digest is published
    either way: it is the integrity claim, and it is true whether or not
    anybody is currently hosting the bytes.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str
    sha256: str
    cid: str | None = None
    gateway_url: str | None = None


class StoryView(BaseModel):
    """The maker's side of the record, as far as they have allowed."""

    model_config = ConfigDict(extra="forbid")

    weaver_display_name: str | None = None
    region: str | None = None
    # True when the maker has withdrawn from the public page. The fields above
    # are then null, and saying why is better than leaving a reader to wonder
    # whether the record is incomplete.
    maker_opted_out: bool = False
    media: list[MediaView] = Field(default_factory=list)


class ScanBlock(BaseModel):
    """How often this tag has been scanned, and what that pattern looks like."""

    model_config = ConfigDict(extra="forbid")

    count: int
    suspicion_level: str
    reason: str | None = None
    signals: list[str] = Field(default_factory=list)


class ClaimBlock(BaseModel):
    """First-scan-wins ownership, stated as fact."""

    model_config = ConfigDict(extra="forbid")

    status: str
    claimed: bool
    claimed_at: UtcDatetime | None = None
    is_your_claim: bool = False
    claimed_region: str | None = None
    message: str | None = None


class PublicItemView(BaseModel):
    """Everything a scan of one tag returns."""

    model_config = ConfigDict(extra="forbid")

    tag_code: str
    display_code: str
    category: CategoryView
    attributes: dict[str, Any] = Field(default_factory=dict)
    quantity: Decimal
    quantity_unit: str
    trust: TrustView
    chain: ChainView
    provenance: ProvenanceView
    story: StoryView
    scan: ScanBlock
    claim: ClaimBlock


class ScanRequest(BaseModel):
    """The optional body of a scan.

    Both fields are hints. The fingerprint is an opaque string the client
    chooses and this service only ever stores hashed; the region is consulted
    only when the edge that terminated the connection said nothing.
    """

    model_config = ConfigDict(extra="forbid")

    device_fingerprint: str | None = Field(default=None, max_length=256)
    region_code: str | None = Field(default=None, max_length=8)
