"""Request and response models for attestations, trust, and fraud flags.

Every field here was chosen against one rule: **the payload reports evidence, it
does not deliver a verdict.** There is no boolean saying an object is what it
claims to be, and there never will be, because this system cannot know that and
saying so would be the single most damaging thing it could do. What it can say
is who vouched, in what capacity, how independent they were, and whether anyone
has contested it -- and that is what these models carry.

Attestor identity is deliberately absent. A reader gets the role and a stable
pseudonymous reference; names, emails and user ids stay server-side.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.attestation.trust import TrustLevel
from app.core.clock import UtcDatetime
from app.db.models.enums import DisputeSource, UserRole

__all__ = [
    "AttestationListResponse",
    "AttestationResponse",
    "CreateAttestationRequest",
    "FlagActorRequest",
    "FlagActorResponse",
    "TrustResponse",
]

# A statement is free-form on purpose: an inspector's notes, a co-op's ledger
# reference and a weaver's loom details have nothing in common, and forcing them
# into one schema would either exclude real evidence or produce a schema so
# permissive it validates nothing. It is bounded rather than validated.
MAX_STATEMENT_KEYS = 50
MAX_STATEMENT_BYTES = 16_384


class CreateAttestationRequest(BaseModel):
    """A claim one party makes about one item."""

    model_config = ConfigDict(extra="forbid")

    statement: dict[str, Any] = Field(
        description="Free-form claim: inspection notes, ledger references, dates."
    )

    @field_validator("statement")
    @classmethod
    def _bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Bound the size. Not a schema -- a ceiling.

        Unvalidated JSONB is fine; unbounded JSONB is a way to put a megabyte in
        a row that gets hashed and read on every trust computation.
        """
        if not value:
            raise ValueError("statement must not be empty")
        if len(value) > MAX_STATEMENT_KEYS:
            raise ValueError(f"statement may hold at most {MAX_STATEMENT_KEYS} keys")
        import json

        if len(json.dumps(value).encode("utf-8")) > MAX_STATEMENT_BYTES:
            raise ValueError(f"statement may be at most {MAX_STATEMENT_BYTES} bytes")
        return value


class AttestationResponse(BaseModel):
    """One recorded attestation, with the attestor pseudonymised."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    item_id: uuid.UUID
    # The salted identity digest, which is also the value anchored on chain.
    # Stable across reads, and meaningless to anyone without the subject's salt.
    attestor_ref: str
    # The role held when the attestation was made, not the role held now.
    attestor_role: UserRole
    # Surfaced so a reader can see why an attestation is present but not
    # counting, rather than wondering why the level did not move.
    attestor_fraud_flagged: bool
    statement: dict[str, Any]
    statement_hash: str
    created_at: UtcDatetime


class AttestationListResponse(BaseModel):
    """A page of attestations, newest first."""

    items: list[AttestationResponse]
    next_cursor: str | None = None


class TrustResponse(BaseModel):
    """What corroboration a record has attracted. Not a verdict about the object.

    ``level`` describes the evidence, and the fields beneath it are that
    evidence, so a reader who wants to weigh it themselves can. A payload that
    carried only the level would be asking to be trusted; this one shows its
    working.
    """

    item_id: uuid.UUID
    level: TrustLevel
    # Which independent roles lifted the level. Empty at SELF_DECLARED, and
    # empty at DISPUTED -- a disputed record has no standing corroboration.
    contributing_roles: list[UserRole]
    attestation_count: int
    distinct_attestor_count: int
    # Why the record is contested, when it is. Null is the common case and does
    # not mean "fine"; it means nobody has raised anything.
    dispute_reason: str | None = None
    # Present so a reader can tell "nobody has vouched" from "people vouched and
    # those vouchings were disqualified".
    flagged_attestor_count: int = 0


class FlagActorRequest(BaseModel):
    """Admin action. The reason is required and is shown on every item it touches."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=8, max_length=1000)


class FlagActorResponse(BaseModel):
    """What a flag or a clear actually changed, reported back plainly."""

    actor_id: uuid.UUID
    fraud_flagged: bool
    items_affected: int
    attestations_affected: int
    # True when the actor was already in the requested state and nothing moved.
    already_in_state: bool = False


class DisputeResponse(BaseModel):
    """One open reason a record is contested."""

    model_config = ConfigDict(from_attributes=True)

    item_id: uuid.UUID
    source: DisputeSource
    reason: str
    raised_at: UtcDatetime
