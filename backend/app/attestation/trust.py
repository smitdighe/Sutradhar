"""Trust levels, derived at read time from who vouched and how independent they were.

**The oracle problem, stated plainly.** A chain stores whatever a human typed
into it. A weaver, or a co-op officer taking a bribe, can register a powerloom
piece as handloom and the ledger will hold that claim, unaltered, forever.
Immutability is a property of the record, not of the truth of the record. Any
system that presents an anchored hash as evidence that an object is what it says
it is has quietly substituted one for the other.

So this module does not answer "is this real". It answers **who vouched for it,
and how independent were they**, and it puts that in front of the reader instead
of a verdict. A consumer looking at ``SELF_DECLARED`` can see that exactly one
person -- the person with the most to gain -- has made a claim, and can weigh it
accordingly. That is a smaller promise than the one weak pitches make, and it is
one this system can actually keep.

**Derived, never stored.** No table carries a trust column, nothing has a
setter, and no admin endpoint assigns a level. The level is a pure function of the attestation
set and the dispute set, computed on every read. Three consequences, all of them
the point:

* Nobody can grant a level. There is no privileged write to abuse, and no bug
  that leaves a stale high level behind after the evidence for it disappears.
* Fraud-flagging an attestor takes effect on the very next read, everywhere, with
  no cache to invalidate and no backfill job that might not have run yet.
* The stored data and the displayed level cannot disagree, because there is only
  one of them.

**Independence is what a level measures.** An attestation from the item's own
registrant raises nothing: a person vouching for their own work is exactly the
claim already being made. Neither does a second attestation from someone who has
already attested -- repetition is not corroboration. So the count that matters is
of *distinct actors other than the registrant*, grouped by the role each held at
the time they attested.

**A fraud-flagged participant is disqualifying, not merely discounted.** If any
actor in the chain is flagged, the level is ``DISPUTED`` regardless of how many
other people vouched. Averaging a flagged actor away would let a compromised
co-op officer be outvoted into looking fine, which is precisely the situation a
reader most needs to be told about.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.attestation import Attestation, ItemDispute
from app.db.models.catalog import Item
from app.db.models.enums import UserRole, UserStatus
from app.db.models.user import User

__all__ = [
    "LEVEL_ORDER",
    "RAISING_ROLES",
    "AttestorView",
    "TrustAssessment",
    "TrustLevel",
    "assess",
    "assess_many",
]


class TrustLevel(StrEnum):
    """How much independent corroboration a record has attracted.

    Deliberately a description of the evidence, not a judgement about the
    object. None of these values says anything is real, and none of them ever
    should -- see the module docstring.
    """

    # Only the registrant has vouched. The floor, and the honest default.
    SELF_DECLARED = "SELF_DECLARED"
    # At least one independent co-operative officer has attested.
    CO_OP_ATTESTED = "CO_OP_ATTESTED"
    # At least one independent inspector has attested.
    INSPECTED = "INSPECTED"
    # A participant is fraud-flagged, or the item is contested. Overrides
    # everything above: corroboration from a compromised chain is not evidence.
    DISPUTED = "DISPUTED"


# Ascending order of corroboration. DISPUTED is not on this ladder -- it is not
# "more than INSPECTED", it is a different statement -- and is applied as an
# override rather than a maximum.
LEVEL_ORDER: tuple[TrustLevel, ...] = (
    TrustLevel.SELF_DECLARED,
    TrustLevel.CO_OP_ATTESTED,
    TrustLevel.INSPECTED,
)

# Which role, held independently, lifts the level to what. A weaver attesting to
# someone else's item is recorded and shown, but does not raise the level: peer
# endorsement between weavers is not the independent check a co-op or an
# inspector represents.
RAISING_ROLES: dict[UserRole, TrustLevel] = {
    UserRole.COOP_OFFICER: TrustLevel.CO_OP_ATTESTED,
    UserRole.INSPECTOR: TrustLevel.INSPECTED,
}


@dataclass(frozen=True, slots=True)
class AttestorView:
    """One attestation reduced to what a trust computation needs.

    Carries no name, no email and no user id beyond what the caller already
    holds -- the public serialiser projects this down further still.
    """

    attestor_id: uuid.UUID
    role: UserRole
    is_registrant: bool
    fraud_flagged: bool
    status: UserStatus

    @property
    def counts_toward_level(self) -> bool:
        """Whether this attestation can raise the level at all.

        Three ways to be present in the record and still not raise it: being the
        registrant (self-endorsement), being fraud-flagged (the flag is handled
        as an override, but this keeps a flagged actor from also contributing),
        and not yet being a verified account.
        """
        if self.is_registrant or self.fraud_flagged:
            return False
        # A pending co-op officer is somebody claiming an authority nobody has
        # checked. Letting that raise a level would make the whole ladder
        # self-service: register, claim COOP_OFFICER, attest, look corroborated.
        return self.status is UserStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class TrustAssessment:
    """The computed level and the evidence it was computed from.

    The evidence travels with the verdict on purpose. A reader who disagrees
    with the level can see exactly what produced it, which is the difference
    between a system that reports and one that pronounces.
    """

    item_id: uuid.UUID
    level: TrustLevel
    contributing_roles: tuple[UserRole, ...]
    attestation_count: int
    distinct_attestor_count: int
    dispute_reason: str | None = None
    flagged_attestor_count: int = 0
    disqualified_roles: tuple[UserRole, ...] = field(default_factory=tuple)

    @property
    def is_disputed(self) -> bool:
        return self.level is TrustLevel.DISPUTED


def level_from_views(
    views: list[AttestorView],
    *,
    disputed: bool,
) -> tuple[TrustLevel, tuple[UserRole, ...]]:
    """Reduce a set of attestations to a level and the roles that produced it.

    Pure, synchronous, and the only place the ladder is walked. Everything
    asynchronous around it is loading; this is the rule.
    """
    if disputed or any(view.fraud_flagged for view in views):
        # Override, not a maximum. See the module docstring: a flagged
        # participant cannot be outvoted into looking fine.
        return TrustLevel.DISPUTED, ()

    # Distinct actors, so a second attestation from the same person adds
    # nothing. Repetition is not corroboration.
    independent: dict[uuid.UUID, UserRole] = {
        view.attestor_id: view.role for view in views if view.counts_toward_level
    }

    contributing = {
        role for role in independent.values() if role in RAISING_ROLES
    }
    if not contributing:
        return TrustLevel.SELF_DECLARED, ()

    reached = max(
        (RAISING_ROLES[role] for role in contributing),
        key=LEVEL_ORDER.index,
    )
    # Ordered by the ladder so the payload reads the same way every time.
    ordered = tuple(
        role
        for role in (UserRole.COOP_OFFICER, UserRole.INSPECTOR)
        if role in contributing
    )
    return reached, ordered


async def assess(session: AsyncSession, item: Item) -> TrustAssessment:
    """Compute one item's trust level from the database, right now."""
    results = await assess_many(session, [item])
    return results[item.id]


async def assess_many(
    session: AsyncSession, items: list[Item]
) -> dict[uuid.UUID, TrustAssessment]:
    """Assess several items in a fixed number of queries.

    Batched because the listing endpoints need a level per row, and a per-item
    round trip there turns one page into a hundred queries.
    """
    if not items:
        return {}

    item_ids = [item.id for item in items]
    registrants = {item.id: item.registered_by for item in items}

    attestation_rows = (
        await session.execute(
            select(Attestation, User)
            .join(User, User.id == Attestation.attestor_id)
            .where(Attestation.item_id.in_(item_ids))
            .order_by(Attestation.created_at)
        )
    ).all()

    dispute_rows = (
        await session.execute(
            select(ItemDispute)
            .where(
                ItemDispute.item_id.in_(item_ids),
                ItemDispute.cleared_at.is_(None),
            )
            .order_by(ItemDispute.raised_at)
        )
    ).scalars()

    open_disputes: dict[uuid.UUID, ItemDispute] = {}
    for row in dispute_rows:
        # Oldest open dispute wins the "why" slot. The rest still count -- the
        # item is disputed if any of them is open -- but a payload can only
        # carry one reason, and the first one raised is the honest choice.
        open_disputes.setdefault(row.item_id, row)

    by_item: dict[uuid.UUID, list[AttestorView]] = {item_id: [] for item_id in item_ids}
    for attestation, attestor in attestation_rows:
        by_item[attestation.item_id].append(
            AttestorView(
                attestor_id=attestation.attestor_id,
                # The role snapshotted at attestation time, not the role the
                # account holds now. An inspector who later became a consumer
                # still made that attestation as an inspector, and reading the
                # live role would silently rewrite history.
                role=attestation.attestor_role,
                is_registrant=attestation.attestor_id == registrants[attestation.item_id],
                fraud_flagged=attestor.fraud_flagged_at is not None,
                status=attestor.status,
            )
        )

    assessments: dict[uuid.UUID, TrustAssessment] = {}
    for item in items:
        views = by_item[item.id]
        dispute = open_disputes.get(item.id)
        level, roles = level_from_views(views, disputed=dispute is not None)

        flagged = [view for view in views if view.fraud_flagged]
        reason = dispute.reason if dispute is not None else None
        if reason is None and flagged:
            reason = (
                f"{len(flagged)} attestor(s) on this record are fraud-flagged; "
                "their attestations no longer contribute"
            )

        assessments[item.id] = TrustAssessment(
            item_id=item.id,
            level=level,
            contributing_roles=roles,
            attestation_count=len(views),
            distinct_attestor_count=len({view.attestor_id for view in views}),
            dispute_reason=reason,
            flagged_attestor_count=len(flagged),
            disqualified_roles=tuple(
                sorted(
                    {
                        view.role
                        for view in views
                        if not view.counts_toward_level and not view.is_registrant
                    },
                    key=lambda role: role.value,
                )
            ),
        )
    return assessments
