"""Roles and which of them a user may claim for themselves.

``Role`` is an alias of :class:`~app.db.models.enums.UserRole`, not a second
enum. A parallel declaration would be free to drift from the ``user_role``
Postgres type, and the first symptom of that drift would be a role that passes
validation and then fails to write.

The self-assignable set is the privilege-escalation boundary. Anything outside
it has to be granted by an existing admin, recorded as a ``ROLE_GRANT`` auth
event.
"""

from __future__ import annotations

from app.db.models.enums import UserRole

__all__ = [
    "GRANTABLE_ONLY_ROLES",
    "SELF_ASSIGNABLE_ROLES",
    "Role",
    "is_self_assignable",
]

Role = UserRole

# Roles a user may pick at registration.
#
# CONSUMER is the default and carries no authority. WEAVER is claimable, but a
# self-declared weaver lands in PENDING_VERIFICATION rather than ACTIVE -- see
# app.auth.service.register -- so claiming it grants nothing until a human
# verifies it.
SELF_ASSIGNABLE_ROLES: frozenset[Role] = frozenset({Role.CONSUMER, Role.WEAVER})

# Everything else. COOP_OFFICER and INSPECTOR attest to other people's items,
# and ADMIN can grant roles; all three have to be granted, never claimed.
GRANTABLE_ONLY_ROLES: frozenset[Role] = frozenset(Role) - SELF_ASSIGNABLE_ROLES


def is_self_assignable(role: Role) -> bool:
    """True when *role* may be chosen by the registering user themselves."""
    return role in SELF_ASSIGNABLE_ROLES
