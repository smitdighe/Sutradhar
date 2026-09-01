"""Public verification: recompute the hash, ask the chain, report the difference.

**The whole idea, in four steps.**

1. Load the item row out of PostgreSQL.
2. Recompute its hash from that row with the frozen Phase 6 preimage.
3. Read what was anchored for this item -- from the chain when it can be
   reached, from the indexed event mirror when it cannot.
4. Compare.

Everything else in this module is projection. Step 2 against step 3 is the
reason the chain is in this system at all: it means an operator with write
access to the database cannot quietly change a record, because changing any
hashed column changes what step 2 produces and the anchored value does not
move. The response flips to ``MISMATCH`` and says so to the public.

**The anchor is found by item identity, not by the hash being tested.** Looking
it up by ``items.item_hash`` would let one edited column hide another: rewrite
the digest column to match the tampered row and the lookup finds nothing, which
reads as "never anchored" rather than "changed". So the batched path goes
through ``merkle_leaves.item_id`` and the single path through the append-only
``ANCHORED`` event, both keyed by the item, and the stored digest column is only
a last resort.

**Unreachable is a normal answer, not an error.** No registry is deployed and
writes are off, so ``UNANCHORED`` is the ordinary state of this system today and
is served as a 200 with an explanation. When a chain does exist and is briefly
unreachable, the last indexed state is served with ``stale: true`` and the
timestamp it was observed. Neither case is ever a 500: a public page that breaks
when a testnet has a bad afternoon has confused a dependency for a prerequisite.

**Isolation.** Nothing in this package imports the authentication package, the
moderation package, or any authenticated router, and no serialiser is shared
with them -- ``tests/unit/test_verification_isolation.py`` asserts it by reading
the imports, so the boundary is a test rather than a habit. What *is* shared is
the frozen hasher, the trust ladder and the Merkle code: pure derivations over
models, where a second copy would be free to drift and the first symptom of
drift would be the public view and the private view disagreeing about one
object.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.attestation.trust import assess
from app.chain.batching import get_inclusion_proof, verify_inclusion
from app.config import Settings, get_settings
from app.core.clock import now
from app.core.errors import ErrorCode, NotFoundError, ValidationError
from app.core.ids import TAG_CODE_LENGTH, normalize_tag_code, validate_tag_code
from app.db.models.catalog import GICategory, Item, ItemEvent
from app.db.models.chain import ChainEvent, ChainTx, MerkleLeaf
from app.db.models.enums import ItemEventType
from app.db.models.media import ItemMedia, Media
from app.db.models.user import User
from app.provenance.item_hash import hash_item, registrant_hash
from app.provenance.tree import get_ancestry
from app.verification.anomaly import AnomalyVerdict, assess_scans
from app.verification.claiming import ClaimView
from app.verification.schemas import (
    CategoryView,
    ChainView,
    ClaimBlock,
    InclusionProofView,
    MediaView,
    ProvenanceView,
    PublicEventView,
    PublicItemView,
    ScanBlock,
    StoryView,
    TreeStepView,
    TrustView,
    VerificationResult,
)

__all__ = [
    "MAX_ANCESTRY_DEPTH",
    "MAX_PUBLIC_EVENTS",
    "AnchorRecord",
    "ChainReader",
    "build_view",
    "chain_state",
    "load_item_by_tag",
    "public_attributes",
    "recompute_item_hash",
]

# The tree guard already caps registration depth; this is the reader's own
# ceiling so a cycle introduced by hand in the database cannot spin a public
# request forever.
MAX_ANCESTRY_DEPTH = 32

# A public history is a summary, not an export.
MAX_PUBLIC_EVENTS = 50

# Attribute keys that could carry a person rather than a property. Matched as
# substrings against the lowercased key, so `contact_number` and `weaverName`
# are both caught. Category schemas are operator-authored and can grow a field
# at any time; this is the floor under that.
_IDENTIFYING_KEY_PARTS = (
    "aadhaar",
    "aadhar",
    "account",
    "address",
    "contact",
    "dob",
    "email",
    "gst",
    "ifsc",
    "mobile",
    "name",
    "pan_",
    "passport",
    "phone",
    "upi",
    "user",
    "weaver_id",
)

# Provenance events a reader is shown. ANCHORED and REORGED carry chain
# coordinates; the rest are the shape of the object's life. Anything not listed
# -- anything a later phase adds -- is withheld until somebody decides it is
# publishable, which is the safe default for a public surface.
_PUBLIC_EVENT_TYPES = frozenset(
    {
        ItemEventType.REGISTERED,
        ItemEventType.SPLIT,
        ItemEventType.ATTESTED,
        ItemEventType.ANCHORED,
        ItemEventType.REORGED,
        ItemEventType.ANCHOR_FAILED,
        ItemEventType.DISPUTED,
        ItemEventType.DISPUTE_CLEARED,
        ItemEventType.TAG_ISSUED,
        ItemEventType.CLAIMED,
    }
)


class ChainReader(Protocol):
    """The two live reads this module makes. Deliberately tiny.

    A protocol rather than a concrete client so the public path depends on two
    questions, not on a chain package. Anything that can answer them -- a real
    node, the offline fake, nothing at all -- is substitutable.
    """

    async def is_item_anchored(self, item_hash: str) -> bool: ...

    async def is_batch_anchored(self, root: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class AnchorRecord:
    """What this system believes was anchored for one item, and where from."""

    anchored_hash: str
    tx_hash: str | None = None
    block_number: int | None = None
    confirmations: int = 0
    anchored_at: datetime | None = None
    # When the evidence was observed off the chain. None when it came from a
    # source with no observation time of its own.
    observed_at: datetime | None = None
    root: str | None = None
    proof: tuple[str, ...] = ()
    leaf_index: int = 0
    leaf_count: int = 0

    @property
    def batched(self) -> bool:
        return self.root is not None


# ---------------------------------------------------------------- lookup


async def load_item_by_tag(session: AsyncSession, raw_code: str) -> Item:
    """Resolve any typed form of a tag code to its item.

    The checksum is verified **before** any query runs. A mistyped code is a
    client error and answering it needs no database at all; going to Postgres
    first would turn every scan of a smudged label into a lookup, and would let
    a stranger probe the table by feeding it codes that cannot exist.

    A well-formed code nobody holds is a plain 404 that says nothing about
    whether it was ever issued.

    Deliberately *not* joined to the category and the registrant, which both get
    loaded a moment later. Joining them here and relying on the session's
    identity map to make those loads free does not work: the identity map holds
    **weak** references, so the two rows nobody kept a name for are collected as
    soon as this function returns and the next lookup goes back to the database
    having also paid for the join. :func:`build_view` holds them in locals
    instead, which is the part that actually decides how many statements this
    request costs.
    """
    canonical = normalize_tag_code(raw_code)
    if not validate_tag_code(canonical):
        raise ValidationError(
            code=ErrorCode.INVALID_TAG_CODE,
            status=400,
            message=(
                f"that is not a readable tag code: it must be {TAG_CODE_LENGTH} "
                "characters and end in a check symbol"
            ),
        )

    item = (
        await session.execute(select(Item).where(Item.tag_code == canonical))
    ).scalar_one_or_none()
    if item is None:
        raise NotFoundError(
            code=ErrorCode.NOT_FOUND,
            message="no record for this tag",
        )
    return item


# ---------------------------------------------------------------- recompute


async def recompute_item_hash(
    session: AsyncSession,
    item: Item,
    category: GICategory | None = None,
    registrant: User | None = None,
) -> str:
    """Rebuild the item's hash from the row as it stands right now.

    Uses the frozen preimage, unchanged, because the value it is compared
    against was produced by that exact function and a chain cannot be
    rewritten. If the registrant's salt has been erased under DPDP the identity
    digest can no longer be reproduced, and this returns a value that will not
    match -- which is the honest outcome: the record was deliberately made
    unlinkable, and pretending otherwise would defeat the erasure.

    *category* and *registrant* are accepted so a caller that already holds them
    does not make this function fetch them again. Both are optional: a caller
    with only an item still gets a correct answer, just at the cost of two more
    reads.
    """
    if category is None:
        category = await session.get(GICategory, item.category_id)
    if registrant is None:
        registrant = await session.get(User, item.registered_by)
    if category is None or registrant is None:  # pragma: no cover - FKs are RESTRICT
        return ""

    recomputed, _preimage = hash_item(
        item_id=item.id,
        category_slug=category.slug,
        category_schema_version=item.category_schema_version,
        parent_id=item.parent_id,
        quantity=item.quantity,
        quantity_unit=item.quantity_unit,
        attributes=dict(item.attributes),
        registered_by_hash=registrant_hash(registrant.id, registrant.identity_salt),
        registered_at=item.created_at,
    )
    return recomputed


# ---------------------------------------------------------------- the anchor


async def _batched_anchor(session: AsyncSession, item: Item) -> AnchorRecord | None:
    """The batched path, keyed by ``merkle_leaves.item_id``."""
    if (
        await session.execute(select(MerkleLeaf.item_id).where(MerkleLeaf.item_id == item.id))
    ).scalar_one_or_none() is None:
        return None

    proof = await get_inclusion_proof(session, item.id)
    if proof is None:  # pragma: no cover - the leaf row was just seen
        return None

    confirmations = 0
    anchored_at: datetime | None = None
    if proof.tx_hash:
        tx = (
            await session.execute(select(ChainTx).where(ChainTx.tx_hash == proof.tx_hash))
        ).scalar_one_or_none()
        if tx is not None:
            confirmations = tx.confirmations
            anchored_at = tx.created_at

    return AnchorRecord(
        anchored_hash=proof.item_hash,
        tx_hash=proof.tx_hash,
        block_number=proof.block_number,
        confirmations=confirmations,
        anchored_at=anchored_at,
        root=proof.root,
        proof=proof.proof,
        leaf_index=proof.leaf_index,
        leaf_count=proof.leaf_count,
    )


def _chain_event_record(event: ChainEvent) -> AnchorRecord:
    return AnchorRecord(
        anchored_hash=event.subject_hash,
        tx_hash=event.tx_hash,
        block_number=event.block_number,
        anchored_at=datetime.fromtimestamp(event.chain_timestamp, UTC),
        observed_at=event.observed_at,
    )


async def _single_anchor(session: AsyncSession, item: Item) -> AnchorRecord | None:
    """The single-anchor path.

    Preferred route is the append-only ``ANCHORED`` event, which is keyed by
    item id and names the transaction that carried it; the mirrored chain log
    for that transaction then supplies the hash the chain actually saw. Falling
    back to a lookup on the stored digest column is last, and is the only route
    that could be defeated by editing that column.
    """
    anchored_event = (
        await session.execute(
            select(ItemEvent)
            .where(
                ItemEvent.item_id == item.id,
                ItemEvent.event_type == ItemEventType.ANCHORED,
            )
            .order_by(ItemEvent.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if anchored_event is not None:
        tx_hash = anchored_event.payload.get("tx_hash")
        if isinstance(tx_hash, str) and tx_hash:
            log = (
                await session.execute(
                    select(ChainEvent).where(
                        ChainEvent.tx_hash == tx_hash,
                        ChainEvent.event_name == "ItemAnchored",
                    )
                )
            ).scalar_one_or_none()
            if log is not None:
                record = _chain_event_record(log)
                confirmations = anchored_event.payload.get("confirmations")
                return AnchorRecord(
                    anchored_hash=record.anchored_hash,
                    tx_hash=record.tx_hash,
                    block_number=record.block_number,
                    confirmations=int(confirmations) if isinstance(confirmations, int) else 0,
                    anchored_at=record.anchored_at,
                    observed_at=record.observed_at,
                )

    log = (
        await session.execute(
            select(ChainEvent)
            .where(
                ChainEvent.event_name == "ItemAnchored",
                ChainEvent.subject_hash == item.item_hash,
            )
            .order_by(ChainEvent.block_number)
            .limit(1)
        )
    ).scalar_one_or_none()
    return _chain_event_record(log) if log is not None else None


async def find_anchor(session: AsyncSession, item: Item) -> AnchorRecord | None:
    """What was anchored for this item, batched path first."""
    return await _batched_anchor(session, item) or await _single_anchor(session, item)


async def chain_state(
    session: AsyncSession,
    item: Item,
    recomputed: str,
    reader: ChainReader | None = None,
) -> ChainView:
    """Compare the recomputed hash to what was anchored, live where possible."""
    anchor = await find_anchor(session, item)
    checked_at = now()
    live = False
    result = VerificationResult.UNANCHORED

    if anchor is not None:
        # The offline comparison. For a batch this is the inclusion proof: the
        # recomputed hash has to be a leaf under the anchored root, which is a
        # stronger statement than equality with a stored leaf and is checkable
        # by anybody holding the proof.
        if anchor.batched and anchor.root is not None:
            offline_ok = bool(recomputed) and verify_inclusion(
                recomputed, list(anchor.proof), anchor.root
            )
        else:
            offline_ok = bool(recomputed) and recomputed == anchor.anchored_hash
        result = VerificationResult.MATCH if offline_ok else VerificationResult.MISMATCH

    if reader is not None:
        try:
            if anchor is not None and anchor.batched and anchor.root is not None:
                live = True
                if not await reader.is_batch_anchored(anchor.root):
                    # The root this proof is against is not on chain. The proof
                    # is still internally consistent and still proves nothing.
                    result = VerificationResult.UNANCHORED
            elif recomputed:
                anchored_now = await reader.is_item_anchored(recomputed)
                live = True
                if anchored_now:
                    result = VerificationResult.MATCH
                elif anchor is not None:
                    # The chain has never seen this hash, but something was
                    # anchored for this item. The row moved.
                    result = VerificationResult.MISMATCH
                else:
                    result = VerificationResult.UNANCHORED
        except Exception:  # noqa: BLE001 - a public page never fails on a dependency
            live = False

    if anchor is not None and anchor.observed_at is not None and not live:
        checked_at = anchor.observed_at

    proof_view = (
        InclusionProofView(
            root=anchor.root,
            leaf_index=anchor.leaf_index,
            leaf_count=anchor.leaf_count,
            proof=list(anchor.proof),
        )
        if anchor is not None and anchor.root is not None
        else None
    )

    return ChainView(
        status=str(item.status),
        tx_hash=anchor.tx_hash if anchor else None,
        block_number=anchor.block_number if anchor else None,
        confirmations=anchor.confirmations if anchor else 0,
        anchored_at=anchor.anchored_at if anchor else None,
        verification=result,
        # Anything not confirmed by a live call this request is last known
        # state, and says so.
        stale=not live,
        chain_checked_at=checked_at,
        inclusion_proof=proof_view,
    )


# ---------------------------------------------------------------- projection


def public_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """Drop attribute keys that could carry a person rather than a property.

    Category schemas are operator-authored, so the set of keys is not fixed at
    build time. An allowlist is impossible without freezing the catalogue; this
    is the denylist under it, and it is deliberately blunt -- withholding a
    legitimate field called ``dyer_name`` costs a reader one line of context,
    and publishing it costs somebody their name.
    """
    published: dict[str, Any] = {}
    for key, value in attributes.items():
        lowered = str(key).lower()
        if lowered.startswith("_"):
            continue
        if any(part in lowered for part in _IDENTIFYING_KEY_PARTS):
            continue
        published[key] = value
    return published


async def _ancestry(session: AsyncSession, item: Item) -> list[TreeStepView]:
    """The lineage above this item, root first, with a hard depth ceiling.

    One recursive CTE, shared with the authenticated tree endpoint. Walking the
    parent links in Python issues one query per level, which on a four-deep
    lineage is four round trips on the one page a shopper waits for -- and the
    depth is attacker-influenced in the sense that it is whatever somebody
    registered, so the cost is not bounded by anything this module controls.

    The ceiling stays on top of the CTE's own. The depth guard lives in the
    registration path, and a public reader should not be the thing that
    discovers a cycle somebody introduced by hand.
    """
    lineage = await get_ancestry(session, item.id)

    # `get_ancestry` returns root first and includes the item itself as the last
    # element. The public view shows what came *before* this object, so the tail
    # is dropped rather than rendered as a step in its own provenance.
    parents = [node for node in lineage if node.id != item.id][:MAX_ANCESTRY_DEPTH]

    return [
        TreeStepView(
            depth=depth,
            quantity=parent.quantity,
            quantity_unit=parent.quantity_unit,
            status=str(parent.status),
        )
        for depth, parent in enumerate(parents)
    ]


async def _events(session: AsyncSession, item: Item) -> list[PublicEventView]:
    rows = list(
        (
            await session.execute(
                select(ItemEvent)
                .where(ItemEvent.item_id == item.id)
                .order_by(ItemEvent.created_at, ItemEvent.id)
                .limit(MAX_PUBLIC_EVENTS)
            )
        )
        .scalars()
        .all()
    )
    published: list[PublicEventView] = []
    for row in rows:
        if row.event_type not in _PUBLIC_EVENT_TYPES:
            continue
        tx_hash = row.payload.get("tx_hash")
        block_number = row.payload.get("block_number")
        published.append(
            PublicEventView(
                type=str(row.event_type),
                at=row.created_at,
                tx_hash=tx_hash if isinstance(tx_hash, str) else None,
                block_number=block_number if isinstance(block_number, int) else None,
            )
        )
    return published


async def _story(
    session: AsyncSession, item: Item, settings: Settings, maker: User | None = None
) -> StoryView:
    if maker is None:
        maker = await session.get(User, item.registered_by)
    opted_out = bool(maker.public_display_opt_out) if maker is not None else True

    rows = (
        await session.execute(
            select(ItemMedia, Media)
            .join(Media, Media.id == ItemMedia.media_id)
            .where(ItemMedia.item_id == item.id)
            .order_by(Media.created_at)
        )
    ).all()

    gateway = settings.pinata_gateway_url.rstrip("/")
    media = [
        MediaView(
            kind=str(link.kind),
            sha256=blob.sha256,
            cid=blob.cid,
            # Only a content address. There is no public route on this service
            # that serves the bytes, so an unpinned file is reported honestly as
            # having nowhere to read it from rather than given a link that 401s.
            gateway_url=f"{gateway}/{blob.cid}" if blob.cid else None,
        )
        for link, blob in rows
    ]

    if maker is None or opted_out:
        return StoryView(maker_opted_out=True, media=media)
    return StoryView(
        weaver_display_name=maker.display_name,
        region=maker.region,
        maker_opted_out=False,
        media=media,
    )


async def _scan_block(
    session: AsyncSession, item: Item, verdict: AnomalyVerdict | None, settings: Settings
) -> ScanBlock:
    """The scan summary, from one load of the scan history rather than two.

    The count comes off the verdict. Every anomaly rule already runs over the
    whole history, so a separate ``SELECT count(*)`` would ask the database to
    re-read exactly the rows the verdict was derived from to learn how many
    there were.
    """
    assessment = verdict if verdict is not None else await assess_scans(session, item.id, settings)
    return ScanBlock(
        count=assessment.scan_count,
        suspicion_level=str(assessment.level),
        reason=assessment.reason,
        signals=[str(code) for code in assessment.codes],
    )


async def _child_count(session: AsyncSession, item_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                select(func.count()).select_from(Item).where(Item.parent_id == item_id)
            )
        ).scalar_one()
    )


async def build_view(
    session: AsyncSession,
    item: Item,
    claim: ClaimView,
    *,
    reader: ChainReader | None = None,
    verdict: AnomalyVerdict | None = None,
    settings: Settings | None = None,
) -> PublicItemView:
    """Assemble the whole public payload for one item.

    The category and the registrant are read once, into locals that live for the
    whole call, and handed to everything below that needs them. Both details
    matter. Reading them once is obvious; *holding* them is not, and it is the
    part that works: the session's identity map keeps only weak references, so a
    row loaded inside a helper and dropped on return is collected and the next
    helper asking for it pays for the row again. Before this, the registrant was
    fetched twice for every public page view.
    """
    config = settings or get_settings()

    category = await session.get(GICategory, item.category_id)
    registrant = await session.get(User, item.registered_by)

    recomputed = await recompute_item_hash(session, item, category, registrant)
    chain = await chain_state(session, item, recomputed, reader)
    trust = await assess(session, item)

    return PublicItemView(
        tag_code=item.tag_code or "",
        display_code=_grouped(item.tag_code or ""),
        category=CategoryView(
            slug=category.slug if category else "",
            display_name=category.display_name if category else "",
            schema_version=item.category_schema_version,
        ),
        attributes=public_attributes(dict(item.attributes)),
        quantity=item.quantity,
        quantity_unit=item.quantity_unit,
        trust=TrustView(
            level=str(trust.level),
            contributing_roles=[str(role) for role in trust.contributing_roles],
            attestation_count=trust.attestation_count,
            disputed=trust.is_disputed,
        ),
        chain=chain,
        provenance=ProvenanceView(
            ancestry=await _ancestry(session, item),
            events=await _events(session, item),
            child_count=await _child_count(session, item.id),
        ),
        story=await _story(session, item, config, registrant),
        scan=await _scan_block(session, item, verdict, config),
        claim=ClaimBlock(
            status=str(claim.status),
            claimed=claim.claimed,
            claimed_at=claim.claimed_at,
            is_your_claim=claim.is_your_claim,
            claimed_region=claim.claimed_region,
            message=claim.message,
        ),
    )


def _grouped(code: str) -> str:
    """The printed form, grouped in fours. Display only, never compared."""
    return "-".join(code[start : start + 4] for start in range(0, len(code), 4))
