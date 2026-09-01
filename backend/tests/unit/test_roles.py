"""The privilege-escalation guard.

The classification test below is the point of this file. Adding a sixth role
without deciding whether it is self-assignable makes it fail, which is exactly
the moment somebody has to think about whether a stranger may claim it. Without
this, a new role silently inherits whatever the default branch happens to do.
"""

from __future__ import annotations

import pytest

from app.auth.roles import (
    GRANTABLE_ONLY_ROLES,
    SELF_ASSIGNABLE_ROLES,
    Role,
    is_self_assignable,
)
from app.db.models.enums import UserRole

pytestmark = pytest.mark.unit

# Every role, classified by hand. This list is the thing a new role breaks.
EXPECTED_CLASSIFICATION: dict[Role, bool] = {
    Role.CONSUMER: True,  # the default; carries no authority
    Role.WEAVER: True,  # claimable, but lands in PENDING_VERIFICATION
    Role.COOP_OFFICER: False,  # attests to others' items
    Role.INSPECTOR: False,  # attests to others' items
    Role.ADMIN: False,  # grants roles
}


class TestClassificationIsExhaustive:
    def test_every_role_is_explicitly_classified(self) -> None:
        unclassified = set(Role) - set(EXPECTED_CLASSIFICATION)
        assert unclassified == set(), (
            f"role(s) {sorted(map(str, unclassified))} have no explicit self-assignable "
            "decision. Add them to EXPECTED_CLASSIFICATION and to the correct set in "
            "app/auth/roles.py -- do not let a new role default into either bucket."
        )

    def test_no_phantom_roles_in_the_expectation(self) -> None:
        assert set(EXPECTED_CLASSIFICATION) <= set(Role)

    @pytest.mark.parametrize(("role", "expected"), list(EXPECTED_CLASSIFICATION.items()))
    def test_classification_matches(self, role: Role, expected: bool) -> None:
        assert is_self_assignable(role) is expected
        assert (role in SELF_ASSIGNABLE_ROLES) is expected

    def test_the_two_sets_partition_the_enum(self) -> None:
        assert frozenset(Role) == SELF_ASSIGNABLE_ROLES | GRANTABLE_ONLY_ROLES
        assert frozenset() == SELF_ASSIGNABLE_ROLES & GRANTABLE_ONLY_ROLES


class TestPrivilegedRoles:
    @pytest.mark.parametrize("role", [Role.ADMIN, Role.INSPECTOR, Role.COOP_OFFICER])
    def test_privileged_roles_are_never_self_assignable(self, role: Role) -> None:
        assert not is_self_assignable(role)
        assert role in GRANTABLE_ONLY_ROLES

    def test_admin_is_not_self_assignable(self) -> None:
        # Called out separately because it is the one that matters most.
        assert Role.ADMIN not in SELF_ASSIGNABLE_ROLES


class TestEnumIdentity:
    def test_role_is_the_database_enum_not_a_copy(self) -> None:
        # A parallel enum could drift from the `user_role` Postgres type, and
        # the first symptom would be a value that validates and then fails to
        # write. Aliasing makes that impossible.
        assert Role is UserRole

    def test_role_has_exactly_five_members(self) -> None:
        assert len(Role) == 5
