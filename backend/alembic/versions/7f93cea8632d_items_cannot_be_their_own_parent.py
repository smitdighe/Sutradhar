"""items cannot be their own parent

A one-node cycle (``parent_id = id``) would make every recursive CTE in
``app/provenance/tree.py`` spin until its depth ceiling, and would represent a
provenance chain that contains itself. The CHECK makes it unrepresentable rather
than merely unlikely.

Longer cycles cannot be expressed as a CHECK -- they need a graph traversal --
so those are handled by the depth ceiling carried in every CTE.

Revision ID: 7f93cea8632d
Revises: bd57f40f45cc
Create Date: 2026-08-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "7f93cea8632d"
down_revision: str | None = "bd57f40f45cc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "no_self_parent",
        "items",
        "parent_id IS NULL OR parent_id <> id",
    )


def downgrade() -> None:
    op.drop_constraint("ck_items_no_self_parent", "items", type_="check")
