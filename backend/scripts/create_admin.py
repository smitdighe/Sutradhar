"""Create the first ADMIN. The only path by which one comes into existence.

ADMIN is not self-assignable: ``/auth/register`` and ``/auth/oauth/complete``
both refuse it (see ``app/auth/roles.py`` and
``tests/integration/test_no_admin_escalation.py``). So the role has to enter the
system from outside the API, and this script is that door -- deliberately a
single, auditable one. After the first admin exists, further admins are granted
by an existing admin through the Phase 8 routes.

Hashing goes through :func:`app.auth.password.hash_password`, the same function
the login path verifies against. Writing a hash any other way would produce an
account that cannot log in, and the failure would look like a password problem
rather than a seeding bug.

Refuses to run when:

* SEED_ADMIN_PASSWORD is unset or shorter than 12 characters
* APP_ENV=production and the password is still the .env.example placeholder

Idempotent: run it twice and there is exactly one admin, exit 0 both times. The
second run reports and changes nothing -- in particular it does not reset the
password of an admin that already exists.

Usage::

    uv run python scripts/create_admin.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.auth.password import MIN_PASSWORD_LENGTH, hash_password  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core.crypto_shred import new_salt  # noqa: E402
from app.db.models.enums import UserRole, UserStatus  # noqa: E402
from app.db.models.user import User  # noqa: E402

EXIT_OK = 0
EXIT_REFUSED = 1

# The literal in .env.example. An admin password that is still this value has
# not been chosen by anybody.
PLACEHOLDER_PASSWORD = "change_me_locally"


def validate() -> str | None:
    """Return an error message, or None when it is safe to proceed."""
    settings = get_settings()
    password = settings.seed_admin_password

    if not password:
        return (
            "SEED_ADMIN_PASSWORD is not set. Set it in backend/.env before creating "
            "an administrator."
        )
    if len(password) < MIN_PASSWORD_LENGTH:
        return (
            f"SEED_ADMIN_PASSWORD is {len(password)} characters; the minimum is "
            f"{MIN_PASSWORD_LENGTH}. This account can grant every other role, so it "
            "does not get an exemption from the password policy."
        )
    if settings.is_production and password == PLACEHOLDER_PASSWORD:
        return (
            "SEED_ADMIN_PASSWORD is still the .env.example placeholder and APP_ENV is "
            "production. Choose a real password."
        )
    if not settings.seed_admin_email:
        return "SEED_ADMIN_EMAIL is not set."
    return None


async def main() -> int:
    settings = get_settings()

    problem = validate()
    if problem is not None:
        print(f"refusing to create an administrator:\n  {problem}")
        return EXIT_REFUSED

    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    try:
        async with factory() as session:
            # citext, so this also catches a differently-cased duplicate.
            existing = (
                await session.execute(
                    select(User).where(User.email == settings.seed_admin_email)
                )
            ).scalar_one_or_none()

            if existing is not None:
                # Deliberately does not touch the row. Re-running a bootstrap
                # script must never silently reset a live admin's password.
                print(f"admin already exists: {existing.email}  role={existing.role}")
                print("  no changes made")
                return EXIT_OK

            admin = User(
                email=settings.seed_admin_email,
                password_hash=hash_password(settings.seed_admin_password),
                display_name="Sutradhar Administrator",
                role=UserRole.ADMIN,
                status=UserStatus.ACTIVE,
                identity_salt=new_salt(),
            )
            session.add(admin)
            await session.commit()

        print(f"admin created: {settings.seed_admin_email}")
        print("  role=ADMIN status=ACTIVE")
        print(f"  sign in at POST {settings.api_prefix}/auth/login")
        return EXIT_OK
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
