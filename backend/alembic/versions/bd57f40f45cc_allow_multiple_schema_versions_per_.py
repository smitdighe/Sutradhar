"""allow multiple schema versions per category slug

Phase 1 declared ``slug`` with ``unique=True`` *and* a composite
``UNIQUE (slug, schema_version)``. The column-level constraint makes the
composite one unreachable: inserting v2 of any category fails on
``uq_gi_categories_slug`` before the composite is ever consulted.

Since a versioned category is the whole point -- publishing v2 must not
retroactively invalidate v1 items -- the column-level constraint is dropped and
replaced with a plain index, which is what the lookups actually needed.

Revision ID: bd57f40f45cc
Revises: 7b72177ea11e
Create Date: 2026-08-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "bd57f40f45cc"
down_revision: str | None = "7b72177ea11e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the slug-only uniqueness; keep (slug, schema_version)."""
    op.drop_constraint("uq_gi_categories_slug", "gi_categories", type_="unique")
    # Lookups are by slug ("latest active version of patola-silk"), so the
    # index the unique constraint was providing still needs to exist.
    op.create_index("ix_gi_categories_slug", "gi_categories", ["slug"], unique=False)


def downgrade() -> None:
    """Restore slug-only uniqueness.

    This fails if more than one version of any slug exists, which is correct:
    those rows cannot be represented under the old constraint, and silently
    deleting them to make the downgrade succeed would be worse.
    """
    op.drop_index("ix_gi_categories_slug", table_name="gi_categories")
    op.create_unique_constraint("uq_gi_categories_slug", "gi_categories", ["slug"])
