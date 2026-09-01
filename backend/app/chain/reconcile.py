"""Diffing what the chain says against what Postgres says. Reports, never heals.

**Why it does not fix anything.** Auto-correction hides the bug that caused the
drift. If an item is ``CONFIRMED`` in Postgres with nothing on chain to back it,
quietly demoting it makes the symptom disappear and leaves whatever produced it
running. The four categories below are findings for a human, and the only
automatic response anywhere in this package is the reorg demotion in
``confirmations.py``, which is not a correction -- it is the chain changing its
mind, which this system is required to follow.

**The four categories.**

*on-chain-not-in-db* -- an anchor exists on chain that nothing here accounts
for. Either the indexer is behind, or something anchored using this contract and
this key from outside this system.

*in-db-not-on-chain* -- an item this database claims is anchored, with no
matching event. This is the one that matters most: it is the shape of a lie told
to a consumer.

*hash mismatch* -- the item row and its own recorded preimage no longer agree,
or the preimage no longer hashes to the anchored value. Either the row was
edited after anchoring, or the event log was.

*status disagreement* -- the chain and the database agree an anchor exists but
disagree about whether it counts yet. Usually a sweep that has not run;
occasionally a confirmation-depth bug.

Nothing here needs the identity salt. The hash check recomputes from the
preimage stored in the ``REGISTERED`` event, so an item whose subject exercised
their right to erasure still verifies -- which is the point of storing a salted
digest rather than a name.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chain.client import ChainClient
from app.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.models.attestation import Attestation
from app.db.models.catalog import GICategory, Item, ItemEvent
from app.db.models.chain import ChainEvent, MerkleBatch, MerkleLeaf
from app.db.models.enums import ItemEventType, ItemStatus
from app.provenance.item_hash import compute_item_hash, quantise

__all__ = ["Drift", "ReconcileReport", "reconcile"]

logger = get_logger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]

ON_CHAIN_NOT_IN_DB = "on_chain_not_in_db"
IN_DB_NOT_ON_CHAIN = "in_db_not_on_chain"
HASH_MISMATCH = "hash_mismatch"
STATUS_DISAGREEMENT = "status_disagreement"


@dataclass(frozen=True, slots=True)
class Drift:
    """One disagreement between the chain and the database."""

    kind: str
    subject: str
    detail: str
    item_id: uuid.UUID | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "subject": self.subject,
            "detail": self.detail,
            "item_id": str(self.item_id) if self.item_id else None,
        }


@dataclass(slots=True)
class ReconcileReport:
    """Every disagreement found in one pass, grouped by category."""

    on_chain_not_in_db: list[Drift] = field(default_factory=list)
    in_db_not_on_chain: list[Drift] = field(default_factory=list)
    hash_mismatch: list[Drift] = field(default_factory=list)
    status_disagreement: list[Drift] = field(default_factory=list)
    items_checked: int = 0
    events_checked: int = 0

    @property
    def drifts(self) -> list[Drift]:
        return [
            *self.on_chain_not_in_db,
            *self.in_db_not_on_chain,
            *self.hash_mismatch,
            *self.status_disagreement,
        ]

    @property
    def total(self) -> int:
        return len(self.drifts)

    @property
    def clean(self) -> bool:
        """True when the chain and the database agree on everything checked."""
        return self.total == 0

    def add(self, drift: Drift) -> None:
        getattr(self, drift.kind).append(drift)

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "items_checked": self.items_checked,
            "events_checked": self.events_checked,
            "on_chain_not_in_db": len(self.on_chain_not_in_db),
            "in_db_not_on_chain": len(self.in_db_not_on_chain),
            "hash_mismatch": len(self.hash_mismatch),
            "status_disagreement": len(self.status_disagreement),
            "total": self.total,
        }


async def reconcile(
    session_factory: SessionFactory,
    client: ChainClient | None = None,
    settings: Settings | None = None,
) -> ReconcileReport:
    """Walk the event mirror and the item table and diff them.

    *client* is optional: without it the confirmation-depth checks are skipped
    and everything else still runs, so reconciliation remains useful when the
    chain is unreachable -- which is exactly when drift is most likely.
    """
    resolved = settings or get_settings()
    report = ReconcileReport()

    head: int | None = None
    if client is not None:
        head = await client.block_number()

    async with session_factory() as session:
        events = list(
            (
                await session.execute(
                    select(ChainEvent).where(ChainEvent.event_name == "ItemAnchored")
                )
            )
            .scalars()
            .all()
        )
        report.events_checked = len(events)
        by_hash = {event.subject_hash.lower(): event for event in events}

        items = list((await session.execute(select(Item))).scalars().all())
        report.items_checked = len(items)
        item_hashes = {item.item_hash.lower(): item for item in items}

        batched = await _batched_item_ids(session)

        await _check_orphan_events(session, report, by_hash, item_hashes)
        await _check_items(
            session,
            report,
            items,
            by_hash,
            batched,
            head=head,
            required_depth=resolved.chain_confirmations,
        )
        await session.commit()

    log = logger.info if report.clean else logger.warning
    log(
        "chain.reconcile.report",
        **report.as_log_fields(),
        verdict="no drift" if report.clean else "drift found; reported, not corrected",
    )
    for drift in report.drifts[:50]:
        logger.warning("chain.reconcile.drift", **drift.as_dict())
    return report


# --------------------------------------------------------------------- checks


async def _check_orphan_events(
    session: AsyncSession,
    report: ReconcileReport,
    by_hash: dict[str, ChainEvent],
    item_hashes: dict[str, Item],
) -> None:
    """Anchors on chain that no row in this database accounts for.

    Two kinds of hash reach the chain through the same contract function: item
    hashes and attestation statement hashes. The event carries no discriminator
    -- deliberately, because the contract records a claim about a 32-byte value
    and does not need to know what the value means -- so both tables are
    consulted before a hash is called unaccounted for. Checking only ``items``
    would report every anchored attestation as drift and bury the real findings.
    """
    statement_hashes = {
        row.lower()
        for row in (await session.execute(select(Attestation.statement_hash))).scalars().all()
    }

    for subject, event in by_hash.items():
        if subject in item_hashes or subject in statement_hashes:
            continue
        report.add(
            Drift(
                kind=ON_CHAIN_NOT_IN_DB,
                subject=subject,
                detail=(
                    f"ItemAnchored in block {event.block_number} "
                    f"(tx {event.tx_hash}) matches no item and no attestation"
                ),
            )
        )

    roots = list(
        (
            await session.execute(
                select(ChainEvent).where(ChainEvent.event_name == "BatchAnchored")
            )
        )
        .scalars()
        .all()
    )
    if not roots:
        return
    known = {
        row.lower()
        for row in (await session.execute(select(MerkleBatch.root))).scalars().all()
    }
    for event in roots:
        if event.subject_hash.lower() in known:
            continue
        report.add(
            Drift(
                kind=ON_CHAIN_NOT_IN_DB,
                subject=event.subject_hash,
                detail=(
                    f"BatchAnchored over {event.leaf_count} leaves in block "
                    f"{event.block_number} matches no merkle_batches row"
                ),
            )
        )


async def _check_items(
    session: AsyncSession,
    report: ReconcileReport,
    items: list[Item],
    by_hash: dict[str, ChainEvent],
    batched: dict[uuid.UUID, str],
    *,
    head: int | None,
    required_depth: int,
) -> None:
    """Per-item: is it anchored, does its hash still hold, do the statuses agree."""
    for item in items:
        await _check_item_hash(session, report, item)

        event = by_hash.get(item.item_hash.lower())
        covering_root = batched.get(item.id)

        if event is None and covering_root is None:
            if item.status == ItemStatus.CONFIRMED:
                # The category that matters: this database is telling consumers
                # a hash is on chain and there is no event backing it.
                report.add(
                    Drift(
                        kind=IN_DB_NOT_ON_CHAIN,
                        subject=item.item_hash,
                        detail="item is CONFIRMED but no ItemAnchored event was indexed for it",
                        item_id=item.id,
                    )
                )
            continue

        if event is None:
            # Covered by a batch root rather than an individual anchor.
            continue

        if item.status == ItemStatus.PENDING:
            depth = None if head is None else head - event.block_number
            if depth is None or depth >= required_depth:
                report.add(
                    Drift(
                        kind=STATUS_DISAGREEMENT,
                        subject=item.item_hash,
                        detail=(
                            "an ItemAnchored event exists"
                            + (f" at depth {depth}" if depth is not None else "")
                            + " but the item is still PENDING"
                        ),
                        item_id=item.id,
                    )
                )
        elif item.status == ItemStatus.FAILED:
            report.add(
                Drift(
                    kind=STATUS_DISAGREEMENT,
                    subject=item.item_hash,
                    detail=(
                        f"item is FAILED but ItemAnchored exists in block {event.block_number}"
                    ),
                    item_id=item.id,
                )
            )


async def _check_item_hash(
    session: AsyncSession, report: ReconcileReport, item: Item
) -> None:
    """Recompute the anchored digest from the recorded preimage and compare."""
    event = (
        await session.execute(
            select(ItemEvent)
            .where(
                ItemEvent.item_id == item.id,
                ItemEvent.event_type == ItemEventType.REGISTERED,
            )
            .order_by(ItemEvent.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()

    if event is None:
        report.add(
            Drift(
                kind=HASH_MISMATCH,
                subject=item.item_hash,
                detail="no REGISTERED event, so the anchored preimage cannot be recomputed",
                item_id=item.id,
            )
        )
        return

    preimage = event.payload.get("preimage")
    if not isinstance(preimage, dict):
        report.add(
            Drift(
                kind=HASH_MISMATCH,
                subject=item.item_hash,
                detail="the REGISTERED event carries no usable preimage",
                item_id=item.id,
            )
        )
        return

    recomputed = compute_item_hash(preimage)
    if recomputed.lower() != item.item_hash.lower():
        report.add(
            Drift(
                kind=HASH_MISMATCH,
                subject=item.item_hash,
                detail=f"the recorded preimage hashes to {recomputed}, not to the stored hash",
                item_id=item.id,
            )
        )
        return

    divergence = await _preimage_vs_row(session, item, preimage)
    if divergence:
        report.add(
            Drift(
                kind=HASH_MISMATCH,
                subject=item.item_hash,
                detail=f"the item row no longer matches its anchored preimage: {divergence}",
                item_id=item.id,
            )
        )


async def _preimage_vs_row(
    session: AsyncSession, item: Item, preimage: dict[str, Any]
) -> str:
    """Field-by-field diff of the anchored preimage against the live row.

    Catches the case the digest check cannot: a row edited after anchoring, where
    the event log still holds the original preimage and both are internally
    consistent. The hash is right, the row is not, and only comparing them shows it.
    """
    problems: list[str] = []

    if str(preimage.get("item_id")) != str(item.id):
        problems.append(f"item_id {preimage.get('item_id')} != {item.id}")

    parent = preimage.get("parent_id")
    actual_parent = str(item.parent_id) if item.parent_id else None
    if (str(parent) if parent is not None else None) != actual_parent:
        problems.append(f"parent_id {parent} != {actual_parent}")

    try:
        if quantise(Decimal(str(preimage.get("quantity")))) != quantise(item.quantity):
            problems.append(f"quantity {preimage.get('quantity')} != {item.quantity}")
    except (ArithmeticError, TypeError, ValueError):
        problems.append(f"quantity {preimage.get('quantity')!r} is not a usable decimal")

    if preimage.get("quantity_unit") != item.quantity_unit:
        problems.append(f"quantity_unit {preimage.get('quantity_unit')} != {item.quantity_unit}")

    if int(preimage.get("category_schema_version", -1)) != item.category_schema_version:
        problems.append(
            f"category_schema_version {preimage.get('category_schema_version')} "
            f"!= {item.category_schema_version}"
        )

    category = await session.get(GICategory, item.category_id)
    if category is not None and preimage.get("category_slug") != category.slug:
        problems.append(f"category_slug {preimage.get('category_slug')} != {category.slug}")

    if preimage.get("attributes") != item.attributes:
        problems.append("attributes differ from the anchored preimage")

    return "; ".join(problems)


async def _batched_item_ids(session: AsyncSession) -> dict[uuid.UUID, str]:
    """Map each batched item to the root that covers it."""
    rows = (
        await session.execute(
            select(MerkleLeaf.item_id, MerkleBatch.root).join(
                MerkleBatch, MerkleBatch.id == MerkleLeaf.batch_id
            )
        )
    ).all()
    return {row[0]: row[1] for row in rows}
