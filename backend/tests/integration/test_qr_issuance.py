"""Tag issuance, collisions, idempotency, bulk runs, and the served QR images.

The collision test is the point of this file. A duplicate tag code means two
textiles claim one provenance and there is no way to tell them apart
afterwards, so the retry is exercised against a real unique index rather than
against a mock that agrees with the code.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import zxingcpp
from PIL import Image
from seeds.loader import load_categories
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog import registry
from app.config import get_settings
from app.core.crypto_shred import new_salt
from app.core.ids import new_tag_code, validate_tag_code
from app.db.models.catalog import Item, ItemEvent
from app.db.models.enums import ItemEventType, ItemStatus, UserRole, UserStatus
from app.db.models.user import User
from app.qr import service

pytestmark = pytest.mark.integration

API = get_settings().api_prefix
ITEMS = f"{API}/items"
BULK = f"{API}/admin/tags/bulk"
PASSWORD = "correct-horse-battery-staple"

PATOLA: dict[str, Any] = {
    "warp_count": 120,
    "weft_count": 116,
    "dye_type": "natural",
    "double_ikat": True,
    "loom_type": "pit",
    "weave_days": 210,
    "gi_registration_no": "GI-00232",
}


def key() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex}


async def make_user(session: AsyncSession, role: UserRole) -> tuple[User, dict[str, str]]:
    from app.auth.password import hash_password

    email = f"qr-{role.lower()}-{uuid.uuid4().hex[:8]}@example.com"
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
    return user, {"email": email}


async def headers_for(client: httpx.AsyncClient, email: str) -> dict[str, str]:
    response = await client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def actor(
    client: httpx.AsyncClient, session: AsyncSession, role: UserRole
) -> tuple[User, dict[str, str]]:
    user, login = await make_user(session, role)
    return user, await headers_for(client, login["email"])


@pytest.fixture
async def seeded(session: AsyncSession) -> None:
    await load_categories(session)
    await session.commit()
    registry.invalidate()


@pytest.fixture
async def weaver(
    client: httpx.AsyncClient, session: AsyncSession, seeded: None
) -> tuple[User, dict[str, str]]:
    return await actor(client, session, UserRole.WEAVER)


@pytest.fixture
async def officer(
    client: httpx.AsyncClient, session: AsyncSession, seeded: None
) -> tuple[User, dict[str, str]]:
    return await actor(client, session, UserRole.COOP_OFFICER)


def body(quantity: str = "12.0000", **overrides: Any) -> dict[str, Any]:
    return {
        "category_slug": "patola-silk",
        "attributes": PATOLA,
        "quantity": quantity,
        "quantity_unit": "metre",
        **overrides,
    }


async def register(
    client: httpx.AsyncClient, headers: dict[str, str], quantity: str = "12.0000"
) -> uuid.UUID:
    response = await client.post(
        ITEMS, json=body(quantity), headers={**headers, **key()}
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def issue(
    client: httpx.AsyncClient, headers: dict[str, str], item_id: uuid.UUID, **extra: str
) -> httpx.Response:
    return await client.post(
        f"{ITEMS}/{item_id}/tag", headers={**headers, **key(), **extra}
    )


async def reload_item(session: AsyncSession, item_id: uuid.UUID) -> Item:
    session.expire_all()
    item = await session.get(Item, item_id)
    assert item is not None
    return item


async def tag_events(session: AsyncSession, item_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(ItemEvent)
                .where(
                    ItemEvent.item_id == item_id,
                    ItemEvent.event_type == ItemEventType.TAG_ISSUED,
                )
            )
        ).scalar_one()
    )


# ---------------------------------------------------------------- issuance


class TestIssuance:
    async def test_issues_a_valid_checksummed_code(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        _user, headers = weaver
        item_id = await register(client, headers)

        response = await issue(client, headers, item_id)
        assert response.status_code == 201, response.text
        payload = response.json()

        assert validate_tag_code(payload["tag_code"])
        assert payload["display_code"] == service.format_tag_code(payload["tag_code"])
        assert payload["payload_url"] == service.tag_url(payload["tag_code"])
        assert payload["warnings"] == []

        item = await reload_item(session, item_id)
        assert item.tag_code == payload["tag_code"]

    async def test_writes_a_tag_issued_event(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        _user, headers = weaver
        item_id = await register(client, headers)
        await issue(client, headers, item_id)
        assert await tag_events(session, item_id) == 1

    async def test_stored_code_carries_no_separators(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        _user, headers = weaver
        item_id = await register(client, headers)
        code = (await issue(client, headers, item_id)).json()["tag_code"]
        # Grouping is for the label, never for the column.
        assert "-" not in code and " " not in code and code.isupper() or code.isdigit()

    async def test_missing_idempotency_key_is_refused(
        self, client: httpx.AsyncClient, weaver: Any
    ) -> None:
        _user, headers = weaver
        item_id = await register(client, headers)
        response = await client.post(f"{ITEMS}/{item_id}/tag", headers=headers)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_FAILED"

    async def test_double_issuance_conflicts_and_returns_the_existing_code(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        _user, headers = weaver
        item_id = await register(client, headers)
        first = (await issue(client, headers, item_id)).json()

        second = await issue(client, headers, item_id)
        assert second.status_code == 409
        error = second.json()["error"]
        assert error["code"] == "TAG_ALREADY_ISSUED"
        # A fact to read, not a failure to retry: the caller is handed the code
        # it should be printing.
        assert error["details"]["tag_code"] == first["tag_code"]
        assert error["details"]["display_code"] == first["display_code"]
        assert await tag_events(session, item_id) == 1

    async def test_same_idempotency_key_replays_one_tag(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        _user, headers = weaver
        item_id = await register(client, headers)
        shared = key()

        first = await client.post(f"{ITEMS}/{item_id}/tag", headers={**headers, **shared})
        second = await client.post(f"{ITEMS}/{item_id}/tag", headers={**headers, **shared})

        assert first.status_code == second.status_code == 201
        assert first.json() == second.json()
        assert await tag_events(session, item_id) == 1


class TestCollision:
    @pytest.fixture
    def collide_once(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
        """Make the generator hand back one already-taken code, then behave."""
        taken: list[str] = []

        def generator() -> str:
            if taken and len(taken) == 1:
                # Second caller gets the first caller's code, exactly once.
                taken.append("collided")
                return taken[0]
            code = new_tag_code()
            if not taken:
                taken.append(code)
            return code

        monkeypatch.setattr(service, "new_tag_code", generator)
        yield taken

    async def test_a_taken_code_is_retried_and_the_item_still_gets_one_tag(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        weaver: Any,
        collide_once: list[str],
    ) -> None:
        _user, headers = weaver
        first_item = await register(client, headers)
        second_item = await register(client, headers)

        first = await issue(client, headers, first_item)
        assert first.status_code == 201
        colliding_code = first.json()["tag_code"]

        second = await issue(client, headers, second_item)
        assert second.status_code == 201, second.text
        assert second.json()["tag_code"] != colliding_code
        assert collide_once[1] == "collided", "the collision path was never taken"

        # Exactly one item wears the contested code, and the retry did not
        # leave the second item untagged or doubly evented.
        holders = (
            await session.execute(select(Item).where(Item.tag_code == colliding_code))
        ).scalars().all()
        assert len(holders) == 1
        assert holders[0].id == first_item
        assert await tag_events(session, second_item) == 1

    async def test_an_exhausted_generator_is_a_server_fault(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        weaver: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _user, headers = weaver
        first_item = await register(client, headers)
        second_item = await register(client, headers)
        taken = (await issue(client, headers, first_item)).json()["tag_code"]

        # A generator that only ever returns a taken code is a broken generator,
        # and the response says so rather than blaming the caller.
        monkeypatch.setattr(service, "new_tag_code", lambda: taken)
        response = await issue(client, headers, second_item)
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "TAG_GENERATION_EXHAUSTED"
        assert (await reload_item(session, second_item)).tag_code is None


class TestRefusals:
    async def test_a_weaver_cannot_tag_somebody_elses_item(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        _owner, owner_headers = weaver
        item_id = await register(client, owner_headers)

        _other, other_headers = await actor(client, session, UserRole.WEAVER)
        response = await issue(client, other_headers, item_id)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"
        assert (await reload_item(session, item_id)).tag_code is None

    async def test_a_coop_officer_may_tag_anybody_s_item(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any, officer: Any
    ) -> None:
        _owner, owner_headers = weaver
        item_id = await register(client, owner_headers)
        _officer, officer_headers = officer

        assert (await issue(client, officer_headers, item_id)).status_code == 201

    async def test_a_consumer_may_not_tag_at_all(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        _owner, owner_headers = weaver
        item_id = await register(client, owner_headers)

        _consumer, consumer_headers = await actor(client, session, UserRole.CONSUMER)
        response = await issue(client, consumer_headers, item_id)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"

    async def test_a_failed_item_is_not_tagged(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        _user, headers = weaver
        item_id = await register(client, headers)

        item = await reload_item(session, item_id)
        item.status = ItemStatus.FAILED
        await session.commit()

        response = await issue(client, headers, item_id)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "TAG_NOT_ISSUABLE"
        assert (await reload_item(session, item_id)).tag_code is None

    async def test_an_unknown_item_is_not_found(
        self, client: httpx.AsyncClient, weaver: Any
    ) -> None:
        _user, headers = weaver
        response = await issue(client, headers, uuid.uuid4())
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "ITEM_NOT_FOUND"


class TestSplitWarning:
    async def test_tagging_a_parent_warns_without_blocking(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        _user, headers = weaver
        parent_id = await register(client, headers)

        split = await client.post(
            f"{ITEMS}/{parent_id}/split",
            json={
                "children": [
                    {"quantity": "5.5000", "attributes": PATOLA},
                    {"quantity": "5.5000", "attributes": PATOLA},
                ]
            },
            headers={**headers, **key()},
        )
        assert split.status_code == 200, split.text

        response = await issue(client, headers, parent_id)
        # A bolt sold whole is legitimate, so this is not refused -- but one tag
        # over several objects is the substitution path, so it is said out loud.
        assert response.status_code == 201
        warnings = response.json()["warnings"]
        assert len(warnings) == 1
        assert "smallest sellable unit" in warnings[0]

    async def test_tagging_a_child_warns_about_nothing(
        self, client: httpx.AsyncClient, weaver: Any
    ) -> None:
        _user, headers = weaver
        parent_id = await register(client, headers)
        split = await client.post(
            f"{ITEMS}/{parent_id}/split",
            json={"children": [{"quantity": "5.5000", "attributes": PATOLA}]},
            headers={**headers, **key()},
        )
        child_id = split.json()["children"][0]["id"]

        response = await issue(client, headers, uuid.UUID(child_id))
        assert response.status_code == 201
        assert response.json()["warnings"] == []


# ---------------------------------------------------------------- images


class TestQrImages:
    @pytest.fixture
    async def tagged(
        self, client: httpx.AsyncClient, weaver: Any
    ) -> tuple[uuid.UUID, str, dict[str, str]]:
        _user, headers = weaver
        item_id = await register(client, headers)
        code = (await issue(client, headers, item_id)).json()["tag_code"]
        return item_id, code, headers

    async def test_png_decodes_to_exactly_the_payload_url(
        self, client: httpx.AsyncClient, tagged: tuple[uuid.UUID, str, dict[str, str]]
    ) -> None:
        item_id, code, headers = tagged
        response = await client.get(f"{ITEMS}/{item_id}/tag/qr?format=png", headers=headers)

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        decoded = zxingcpp.read_barcode(Image.open(io.BytesIO(response.content)).convert("RGB"))
        assert decoded is not None
        assert decoded.text == service.tag_url(code)

    async def test_png_defaults_to_512_and_honours_size(
        self, client: httpx.AsyncClient, tagged: tuple[uuid.UUID, str, dict[str, str]]
    ) -> None:
        item_id, _code, headers = tagged
        default = await client.get(f"{ITEMS}/{item_id}/tag/qr", headers=headers)
        assert Image.open(io.BytesIO(default.content)).size == (512, 512)

        larger = await client.get(f"{ITEMS}/{item_id}/tag/qr?size=1024", headers=headers)
        assert Image.open(io.BytesIO(larger.content)).size == (1024, 1024)

    async def test_svg_is_served_scalable(
        self, client: httpx.AsyncClient, tagged: tuple[uuid.UUID, str, dict[str, str]]
    ) -> None:
        item_id, code, headers = tagged
        response = await client.get(f"{ITEMS}/{item_id}/tag/qr?format=svg", headers=headers)

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/svg+xml"
        assert "viewBox=" in response.text
        assert "@" not in response.text
        assert code not in response.text.replace(service.format_tag_code(code), "")

    async def test_the_image_is_cacheable_forever(
        self, client: httpx.AsyncClient, tagged: tuple[uuid.UUID, str, dict[str, str]]
    ) -> None:
        item_id, _code, headers = tagged
        response = await client.get(f"{ITEMS}/{item_id}/tag/qr", headers=headers)
        # The payload never changes for a tag, so revalidation is pure waste.
        assert "immutable" in response.headers["cache-control"]
        assert "public" in response.headers["cache-control"]

    async def test_an_untagged_item_has_no_qr(
        self, client: httpx.AsyncClient, weaver: Any
    ) -> None:
        _user, headers = weaver
        item_id = await register(client, headers)
        response = await client.get(f"{ITEMS}/{item_id}/tag/qr", headers=headers)
        assert response.status_code == 404

    async def test_an_unknown_format_is_refused(
        self, client: httpx.AsyncClient, tagged: tuple[uuid.UUID, str, dict[str, str]]
    ) -> None:
        item_id, _code, headers = tagged
        response = await client.get(f"{ITEMS}/{item_id}/tag/qr?format=pdf", headers=headers)
        assert response.status_code == 422


# ---------------------------------------------------------------- lookup


class TestNormalisation:
    async def test_every_typed_form_resolves_to_the_same_item(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any
    ) -> None:
        _user, headers = weaver
        item_id = await register(client, headers)
        code = (await issue(client, headers, item_id)).json()["tag_code"]
        grouped = service.format_tag_code(code)

        forms = [
            code,
            code.lower(),
            grouped,
            grouped.lower(),
            grouped.replace("-", " "),
            f" {grouped.lower()} ",
        ]
        for typed in forms:
            resolved = await service.lookup_by_tag_code(session, typed)
            assert resolved.id == item_id, typed

    async def test_a_mistyped_code_is_told_it_is_mistyped(
        self, session: AsyncSession
    ) -> None:
        from app.core.errors import ValidationError

        with pytest.raises(ValidationError) as caught:
            await service.lookup_by_tag_code(session, "NOT-A-REAL-CODE")
        assert caught.value.code == "INVALID_TAG_CODE"

    async def test_a_valid_code_nobody_holds_is_not_found(
        self, session: AsyncSession
    ) -> None:
        from app.core.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await service.lookup_by_tag_code(session, new_tag_code())


# ---------------------------------------------------------------- bulk


class TestBulkIssuance:
    async def test_a_mixed_batch_issues_only_the_eligible_items(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any, officer: Any
    ) -> None:
        _owner, owner_headers = weaver
        _officer, officer_headers = officer

        fresh_a = await register(client, owner_headers)
        fresh_b = await register(client, owner_headers)
        already = await register(client, owner_headers)
        broken = await register(client, owner_headers)
        missing = uuid.uuid4()

        existing_code = (await issue(client, owner_headers, already)).json()["tag_code"]
        item = await reload_item(session, broken)
        item.status = ItemStatus.FAILED
        await session.commit()

        response = await client.post(
            BULK,
            json={"item_ids": [str(i) for i in (fresh_a, already, broken, fresh_b, missing)]},
            headers=officer_headers,
        )
        assert response.status_code == 200, response.text
        payload = response.json()

        assert payload["requested"] == 5
        assert payload["issued"] == 2
        assert payload["already_tagged"] == 1
        assert payload["failed"] == 2

        by_id = {result["item_id"]: result for result in payload["results"]}
        assert by_id[str(fresh_a)]["outcome"] == "issued"
        assert validate_tag_code(by_id[str(fresh_a)]["tag_code"])
        assert by_id[str(already)]["outcome"] == "already_tagged"
        assert by_id[str(already)]["tag_code"] == existing_code
        assert by_id[str(broken)]["reason_code"] == "TAG_NOT_ISSUABLE"
        assert by_id[str(missing)]["reason_code"] == "ITEM_NOT_FOUND"

        # Partial success is a success: the good rows really committed.
        assert (await reload_item(session, fresh_a)).tag_code is not None
        assert (await reload_item(session, fresh_b)).tag_code is not None
        assert (await reload_item(session, broken)).tag_code is None

    async def test_repeated_ids_are_collapsed(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any, officer: Any
    ) -> None:
        _owner, owner_headers = weaver
        _officer, officer_headers = officer
        item_id = await register(client, owner_headers)

        response = await client.post(
            BULK,
            json={"item_ids": [str(item_id), str(item_id)]},
            headers=officer_headers,
        )
        assert response.status_code == 200
        # The same id twice is one item, not one issue and one spurious
        # "already tagged".
        assert len(response.json()["results"]) == 1
        assert await tag_events(session, item_id) == 1

    async def test_a_batch_over_the_ceiling_issues_nothing(
        self, client: httpx.AsyncClient, session: AsyncSession, weaver: Any, officer: Any
    ) -> None:
        _owner, owner_headers = weaver
        _officer, officer_headers = officer
        real = await register(client, owner_headers)

        oversized = [str(real)] + [str(uuid.uuid4()) for _ in range(service.BULK_MAX_ITEMS)]
        response = await client.post(BULK, json={"item_ids": oversized}, headers=officer_headers)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "BULK_TOO_LARGE"
        assert (await reload_item(session, real)).tag_code is None

    async def test_a_weaver_may_not_bulk_issue(
        self, client: httpx.AsyncClient, weaver: Any
    ) -> None:
        _user, headers = weaver
        response = await client.post(BULK, json={"item_ids": []}, headers=headers)
        assert response.status_code == 403
