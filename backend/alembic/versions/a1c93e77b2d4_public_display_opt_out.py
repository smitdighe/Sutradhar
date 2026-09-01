"""let a maker withdraw from the public verification page

The public page names the person who made the object. That is most of the point
of the record for them -- and it is still their choice, so it has to be
withdrawable without deleting anything.

Withdrawal is deliberately *not* crypto-shredding. Deleting ``identity_salt``
makes an anchored hash permanently unlinkable and cannot be undone; this flag
only stops the display name and region from being projected into the public
payload. The provenance chain is untouched and still verifies, which is what
lets the choice be reversed.

Default false, and NOT NULL: a nullable flag would make "has not decided" and
"has not opted out" the same value in the one place where the difference would
have to be guessed at read time.

Revision ID: a1c93e77b2d4
Revises: f4a71c9e2d80
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c93e77b2d4"
down_revision: str | None = "f4a71c9e2d80"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "public_display_opt_out",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "public_display_opt_out")
