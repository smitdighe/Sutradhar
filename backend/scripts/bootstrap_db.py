"""Bring a database up to a working, seeded state. One command, offline.

Not to be confused with ``scripts/bootstrap_db.sql``, which runs *once* as a
PostgreSQL superuser to create the ``sutradhar`` role and its two databases.
This script runs as ``sutradhar`` against a database that already exists, and
does the rest: migrations, then seed data.

Steps, in order::

    1. verify connectivity                (names the env var if it fails)
    2. alembic upgrade head
    3. load categories                    idempotent on (slug, schema_version)
    4. load users                         idempotent on email
    5. load items, attestations, scans    idempotent on the seed key
    6. print a summary

Running it twice is a no-op the second time -- no unique violation, no duplicate
categories, no duplicate users. That is what makes it safe to run against a
database somebody is already demoing on.

Usage::

    uv run python scripts/bootstrap_db.py
    uv run python scripts/bootstrap_db.py --dry-run
    uv run python scripts/bootstrap_db.py --categories-only
    uv run python scripts/bootstrap_db.py --reset --yes-i-mean-it
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from seeds.loader import (  # noqa: E402
    SeedReport,
    load_categories,
    load_items,
    load_reputation,
    load_users,
)
from sqlalchemy import func, select, text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.models.attestation import Attestation  # noqa: E402
from app.db.models.catalog import GICategory, Item  # noqa: E402
from app.db.models.scan import Scan  # noqa: E402
from app.db.models.user import User  # noqa: E402

EXIT_OK = 0
EXIT_FAILED = 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--reset",
        action="store_true",
        help="drop and recreate the schema before migrating (destructive)",
    )
    parser.add_argument(
        "--yes-i-mean-it",
        action="store_true",
        dest="confirmed",
        help="required alongside --reset; without it --reset refuses to run",
    )
    parser.add_argument(
        "--categories-only", action="store_true", help="load categories, skip users and items"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    return parser.parse_args(argv)


async def verify_connectivity() -> bool:
    """Connect, or explain exactly which variable to look at."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001 - the message is the point
        print(f"  ! cannot connect: {type(exc).__name__}: {exc}")
        print("  ! DATABASE_URL in backend/.env is what this script reads.")
        print(f"  ! currently: {settings.database_url.split('@')[-1]}")
        print("  ! is PostgreSQL running, and has scripts/bootstrap_db.sql been applied?")
        return False
    finally:
        await engine.dispose()
    return True


async def reset_schema() -> None:
    """Drop every table and type this project owns, then let Alembic rebuild."""
    engine = create_async_engine(get_settings().database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        # Enum types outlive their tables, and alembic_version is not in the
        # metadata, so neither is covered by drop_all.
        from app.db.models.enums import ALL_ENUM_TYPE_NAMES

        for name in ALL_ENUM_TYPE_NAMES:
            await connection.execute(text(f'DROP TYPE IF EXISTS "{name}" CASCADE'))
        await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    await engine.dispose()


def run_migrations() -> bool:
    """``alembic upgrade head`` as a subprocess, so its own env.py config applies."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        return False
    for line in result.stderr.splitlines():
        if "Running upgrade" in line:
            print(f"  {line.split('] ', 1)[-1]}")
    return True


def print_summary(reports: list[SeedReport], counts: dict[str, int], dry_run: bool) -> None:
    print()
    print(f"  {'WHAT':<14}{'CREATED':>9}{'EXISTED':>9}{'SKIPPED':>9}")
    print(f"  {'-' * 14}{'-' * 9:>9}{'-' * 9:>9}{'-' * 9:>9}")
    for report in reports:
        print(
            f"  {report.label:<14}{report.created:>9}{report.existed:>9}{report.skipped:>9}"
        )
    notes = [note for report in reports for note in report.notes]
    if notes:
        print("\n  notes:")
        for note in notes:
            print(f"    - {note}")
    if not dry_run:
        print("\n  database now holds:")
        for label, value in counts.items():
            print(f"    {label:<16}{value}")


async def current_counts(session_factory: async_sessionmaker) -> dict[str, int]:  # type: ignore[type-arg]
    async with session_factory() as session:
        result = {}
        for label, model in (
            ("categories", GICategory),
            ("users", User),
            ("items", Item),
            ("attestations", Attestation),
            ("scans", Scan),
        ):
            result[label] = int(
                (await session.execute(select(func.count()).select_from(model))).scalar_one()
            )
        return result


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()

    print(f"sutradhar bootstrap  env={settings.app_env}  dry_run={args.dry_run}")
    print()

    if args.reset:
        if not args.confirmed:
            print("  ! --reset drops every table in this database.")
            print("  ! re-run with --yes-i-mean-it if that is what you want.")
            return EXIT_FAILED
        if settings.is_production:
            # No flag combination should be able to wipe production from a
            # developer script.
            print("  ! refusing to --reset with APP_ENV=production.")
            return EXIT_FAILED
        if args.dry_run:
            print("  --reset ignored under --dry-run")
        else:
            print("  dropping schema ...")
            await reset_schema()

    print("1. connectivity")
    if not await verify_connectivity():
        return EXIT_FAILED
    print("   ok")

    print("2. migrations")
    if args.dry_run:
        print("   skipped (dry run)")
    elif not run_migrations():
        print("   ! alembic upgrade head failed")
        return EXIT_FAILED
    else:
        print("   at head")

    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    reports: list[SeedReport] = []

    try:
        async with factory() as session:
            print("3. categories")
            reports.append(await load_categories(session))

            if not args.categories_only:
                print("4. users")
                reports.append(await load_users(session))
                print("5. items")
                reports.append(await load_items(session))
                # Last: flagging an actor disputes everything they
                # registered, so the items have to exist first.
                reports.append(await load_reputation(session))

            if args.dry_run:
                # Everything above really ran; discarding it here is what makes
                # the preview trustworthy.
                await session.rollback()
            else:
                await session.commit()

        counts = {} if args.dry_run else await current_counts(factory)
    finally:
        await engine.dispose()

    print_summary(reports, counts, args.dry_run)
    print()
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
