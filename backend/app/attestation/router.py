"""Attestation, trust and fraud-flag endpoints.

The trust endpoint is the one that matters and it deliberately answers a
narrower question than the one people ask. Asked "is this saree real", it
answers "here is who vouched for it, in what capacity, how many of them were
independent of the seller, and whether anyone has contested it". That is the
honest answer, and refusing to compress it into a yes or no is the whole point
of the phase.

Mounted under the authenticated prefix. The public consumer-facing view is
Phase 11 and projects further still.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.attestation import reputation, service
from app.attestation.schemas import (
    AttestationListResponse,
    AttestationResponse,
    CreateAttestationRequest,
    FlagActorRequest,
    FlagActorResponse,
    TrustResponse,
)
from app.attestation.trust import assess
from app.auth.guards import get_current_user, require_role
from app.auth.roles import Role
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, clamp_limit, decode_cursor, encode_cursor
from app.db.models.user import User
from app.db.session import get_session

__all__ = ["admin_router", "router"]

router = APIRouter(prefix="/items", tags=["attestation"])

admin_router = APIRouter(
    prefix="/admin/actors",
    tags=["attestation", "admin"],
    dependencies=[Depends(require_role(Role.ADMIN))],
)

# Who may reach the endpoint at all. Finer-grained refusals -- fraud-flagged,
# not yet verified, attesting to somebody else's item -- live in the service,
# because they depend on the item as well as the actor.
_can_attest = require_role(Role.WEAVER, Role.COOP_OFFICER, Role.INSPECTOR)


@router.post(
    "/{item_id}/attestations",
    status_code=status.HTTP_201_CREATED,
    response_model=AttestationResponse,
    summary="Record an attestation about an item",
)
async def create_attestation(
    item_id: uuid.UUID,
    payload: CreateAttestationRequest,
    actor: User = Depends(_can_attest),
    session: AsyncSession = Depends(get_session),
) -> AttestationResponse:
    """Vouch for an item. One attestation per actor per item, enforced by the database.

    A second attempt is a 409 from ``uq_attestations_item_attestor``, not from
    an application check: a check-then-write has a gap, and two simultaneous
    requests would both pass it.
    """
    attestation = await service.create_attestation(session, item_id, payload.statement, actor)
    await session.commit()

    from app.attestation.statement_hash import attestor_hash

    return AttestationResponse(
        id=attestation.id,
        item_id=attestation.item_id,
        attestor_ref=attestor_hash(actor.id, actor.identity_salt),
        attestor_role=attestation.attestor_role,
        attestor_fraud_flagged=False,
        statement=dict(attestation.statement),
        statement_hash=attestation.statement_hash,
        created_at=attestation.created_at,
    )


@router.get(
    "/{item_id}/attestations",
    response_model=AttestationListResponse,
    summary="List the attestations recorded against an item",
)
async def list_attestations(
    item_id: uuid.UUID,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    _actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AttestationListResponse:
    """Newest first. Carries roles and pseudonymous references, never identities."""
    await service.load_item(session, item_id)

    decoded = decode_cursor(cursor) if cursor else None
    views, has_more = await service.list_attestations(
        session, item_id, decoded, clamp_limit(limit)
    )

    next_cursor = (
        encode_cursor(views[-1].created_at, views[-1].id) if has_more and views else None
    )
    return AttestationListResponse(
        items=[AttestationResponse.model_validate(view) for view in views],
        next_cursor=next_cursor,
    )


@router.get(
    "/{item_id}/trust",
    response_model=TrustResponse,
    summary="Who vouched for this item, and how independent were they",
)
async def get_trust(
    item_id: uuid.UUID,
    _actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TrustResponse:
    """Derived on every read from the attestation and dispute sets.

    Nothing is cached and nothing is stored, so a fraud flag applied a second
    ago is already reflected here.
    """
    item = await service.load_item(session, item_id)
    assessment = await assess(session, item)
    return TrustResponse(
        item_id=assessment.item_id,
        level=assessment.level,
        contributing_roles=list(assessment.contributing_roles),
        attestation_count=assessment.attestation_count,
        distinct_attestor_count=assessment.distinct_attestor_count,
        dispute_reason=assessment.dispute_reason,
        flagged_attestor_count=assessment.flagged_attestor_count,
    )


# ------------------------------------------------------------------- admin


@admin_router.post(
    "/{user_id}/fraud-flag",
    response_model=FlagActorResponse,
    summary="Flag an actor and dispute everything they registered",
)
async def fraud_flag(
    user_id: uuid.UUID,
    payload: FlagActorRequest,
    admin: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> FlagActorResponse:
    """One transaction: the flag, the audit event, and every affected item.

    Items the actor *registered* become disputed. Items they merely attested to
    are not touched -- their trust level drops on the next read, because the
    level is derived and a flagged attestor stops counting.
    """
    outcome = await reputation.flag_actor(session, user_id, payload.reason, admin.id)
    await session.commit()
    return FlagActorResponse(
        actor_id=outcome.actor_id,
        fraud_flagged=True,
        items_affected=outcome.items_affected,
        attestations_affected=outcome.attestations_affected,
        already_in_state=outcome.already_in_state,
    )


@admin_router.post(
    "/{user_id}/fraud-clear",
    response_model=FlagActorResponse,
    summary="Lift a fraud flag and the disputes it caused",
)
async def fraud_clear(
    user_id: uuid.UUID,
    payload: FlagActorRequest,
    admin: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> FlagActorResponse:
    """Reverses exactly what the flag did, and nothing else.

    An item independently disputed by an inspector stays disputed: that finding
    is not this flag's to lift.
    """
    outcome = await reputation.clear_fraud_flag(session, user_id, admin.id, payload.reason)
    await session.commit()
    return FlagActorResponse(
        actor_id=outcome.actor_id,
        fraud_flagged=False,
        items_affected=outcome.items_affected,
        attestations_affected=outcome.attestations_affected,
        already_in_state=outcome.already_in_state,
    )
