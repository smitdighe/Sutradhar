"""initial empty baseline

Scaffold only: establishes the alembic_version row so later migrations have a
parent. Creates no tables.

Revision ID: 6ab133d553ca
Revises:
Create Date: 2026-08-26
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "6ab133d553ca"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op baseline."""


def downgrade() -> None:
    """No-op baseline."""
