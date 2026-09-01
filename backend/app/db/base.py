"""Declarative base and the metadata object Alembic autogenerates against.

Every model module is imported at the bottom of this file. Importing
:mod:`app.db.base` alone is therefore enough to populate ``Base.metadata``,
which is what ``alembic/env.py`` relies on -- a model that is not reachable
from here is invisible to autogenerate and silently never gets a table.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Explicit constraint naming keeps Alembic autogenerate deterministic and gives
# every index and constraint a predictable name to reference in a migration.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Common declarative base for every ORM model in the service."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# Imported for their side effect of registering tables on Base.metadata.
# Order does not matter here; the migration controls DDL ordering.
from app.db.models import (  # noqa: E402,F401
    attestation,
    catalog,
    chain,
    media,
    oauth_state,
    ops,
    outbox,
    scan,
    user,
)

target_metadata = Base.metadata

__all__ = ["Base", "target_metadata"]
