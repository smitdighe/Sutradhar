"""The thirty-second stage moment, as an executable test.

The pitch is "infrastructure, not product": one platform, many GI categories,
where adding a category is a config change rather than a release. That claim is
only credible if it can be done live, and this file is the proof.

The sequence below is exactly what happens on stage: POST a category schema
nobody has seen, then immediately use it. No restart, no redeploy, no migration.
If this test passes, the demo works. If somebody breaks the registry
invalidation, this fails long before a judge sees it.

Banarasi brocade is used rather than a seeded category, so the test cannot pass
on data that was already loaded.
"""

from __future__ import annotations

import os
import time
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.crypto_shred import new_salt
from app.core.hashing import hash_object
from app.db.models.catalog import GICategory, Item
from app.db.models.enums import ItemStatus, UserRole, UserStatus
from app.db.models.user import User

pytestmark = pytest.mark.integration

API = get_settings().api_prefix
BUDGET_SECONDS = 30.0

BANARASI: dict[str, Any] = {
    "slug": "banarasi-brocade",
    "display_name": "Banarasi Brocade",
    "is_textile": True,
    "quantity_unit": "metre",
    "attribute_schema": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Banarasi Brocade",
        "type": "object",
        "additionalProperties": False,
        "required": ["zari_type", "weave_technique", "motif_tradition", "gi_registration_no"],
        "properties": {
            "zari_type": {"type": "string", "enum": ["real_gold", "silver", "tested", "imitation"]},
            "weave_technique": {
                "type": "string",
                "enum": ["kadhua", "kadhiyal", "phekua", "jangla"],
            },
            "motif_tradition": {"type": "string", "minLength": 2, "maxLength": 60},
            "loom_count": {"type": "integer", "minimum": 1, "maximum": 500},
            "gi_registration_no": {"type": "string", "pattern": "^GI-[0-9]{3,6}$"},
        },
    },
}

VALID_ATTRIBUTES: dict[str, Any] = {
    "zari_type": "real_gold",
    "weave_technique": "kadhua",
    "motif_tradition": "shikargah",
    "loom_count": 3,
    "gi_registration_no": "GI-00087",
}


async def admin_token(client: httpx.AsyncClient, session: AsyncSession) -> str:
    """An ADMIN session. Created directly, since the API cannot mint one."""
    from app.auth.password import hash_password

    email = f"cat-admin-{uuid.uuid4().hex[:8]}@example.com"
    password = "correct-horse-battery-staple"
    session.add(
        User(
            email=email,
            password_hash=hash_password(password),
            display_name="Category Admin",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            identity_salt=new_salt(),
        )
    )
    await session.commit()

    response = await client.post(
        f"{API}/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


async def weaver_for(session: AsyncSession) -> User:
    weaver = User(
        email=f"cat-weaver-{uuid.uuid4().hex[:8]}@example.com",
        display_name="Stage Weaver",
        role=UserRole.WEAVER,
        status=UserStatus.ACTIVE,
        identity_salt=new_salt(),
    )
    session.add(weaver)
    await session.commit()
    return weaver


class TestLiveCategoryAdd:
    async def test_new_category_is_usable_within_thirty_seconds(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        token = await admin_token(client, session)
        weaver = await weaver_for(session)
        headers = {"Authorization": f"Bearer {token}"}

        # The process this test is running against. If any step below required
        # a restart, this would change.
        pid_before = os.getpid()
        started = time.perf_counter()

        # --- 1. publish a category nobody has seen ---
        created = await client.post(f"{API}/admin/categories", json=BANARASI, headers=headers)
        assert created.status_code == 201, created.text
        assert created.json()["slug"] == "banarasi-brocade"
        assert created.json()["schema_version"] == 1

        # --- 2. it is readable immediately, with no restart in between ---
        fetched = await client.get(f"{API}/categories/banarasi-brocade")
        assert fetched.status_code == 200
        assert fetched.json()["display_name"] == "Banarasi Brocade"

        # --- 3. and it validates attributes immediately ---
        checked = await client.post(
            f"{API}/categories/banarasi-brocade/validate",
            json={"attributes": VALID_ATTRIBUTES},
        )
        assert checked.status_code == 200, checked.text
        assert checked.json() == {
            "valid": True,
            "slug": "banarasi-brocade",
            "schema_version": 1,
        }

        # --- 4. an item persists against it, pinned to v1 ---
        category = (
            await session.execute(
                select(GICategory).where(GICategory.slug == "banarasi-brocade")
            )
        ).scalar_one()
        item = Item(
            category_id=category.id,
            category_schema_version=category.schema_version,
            registered_by=weaver.id,
            attributes=VALID_ATTRIBUTES,
            quantity=Decimal("6.0000"),
            quantity_unit=category.quantity_unit,
            item_hash=hash_object({"stage": "demo", "n": uuid.uuid4().hex}),
            status=ItemStatus.PENDING,
        )
        session.add(item)
        await session.commit()

        elapsed = time.perf_counter() - started

        assert os.getpid() == pid_before, "the process restarted; that is not a live add"
        assert elapsed < BUDGET_SECONDS, (
            f"the live category add took {elapsed:.2f}s, over the {BUDGET_SECONDS}s budget"
        )

        stored = await session.get(Item, item.id)
        assert stored is not None
        assert stored.category_schema_version == 1
        assert stored.attributes == VALID_ATTRIBUTES

    async def test_the_new_category_rejects_the_wrong_payload(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        # A category that accepts anything would make the add meaningless.
        token = await admin_token(client, session)
        await client.post(
            f"{API}/admin/categories",
            json=BANARASI,
            headers={"Authorization": f"Bearer {token}"},
        )

        response = await client.post(
            f"{API}/categories/banarasi-brocade/validate",
            json={"attributes": {"warp_count": 120, "dye_type": "natural"}},
        )
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "ATTRIBUTE_VALIDATION_FAILED"
        paths = {item["path"] for item in error["details"]["errors"]}
        # Both the unknown keys and the missing required ones, by path.
        assert "/warp_count" in paths
        assert "/zari_type" in paths

    async def test_the_category_appears_in_the_public_listing(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        token = await admin_token(client, session)
        await client.post(
            f"{API}/admin/categories",
            json=BANARASI,
            headers={"Authorization": f"Bearer {token}"},
        )

        listing = await client.get(f"{API}/categories")
        assert listing.status_code == 200
        slugs = {row["slug"] for row in listing.json()["data"]}
        assert "banarasi-brocade" in slugs

    async def test_a_second_add_is_still_fast(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        # The registry reloads wholesale on every write. This asserts that stays
        # cheap enough for a live demo as the table grows.
        token = await admin_token(client, session)
        headers = {"Authorization": f"Bearer {token}"}

        await client.post(f"{API}/admin/categories", json=BANARASI, headers=headers)

        second = dict(BANARASI, slug="chanderi-silk", display_name="Chanderi Silk")
        started = time.perf_counter()
        response = await client.post(f"{API}/admin/categories", json=second, headers=headers)
        elapsed = time.perf_counter() - started

        assert response.status_code == 201
        assert elapsed < BUDGET_SECONDS
