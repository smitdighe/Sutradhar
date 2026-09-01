"""Shared setup for the Phase 12 suites.

Six new files all need the same four things -- a category catalogue, a user in a
given role, a bearer header, and a registered-and-tagged item -- and six copies
of them would drift. Everything here goes through the real HTTP API wherever an
API exists, so a helper cannot accidentally create state the application itself
could never produce.

Deliberately not named ``test_*``: pytest collects by filename, and a module of
helpers that gets collected reports its fixtures as passing tests.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from seeds.loader import load_categories
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog import registry
from app.config import get_settings
from app.core.crypto_shred import new_salt
from app.db.models.catalog import Item
from app.db.models.enums import UserRole, UserStatus
from app.db.models.user import User

__all__ = [
    "API",
    "ASSAM",
    "GUJARAT",
    "PASSWORD",
    "PATOLA",
    "auth_headers",
    "idempotency",
    "issue_tag",
    "load_catalogue",
    "make_user",
    "register_item",
    "tag_code_of",
    "tagged_item",
]

API = get_settings().api_prefix
PASSWORD = "correct-horse-battery-staple"

# Edge headers, the same shape a real deployment receives from Vercel or
# Cloudflare. Two states roughly 2,000 km apart, which is what makes the
# velocity rule fire when they are seconds apart.
GUJARAT = {"X-Geo-Country": "IN", "X-Geo-Region": "GJ"}
ASSAM = {"X-Geo-Country": "IN", "X-Geo-Region": "AS"}

PATOLA: dict[str, Any] = {
    "warp_count": 120,
    "weft_count": 116,
    "dye_type": "natural",
    "double_ikat": True,
    "loom_type": "pit",
    "weave_days": 210,
    "gi_registration_no": "GI-00232",
}


def idempotency() -> dict[str, str]:
    """A fresh Idempotency-Key header. Required on every mutating POST."""
    return {"Idempotency-Key": uuid.uuid4().hex}


async def load_catalogue(session: AsyncSession) -> None:
    """Seed the GI categories and drop the in-process registry cache."""
    await load_categories(session)
    await session.commit()
    registry.invalidate()


async def make_user(
    session: AsyncSession,
    role: UserRole,
    *,
    status: UserStatus = UserStatus.ACTIVE,
    prefix: str = "p12",
    **columns: Any,
) -> User:
    """One user with a password, in a given role."""
    from app.auth.password import hash_password

    user = User(
        email=f"{prefix}-{role.lower()}-{uuid.uuid4().hex[:8]}@example.com",
        password_hash=hash_password(PASSWORD),
        display_name=f"Test {role.title()}",
        role=role,
        status=status,
        identity_salt=new_salt(),
        **columns,
    )
    session.add(user)
    await session.commit()
    return user


async def auth_headers(client: httpx.AsyncClient, user: User) -> dict[str, str]:
    """Log in through the real endpoint and return the bearer header."""
    response = await client.post(
        f"{API}/auth/login", json={"email": user.email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def register_item(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    category_slug: str = "patola-silk",
    attributes: dict[str, Any] | None = None,
    quantity: str = "12.0000",
    quantity_unit: str = "metre",
) -> uuid.UUID:
    """Register one item through the API. Returns its id."""
    response = await client.post(
        f"{API}/items",
        json={
            "category_slug": category_slug,
            "attributes": attributes if attributes is not None else PATOLA,
            "quantity": quantity,
            "quantity_unit": quantity_unit,
        },
        headers={**headers, **idempotency()},
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def issue_tag(
    client: httpx.AsyncClient, headers: dict[str, str], item_id: uuid.UUID
) -> str:
    """Issue a tag through the API. Returns the bare code."""
    response = await client.post(
        f"{API}/items/{item_id}/tag", headers={**headers, **idempotency()}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["tag_code"])


async def tagged_item(
    client: httpx.AsyncClient, session: AsyncSession, **register_kwargs: Any
) -> tuple[User, dict[str, str], uuid.UUID, str]:
    """A weaver, their bearer header, one registered item, and its tag code.

    The whole chain through the API: category seed, account, login, register,
    tag. Four lines in a test instead of forty, and no shortcut that produces
    state the application could not.
    """
    await load_catalogue(session)
    weaver = await make_user(session, UserRole.WEAVER, region="Gujarat")
    headers = await auth_headers(client, weaver)
    item_id = await register_item(client, headers, **register_kwargs)
    code = await issue_tag(client, headers, item_id)
    return weaver, headers, item_id, code


async def tag_code_of(session: AsyncSession, item_id: uuid.UUID) -> str | None:
    """Read an item's bound tag code straight from the row."""
    return (
        await session.execute(select(Item.tag_code).where(Item.id == item_id))
    ).scalar_one_or_none()
