"""Alembic environment and this project's migration conventions.

The database URL is never stored in ``alembic.ini``; it comes from
``DATABASE_URL_SYNC`` via :mod:`app.config`, so migrations and the running
service read the same configuration. Both offline and online modes are wired.

Conventions, which apply to every revision from here on:

**One head, always.** ``uv run alembic heads`` prints exactly one line. When two
branches both add a revision, resolve it with ``alembic merge``, never by
hand-editing ``down_revision`` -- an edited revision that somebody has already
applied leaves their database claiming an ancestry it does not have.

**Every migration is reversible.** ``downgrade()`` is written, not stubbed with
``pass``. The test is ``alembic downgrade base && alembic upgrade head``, which
is what catches the thing autogenerate always misses: enum types outlive their
tables, so a ``downgrade`` that drops tables without dropping types makes the
*next* upgrade fail on CREATE TYPE.

**Autogenerate is a draft.** It is a starting point to be read line by line, not
an output to commit. It reliably gets wrong, or silently omits:

* native enum creation order, and DROP TYPE on downgrade (see above)
* ``ON DELETE`` clauses -- it will happily emit a foreign key with none
* partial and composite indexes, and index ordering
* server defaults, especially expression defaults
* ``citext`` and other extension types, including the CREATE EXTENSION they need

**Data migrations are separate revisions from schema migrations.** A revision
either changes shape or moves rows, never both. Mixing them means a failure
halfway through leaves a schema you cannot reason about, and it makes the data
step impossible to re-run on its own.

**Never edit an applied migration.** Once a revision has run anywhere other than
your own machine, it is immutable. Fix it forward with a new revision.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings  # noqa: E402
from app.db.base import target_metadata  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url_sync)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to a database."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_schemas=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_schemas=False,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
