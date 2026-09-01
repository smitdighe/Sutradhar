"""Category schema engine: validation, versioning, retirement, authorization."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
from seeds.loader import load_categories
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog import registry
from app.catalog.validator import MAX_NESTING_DEPTH, MAX_PROPERTIES
from app.config import get_settings
from app.core.crypto_shred import new_salt
from app.core.hashing import hash_object
from app.db.models.catalog import GICategory, Item
from app.db.models.enums import ItemStatus, UserRole, UserStatus
from app.db.models.user import User

pytestmark = pytest.mark.integration

API = get_settings().api_prefix
PASSWORD = "correct-horse-battery-staple"

PATOLA_OK: dict[str, Any] = {
    "warp_count": 120,
    "weft_count": 116,
    "dye_type": "natural",
    "double_ikat": True,
    "loom_type": "pit",
    "weave_days": 210,
    "gi_registration_no": "GI-00232",
}
KOLHAPURI_OK: dict[str, Any] = {
    "leather_type": "buffalo",
    "tanning_method": "vegetable",
    "sole_thickness_mm": 8.5,
    "braid_pattern": "kapshi",
    "artisan_cluster": "Kolhapur North",
}

MINIMAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name"],
    "properties": {"name": {"type": "string"}},
}


async def make_user(session: AsyncSession, role: UserRole) -> tuple[User, str]:
    from app.auth.password import hash_password

    email = f"cat-{role.lower()}-{uuid.uuid4().hex[:8]}@example.com"
    user = User(
        email=email,
        password_hash=hash_password(PASSWORD),
        display_name=f"Test {role}",
        role=role,
        status=UserStatus.ACTIVE,
        identity_salt=new_salt(),
    )
    session.add(user)
    await session.commit()
    return user, email


async def token_for(client: httpx.AsyncClient, email: str) -> str:
    response = await client.post(
        f"{API}/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


@pytest.fixture
async def admin_headers(
    client: httpx.AsyncClient, session: AsyncSession
) -> dict[str, str]:
    _, email = await make_user(session, UserRole.ADMIN)
    return {"Authorization": f"Bearer {await token_for(client, email)}"}


@pytest.fixture
async def seeded(session: AsyncSession) -> None:
    await load_categories(session)
    await session.commit()
    registry.invalidate()


def make_category(slug: str, **overrides: Any) -> dict[str, Any]:
    return {
        "slug": slug,
        "display_name": slug.replace("-", " ").title(),
        "is_textile": True,
        "quantity_unit": "metre",
        "attribute_schema": MINIMAL_SCHEMA,
        **overrides,
    }


# ---------------------------------------------------------------- cross-rejection


class TestCategoriesRejectEachOther:
    """Two structurally different categories, each refusing the other's payload.

    This is the platform claim under test: the engine is not Patola-shaped with
    other categories bolted on, it genuinely enforces whatever schema it holds.
    """

    async def test_patola_accepts_its_own(
        self, client: httpx.AsyncClient, seeded: None
    ) -> None:
        response = await client.post(
            f"{API}/categories/patola-silk/validate", json={"attributes": PATOLA_OK}
        )
        assert response.status_code == 200
        assert response.json()["valid"] is True

    async def test_kolhapuri_accepts_its_own(
        self, client: httpx.AsyncClient, seeded: None
    ) -> None:
        response = await client.post(
            f"{API}/categories/kolhapuri-chappal/validate", json={"attributes": KOLHAPURI_OK}
        )
        assert response.status_code == 200

    async def test_patola_rejects_kolhapuri_attributes_by_field(
        self, client: httpx.AsyncClient, seeded: None
    ) -> None:
        response = await client.post(
            f"{API}/categories/patola-silk/validate", json={"attributes": KOLHAPURI_OK}
        )
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "ATTRIBUTE_VALIDATION_FAILED"
        paths = {item["path"] for item in error["details"]["errors"]}
        # Kolhapuri's keys named as unrecognised, Patola's named as missing.
        assert "/leather_type" in paths
        assert "/warp_count" in paths

    async def test_kolhapuri_rejects_patola_attributes_by_field(
        self, client: httpx.AsyncClient, seeded: None
    ) -> None:
        response = await client.post(
            f"{API}/categories/kolhapuri-chappal/validate", json={"attributes": PATOLA_OK}
        )
        assert response.status_code == 422
        paths = {item["path"] for item in response.json()["error"]["details"]["errors"]}
        assert "/warp_count" in paths
        assert "/leather_type" in paths


# ---------------------------------------------------------------- attribute errors


class TestAttributeErrors:
    async def test_unknown_key_is_named(
        self, client: httpx.AsyncClient, seeded: None
    ) -> None:
        # A typo'd key must be rejected, not absorbed into JSONB where nobody
        # sees it again.
        bad = {**PATOLA_OK, "warp_cout": 120}
        response = await client.post(
            f"{API}/categories/patola-silk/validate", json={"attributes": bad}
        )
        assert response.status_code == 422
        errors = response.json()["error"]["details"]["errors"]
        offending = [item for item in errors if item["path"] == "/warp_cout"]
        assert offending, errors
        assert "warp_cout" in offending[0]["message"]

    async def test_missing_required_key_is_named(
        self, client: httpx.AsyncClient, seeded: None
    ) -> None:
        incomplete = {key: value for key, value in PATOLA_OK.items() if key != "dye_type"}
        response = await client.post(
            f"{API}/categories/patola-silk/validate", json={"attributes": incomplete}
        )
        assert response.status_code == 422
        errors = response.json()["error"]["details"]["errors"]
        assert any(item["path"] == "/dye_type" for item in errors), errors

    async def test_wrong_type_reports_the_path(
        self, client: httpx.AsyncClient, seeded: None
    ) -> None:
        response = await client.post(
            f"{API}/categories/patola-silk/validate",
            json={"attributes": {**PATOLA_OK, "warp_count": "one hundred"}},
        )
        assert response.status_code == 422
        errors = response.json()["error"]["details"]["errors"]
        offending = [item for item in errors if item["path"] == "/warp_count"]
        assert offending
        assert "integer" in offending[0]["message"]

    async def test_enum_violation_reports_the_path(
        self, client: httpx.AsyncClient, seeded: None
    ) -> None:
        response = await client.post(
            f"{API}/categories/patola-silk/validate",
            json={"attributes": {**PATOLA_OK, "dye_type": "fluorescent"}},
        )
        assert response.status_code == 422
        assert any(
            item["path"] == "/dye_type"
            for item in response.json()["error"]["details"]["errors"]
        )

    async def test_pattern_violation_reports_the_path(
        self, client: httpx.AsyncClient, seeded: None
    ) -> None:
        response = await client.post(
            f"{API}/categories/patola-silk/validate",
            json={"attributes": {**PATOLA_OK, "gi_registration_no": "not-a-gi-number"}},
        )
        assert response.status_code == 422
        assert any(
            item["path"] == "/gi_registration_no"
            for item in response.json()["error"]["details"]["errors"]
        )

    async def test_every_error_is_reported_at_once(
        self, client: httpx.AsyncClient, seeded: None
    ) -> None:
        # A weaver filling a form should see everything wrong with it in one
        # round trip, not discover the next problem after fixing the first.
        response = await client.post(
            f"{API}/categories/patola-silk/validate",
            json={"attributes": {"warp_count": "wrong", "junk": 1}},
        )
        assert response.status_code == 422
        errors = response.json()["error"]["details"]["errors"]
        assert len(errors) > 3

    async def test_never_leaks_a_traceback(
        self, client: httpx.AsyncClient, seeded: None
    ) -> None:
        response = await client.post(
            f"{API}/categories/patola-silk/validate", json={"attributes": {"a": [1, {"b": 2}]}}
        )
        assert response.status_code == 422
        body = response.text
        assert "Traceback" not in body
        assert "jsonschema" not in body


# ---------------------------------------------------------------- schema meta-validation


class TestSchemaMetaValidation:
    async def test_malformed_schema_is_rejected_and_creates_nothing(
        self, client: httpx.AsyncClient, session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        before = (
            await session.execute(select(func.count()).select_from(GICategory))
        ).scalar_one()

        response = await client.post(
            f"{API}/admin/categories",
            json=make_category(
                "broken-cat", attribute_schema={"type": "object", "required": "not-a-list"}
            ),
            headers=admin_headers,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_CATEGORY_SCHEMA"

        after = (
            await session.execute(select(func.count()).select_from(GICategory))
        ).scalar_one()
        assert after == before

    async def test_error_names_the_location(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        response = await client.post(
            f"{API}/admin/categories",
            json=make_category(
                "bad-type",
                attribute_schema={
                    "type": "object",
                    "properties": {"x": {"type": "not-a-json-type"}},
                },
            ),
            headers=admin_headers,
        )
        assert response.status_code == 422
        errors = response.json()["error"]["details"]["errors"]
        assert errors
        assert any("/" in item["path"] for item in errors)

    @pytest.mark.parametrize(
        "ref",
        [
            "https://evil.example.com/schema.json",
            "http://169.254.169.254/latest/meta-data",
            "file:///etc/passwd",
        ],
    )
    async def test_remote_ref_is_rejected(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str], ref: str
    ) -> None:
        # A schema that fetches over the network at validation time is a demo
        # that dies on conference Wi-Fi and an SSRF vector besides.
        response = await client.post(
            f"{API}/admin/categories",
            json=make_category(
                "remote-ref",
                attribute_schema={
                    "type": "object",
                    "properties": {"x": {"$ref": ref}},
                },
            ),
            headers=admin_headers,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_CATEGORY_SCHEMA"
        assert any(
            "remote $ref" in item["message"]
            for item in response.json()["error"]["details"]["errors"]
        )

    async def test_local_ref_is_allowed(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        # Offline-resolvable references are legitimate and must keep working.
        response = await client.post(
            f"{API}/admin/categories",
            json=make_category(
                "local-ref",
                attribute_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "$defs": {"grade": {"type": "string", "enum": ["a", "b"]}},
                    "properties": {"grade": {"$ref": "#/$defs/grade"}},
                },
            ),
            headers=admin_headers,
        )
        assert response.status_code == 201

    async def test_additional_properties_false_is_injected(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        # Submitted open; stored closed. A typo'd attribute key must be
        # rejected, not silently absorbed.
        response = await client.post(
            f"{API}/admin/categories",
            json=make_category(
                "open-cat",
                attribute_schema={
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}},
                },
            ),
            headers=admin_headers,
        )
        assert response.status_code == 201
        assert response.json()["attribute_schema"]["additionalProperties"] is False

        rejected = await client.post(
            f"{API}/categories/open-cat/validate",
            json={"attributes": {"name": "x", "sneaky": 1}},
        )
        assert rejected.status_code == 422

    async def test_oversized_schema_is_rejected(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        huge = {
            "type": "object",
            "properties": {
                f"field_{index}": {"type": "string", "description": "x" * 200}
                for index in range(500)
            },
        }
        response = await client.post(
            f"{API}/admin/categories",
            json=make_category("huge-cat", attribute_schema=huge),
            headers=admin_headers,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_CATEGORY_SCHEMA"

    async def test_too_many_properties_is_rejected(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        wide = {
            "type": "object",
            "properties": {f"f{index}": {"type": "string"} for index in range(MAX_PROPERTIES + 1)},
        }
        response = await client.post(
            f"{API}/admin/categories",
            json=make_category("wide-cat", attribute_schema=wide),
            headers=admin_headers,
        )
        assert response.status_code == 422

    async def test_too_deep_is_rejected(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        node: dict[str, Any] = {"type": "string"}
        for _ in range(MAX_NESTING_DEPTH + 4):
            node = {"type": "object", "properties": {"child": node}}
        response = await client.post(
            f"{API}/admin/categories",
            json=make_category("deep-cat", attribute_schema=node),
            headers=admin_headers,
        )
        assert response.status_code == 422


# ---------------------------------------------------------------- versioning


class TestVersioning:
    async def test_v2_can_be_published(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        await client.post(
            f"{API}/admin/categories", json=make_category("ver-cat"), headers=admin_headers
        )
        response = await client.post(
            f"{API}/admin/categories/ver-cat/versions",
            json={
                "attribute_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "grade"],
                    "properties": {
                        "name": {"type": "string"},
                        "grade": {"type": "integer"},
                    },
                }
            },
            headers=admin_headers,
        )
        assert response.status_code == 200
        assert response.json()["category"]["schema_version"] == 2

    async def test_v2_reports_a_diff(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        # The operator sees the consequence at the moment they cause it.
        await client.post(
            f"{API}/admin/categories", json=make_category("diff-cat"), headers=admin_headers
        )
        response = await client.post(
            f"{API}/admin/categories/diff-cat/versions",
            json={
                "attribute_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["grade"],
                    "properties": {"grade": {"type": "integer"}},
                }
            },
            headers=admin_headers,
        )
        body = response.json()
        assert body["diff"]["added"] == ["grade"]
        assert body["diff"]["removed"] == ["name"]
        assert body["diff"]["newly_required"] == ["grade"]
        assert body["breaking"] is True

    async def test_an_additive_version_is_not_breaking(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        await client.post(
            f"{API}/admin/categories", json=make_category("additive-cat"), headers=admin_headers
        )
        response = await client.post(
            f"{API}/admin/categories/additive-cat/versions",
            json={
                "attribute_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "note": {"type": "string"},
                    },
                }
            },
            headers=admin_headers,
        )
        assert response.json()["breaking"] is False

    async def test_type_change_is_reported(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        await client.post(
            f"{API}/admin/categories", json=make_category("typed-cat"), headers=admin_headers
        )
        response = await client.post(
            f"{API}/admin/categories/typed-cat/versions",
            json={
                "attribute_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name"],
                    "properties": {"name": {"type": "integer"}},
                }
            },
            headers=admin_headers,
        )
        assert response.json()["diff"]["type_changed"] == [
            {"field": "name", "from": "string", "to": "integer"}
        ]

    async def test_publishing_v2_leaves_v1_items_valid(
        self, client: httpx.AsyncClient, session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        # The invariant the whole product rests on: immutability of the record.
        # A schema change that retroactively invalidated history would undo it.
        await client.post(
            f"{API}/admin/categories", json=make_category("pin-cat"), headers=admin_headers
        )
        v1_payload = {"name": "woven in 2026"}
        assert (
            await client.post(
                f"{API}/categories/pin-cat/validate", json={"attributes": v1_payload}
            )
        ).status_code == 200

        weaver, _ = await make_user(session, UserRole.WEAVER)
        category = (
            await session.execute(
                select(GICategory).where(
                    GICategory.slug == "pin-cat", GICategory.schema_version == 1
                )
            )
        ).scalar_one()
        item = Item(
            category_id=category.id,
            category_schema_version=1,
            registered_by=weaver.id,
            attributes=v1_payload,
            quantity=Decimal("1.0000"),
            quantity_unit=category.quantity_unit,
            item_hash=hash_object({"pin": uuid.uuid4().hex}),
            status=ItemStatus.PENDING,
        )
        session.add(item)
        await session.commit()

        # v2 removes `name` entirely and requires something else.
        await client.post(
            f"{API}/admin/categories/pin-cat/versions",
            json={
                "attribute_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["serial"],
                    "properties": {"serial": {"type": "string"}},
                }
            },
            headers=admin_headers,
        )

        # Against v2 the old payload is nonsense ...
        against_v2 = await client.post(
            f"{API}/categories/pin-cat/validate", json={"attributes": v1_payload}
        )
        assert against_v2.status_code == 422

        # ... but the item is pinned to v1, and against v1 it still verifies.
        against_v1 = await client.post(
            f"{API}/categories/pin-cat/validate",
            json={"attributes": v1_payload, "schema_version": 1},
        )
        assert against_v1.status_code == 200
        assert against_v1.json()["schema_version"] == 1

        stored = await session.get(Item, item.id)
        assert stored is not None
        assert stored.category_schema_version == 1

    async def test_version_history_is_listed(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        await client.post(
            f"{API}/admin/categories", json=make_category("hist-cat"), headers=admin_headers
        )
        await client.post(
            f"{API}/admin/categories/hist-cat/versions",
            json={"attribute_schema": MINIMAL_SCHEMA},
            headers=admin_headers,
        )
        response = await client.get(f"{API}/categories/hist-cat/versions")
        assert response.status_code == 200
        assert [row["schema_version"] for row in response.json()["data"]] == [1, 2]

    async def test_a_pinned_version_is_fetchable(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        await client.post(
            f"{API}/admin/categories", json=make_category("pinned-cat"), headers=admin_headers
        )
        response = await client.get(f"{API}/categories/pinned-cat/v/1")
        assert response.status_code == 200
        assert response.json()["schema_version"] == 1

    async def test_unknown_version_is_404(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        await client.post(
            f"{API}/admin/categories", json=make_category("only-v1"), headers=admin_headers
        )
        response = await client.get(f"{API}/categories/only-v1/v/9")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "CATEGORY_VERSION_NOT_FOUND"

    async def test_duplicate_slug_is_409(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        await client.post(
            f"{API}/admin/categories", json=make_category("dupe-cat"), headers=admin_headers
        )
        response = await client.post(
            f"{API}/admin/categories", json=make_category("dupe-cat"), headers=admin_headers
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CATEGORY_SLUG_EXISTS"


# ---------------------------------------------------------------- immutability


class TestSlugImmutability:
    async def test_patch_cannot_change_the_slug(
        self, client: httpx.AsyncClient, session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        # Slugs appear in URLs and on printed tags. The field is absent from
        # the schema, so no request shape reaches it.
        await client.post(
            f"{API}/admin/categories", json=make_category("stable-slug"), headers=admin_headers
        )
        response = await client.patch(
            f"{API}/admin/categories/stable-slug",
            json={"slug": "renamed-slug", "display_name": "Renamed"},
            headers=admin_headers,
        )
        assert response.status_code == 200
        assert response.json()["slug"] == "stable-slug"
        assert response.json()["display_name"] == "Renamed"

        assert (await client.get(f"{API}/categories/stable-slug")).status_code == 200
        assert (await client.get(f"{API}/categories/renamed-slug")).status_code == 404

    async def test_patch_cannot_change_the_schema(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        # A schema change is a new version by definition.
        await client.post(
            f"{API}/admin/categories", json=make_category("stable-schema"), headers=admin_headers
        )
        await client.patch(
            f"{API}/admin/categories/stable-schema",
            json={"attribute_schema": {"type": "object", "properties": {}}},
            headers=admin_headers,
        )
        fetched = await client.get(f"{API}/categories/stable-schema")
        assert fetched.json()["attribute_schema"]["properties"] == {"name": {"type": "string"}}


# ---------------------------------------------------------------- retirement


class TestRetirement:
    async def test_retired_category_rejects_new_items(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        await client.post(
            f"{API}/admin/categories", json=make_category("retire-cat"), headers=admin_headers
        )
        await client.patch(
            f"{API}/admin/categories/retire-cat",
            json={"is_active": False},
            headers=admin_headers,
        )

        response = await client.post(
            f"{API}/categories/retire-cat/validate", json={"attributes": {"name": "x"}}
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "CATEGORY_RETIRED"

    async def test_retired_category_still_serves_existing_items(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        # Existing items reference this version. A verification that 404s
        # because a category was retired would be a broken provenance record.
        await client.post(
            f"{API}/admin/categories", json=make_category("served-cat"), headers=admin_headers
        )
        await client.patch(
            f"{API}/admin/categories/served-cat",
            json={"is_active": False},
            headers=admin_headers,
        )

        pinned = await client.get(f"{API}/categories/served-cat/v/1")
        assert pinned.status_code == 200
        assert pinned.json()["is_active"] is False

        revalidate = await client.post(
            f"{API}/categories/served-cat/validate",
            json={"attributes": {"name": "x"}, "schema_version": 1},
        )
        assert revalidate.status_code == 200

    async def test_retired_category_is_hidden_from_the_default_listing(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        await client.post(
            f"{API}/admin/categories", json=make_category("hidden-cat"), headers=admin_headers
        )
        await client.patch(
            f"{API}/admin/categories/hidden-cat",
            json={"is_active": False},
            headers=admin_headers,
        )

        default = await client.get(f"{API}/categories")
        assert "hidden-cat" not in {row["slug"] for row in default.json()["data"]}

        included = await client.get(f"{API}/categories", params={"include_inactive": True})
        assert "hidden-cat" in {row["slug"] for row in included.json()["data"]}

    async def test_retirement_applies_to_every_version(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        # Leaving v1 active while v2 is retired would be a way to keep
        # registering items against a category somebody believed was closed.
        await client.post(
            f"{API}/admin/categories", json=make_category("multi-retire"), headers=admin_headers
        )
        await client.post(
            f"{API}/admin/categories/multi-retire/versions",
            json={"attribute_schema": MINIMAL_SCHEMA},
            headers=admin_headers,
        )
        await client.patch(
            f"{API}/admin/categories/multi-retire",
            json={"is_active": False},
            headers=admin_headers,
        )

        versions = await client.get(f"{API}/categories/multi-retire/versions")
        assert all(row["is_active"] is False for row in versions.json()["data"])


# ---------------------------------------------------------------- authorization


class TestAuthorization:
    @pytest.mark.parametrize("role", [UserRole.WEAVER, UserRole.CONSUMER, UserRole.INSPECTOR])
    async def test_non_admin_cannot_create(
        self, client: httpx.AsyncClient, session: AsyncSession, role: UserRole
    ) -> None:
        _, email = await make_user(session, role)
        headers = {"Authorization": f"Bearer {await token_for(client, email)}"}

        response = await client.post(
            f"{API}/admin/categories", json=make_category("weaver-cat"), headers=headers
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"

    async def test_anonymous_cannot_create(self, client: httpx.AsyncClient) -> None:
        response = await client.post(f"{API}/admin/categories", json=make_category("anon-cat"))
        assert response.status_code == 401

    async def test_non_admin_cannot_publish_a_version(
        self, client: httpx.AsyncClient, session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        await client.post(
            f"{API}/admin/categories", json=make_category("guard-cat"), headers=admin_headers
        )
        _, email = await make_user(session, UserRole.WEAVER)
        headers = {"Authorization": f"Bearer {await token_for(client, email)}"}

        response = await client.post(
            f"{API}/admin/categories/guard-cat/versions",
            json={"attribute_schema": MINIMAL_SCHEMA},
            headers=headers,
        )
        assert response.status_code == 403

    async def test_reads_are_public(self, client: httpx.AsyncClient, seeded: None) -> None:
        # A consumer scanning a tag is not logged in.
        assert (await client.get(f"{API}/categories")).status_code == 200
        assert (await client.get(f"{API}/categories/patola-silk")).status_code == 200
        assert (await client.get(f"{API}/categories/patola-silk/versions")).status_code == 200


# ---------------------------------------------------------------- registry


class TestRegistry:
    async def test_new_category_is_visible_without_a_restart(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        assert (await client.get(f"{API}/categories/fresh-cat")).status_code == 404

        await client.post(
            f"{API}/admin/categories", json=make_category("fresh-cat"), headers=admin_headers
        )

        assert (await client.get(f"{API}/categories/fresh-cat")).status_code == 200
        assert (
            await client.post(
                f"{API}/categories/fresh-cat/validate", json={"attributes": {"name": "x"}}
            )
        ).status_code == 200

    async def test_cold_start_loads_lazily(
        self, client: httpx.AsyncClient, seeded: None
    ) -> None:
        # An empty registry on first request must load rather than 500.
        registry.invalidate()
        assert registry.stats()["loaded"] is False

        response = await client.post(
            f"{API}/categories/patola-silk/validate", json={"attributes": PATOLA_OK}
        )
        assert response.status_code == 200
        assert registry.stats()["loaded"] is True

    async def test_a_new_version_replaces_the_cached_validator(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        await client.post(
            f"{API}/admin/categories", json=make_category("cache-cat"), headers=admin_headers
        )
        assert (
            await client.post(
                f"{API}/categories/cache-cat/validate", json={"attributes": {"name": "x"}}
            )
        ).status_code == 200

        await client.post(
            f"{API}/admin/categories/cache-cat/versions",
            json={
                "attribute_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["serial"],
                    "properties": {"serial": {"type": "string"}},
                }
            },
            headers=admin_headers,
        )

        # The stale v1 validator would still accept this.
        stale = await client.post(
            f"{API}/categories/cache-cat/validate", json={"attributes": {"name": "x"}}
        )
        assert stale.status_code == 422


# ---------------------------------------------------------------- idempotency


class TestIdempotency:
    async def test_repeating_a_create_with_the_same_key_replays(
        self, client: httpx.AsyncClient, session: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        key = uuid.uuid4().hex
        headers = {**admin_headers, "Idempotency-Key": key}
        payload = make_category("idem-cat")

        first = await client.post(f"{API}/admin/categories", json=payload, headers=headers)
        second = await client.post(f"{API}/admin/categories", json=payload, headers=headers)

        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["id"] == second.json()["id"]

        count = (
            await session.execute(
                select(func.count()).select_from(GICategory).where(GICategory.slug == "idem-cat")
            )
        ).scalar_one()
        assert count == 1

    async def test_same_key_different_body_is_409(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        key = uuid.uuid4().hex
        headers = {**admin_headers, "Idempotency-Key": key}

        await client.post(
            f"{API}/admin/categories", json=make_category("idem-a"), headers=headers
        )
        response = await client.post(
            f"{API}/admin/categories", json=make_category("idem-b"), headers=headers
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


# ---------------------------------------------------------------- lookups


class TestLookups:
    async def test_unknown_slug_is_404(self, client: httpx.AsyncClient) -> None:
        response = await client.get(f"{API}/categories/no-such-category")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "CATEGORY_NOT_FOUND"

    async def test_validate_against_an_unknown_slug_is_404(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post(
            f"{API}/categories/no-such-category/validate", json={"attributes": {}}
        )
        assert response.status_code == 404

    @pytest.mark.parametrize("slug", ["Not-Lower", "has_underscore", "-leading", "a", "sp ace"])
    async def test_bad_slugs_are_rejected(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str], slug: str
    ) -> None:
        response = await client.post(
            f"{API}/admin/categories", json=make_category(slug), headers=admin_headers
        )
        assert response.status_code == 422

    async def test_listing_returns_only_the_latest_version_per_slug(
        self, client: httpx.AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        await client.post(
            f"{API}/admin/categories", json=make_category("latest-cat"), headers=admin_headers
        )
        await client.post(
            f"{API}/admin/categories/latest-cat/versions",
            json={"attribute_schema": MINIMAL_SCHEMA},
            headers=admin_headers,
        )

        listing = await client.get(f"{API}/categories")
        rows = [row for row in listing.json()["data"] if row["slug"] == "latest-cat"]
        assert len(rows) == 1
        assert rows[0]["schema_version"] == 2
