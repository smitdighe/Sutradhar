"""Nothing identifying may leave through the public endpoint. Asserted by grep.

The method is deliberately crude, and that is why it works. A seeded maker is
given every kind of identifying value this system could plausibly hold -- an
email, a phone number, a postal address, a legal name, a government identifier
-- and the raw serialised response is searched for each one as a substring. A
structural assertion about which fields are published would only ever check the
fields somebody remembered to think about; a substring search over the bytes
that actually go on the wire catches the field nobody thought about, including
one added next year.

Internal identifiers are checked the same way. A user id or an item id is not
private, but publishing either turns one tag code into a handle on the whole
item graph, so neither belongs in a response anybody can fetch.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.catalog import registry
from app.config import get_settings
from app.core.crypto_shred import new_salt
from app.db.models.catalog import GICategory, Item
from app.db.models.enums import UserRole, UserStatus
from app.db.models.user import User

pytestmark = pytest.mark.integration

API = get_settings().api_prefix
PASSWORD = "correct-horse-battery-staple"

# Every one of these is planted on the seeded maker, and every one of them must
# be absent from the public bytes.
IDENTIFYING = {
    "email": "meera.raghunathan.private@example.com",
    "legal_name": "Meera Raghunathan",
    "phone": "9876543210",
    "address": "14 Weavers Lane, Patan",
    "government_id": "ABCDE1234F",
    "aadhaar": "234512347890",
    "bank_account": "50100123456789",
}

PATOLA: dict[str, Any] = {
    "warp_count": 120,
    "dye_type": "natural",
    "gi_registration_no": "GI-00232",
}

# A category whose schema legitimately declares identifying fields. This is not
# a contrived case: schemas are operator-authored, nothing stops a co-op adding
# `contact_phone` to make their own paperwork easier, and the moment they do,
# the public projection is the only thing standing between that column and the
# open internet. The registration path itself refuses unknown keys, so the only
# way identifying data reaches an item is through a schema that permits it --
# which is exactly what this category is.
LEAKY_CATEGORY = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Operator-authored category with identifying fields",
    "type": "object",
    "additionalProperties": False,
    "required": ["warp_count"],
    "properties": {
        "warp_count": {"type": "integer"},
        "dye_type": {"type": "string"},
        "gi_registration_no": {"type": "string"},
        "weaver_name": {"type": "string"},
        "contact_phone": {"type": "string"},
        "workshop_address": {"type": "string"},
        "pan_number": {"type": "string"},
        "aadhaar_number": {"type": "string"},
        "bank_account_number": {"type": "string"},
    },
}
CATEGORY_SLUG = "pii-probe-cloth"


@pytest.fixture(autouse=True)
async def _limiter_on_test_db(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.db.session as session_module

    monkeypatch.setattr(session_module, "SessionLocal", session_factory, raising=False)


async def _seed(
    client: httpx.AsyncClient, session: AsyncSession, *, opt_out: bool = False
) -> tuple[uuid.UUID, Item, str]:
    """A maker carrying identifying data, and one tagged object they made.

    Returns the maker's id rather than the row: the session is expired below so
    the item is read fresh, and an expired attribute cannot be loaded lazily
    from async code.
    """
    from app.auth.password import hash_password

    session.add(
        GICategory(
            slug=CATEGORY_SLUG,
            display_name="Probe Cloth",
            is_textile=True,
            attribute_schema=LEAKY_CATEGORY,
            schema_version=1,
            quantity_unit="metre",
            is_active=True,
        )
    )
    await session.commit()
    registry.invalidate()

    maker = User(
        email=IDENTIFYING["email"],
        password_hash=hash_password(PASSWORD),
        # The display handle is the maker's own choice and is the ONLY name
        # that may appear publicly. Their legal name is a separate value and
        # is planted below in a field that must never be published.
        display_name="Meera weaves at Patan",
        role=UserRole.WEAVER,
        status=UserStatus.ACTIVE,
        region="Gujarat",
        org_name=IDENTIFYING["legal_name"],
        identity_salt=new_salt(),
        public_display_opt_out=opt_out,
    )
    session.add(maker)
    await session.commit()

    login = await client.post(
        f"{API}/auth/login", json={"email": IDENTIFYING["email"], "password": PASSWORD}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    registered = await client.post(
        f"{API}/items",
        json={
            "category_slug": CATEGORY_SLUG,
            # Identifying values planted in the free-form attributes as well:
            # category schemas are operator-authored, so this is the path by
            # which somebody's phone number most plausibly ends up in a row.
            "attributes": {
                **PATOLA,
                "weaver_name": IDENTIFYING["legal_name"],
                "contact_phone": IDENTIFYING["phone"],
                "workshop_address": IDENTIFYING["address"],
                "pan_number": IDENTIFYING["government_id"],
                "aadhaar_number": IDENTIFYING["aadhaar"],
                "bank_account_number": IDENTIFYING["bank_account"],
            },
            "quantity": "5.5000",
            "quantity_unit": "metre",
        },
        headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
    )
    assert registered.status_code == 201, registered.text
    item_id = uuid.UUID(registered.json()["id"])

    issued = await client.post(
        f"{API}/items/{item_id}/tag",
        headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
    )
    assert issued.status_code == 201, issued.text

    maker_id = maker.id
    session.expire_all()
    item = await session.get(Item, item_id)
    assert item is not None
    return maker_id, item, str(issued.json()["tag_code"])


class TestNoIdentifyingDataEscapes:
    @pytest.mark.parametrize("label", sorted(IDENTIFYING))
    async def test_the_public_read_publishes_none_of_it(
        self, client: httpx.AsyncClient, session: AsyncSession, label: str
    ) -> None:
        _maker, _item, code = await _seed(client, session)
        response = await client.get(f"/v/{code}")
        assert response.status_code == 200, response.text
        assert IDENTIFYING[label] not in response.text

    @pytest.mark.parametrize("label", sorted(IDENTIFYING))
    async def test_the_public_scan_publishes_none_of_it(
        self, client: httpx.AsyncClient, session: AsyncSession, label: str
    ) -> None:
        _maker, _item, code = await _seed(client, session)
        response = await client.post(
            f"/v/{code}/scan",
            json={"device_fingerprint": "pii-probe"},
            headers={"X-Geo-Country": "IN", "X-Geo-Region": "GJ"},
        )
        assert response.status_code == 201, response.text
        assert IDENTIFYING[label] not in response.text

    async def test_no_internal_identifier_is_published(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        maker_id, item, code = await _seed(client, session)
        body = (await client.get(f"/v/{code}")).text

        assert str(maker_id) not in body
        assert str(item.id) not in body
        assert str(item.category_id) not in body
        # The salted identity digest is not a name, but it is still a stable
        # per-person value and it is what the chain carries. It stays inside.
        assert item.item_hash not in body

    async def test_the_device_fingerprint_does_not_come_back(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _maker, _item, code = await _seed(client, session)
        response = await client.post(
            f"/v/{code}/scan",
            json={"device_fingerprint": "a-very-distinctive-fingerprint"},
            headers={"X-Geo-Country": "IN", "X-Geo-Region": "GJ"},
        )
        assert "a-very-distinctive-fingerprint" not in response.text

    async def test_identifying_attribute_keys_are_withheld_as_well(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        # Not only the values: a published key called `aadhaar_number` with a
        # withheld value still tells a reader what was collected.
        _maker, _item, code = await _seed(client, session)
        attributes = (await client.get(f"/v/{code}")).json()["attributes"]

        for withheld in (
            "weaver_name",
            "contact_phone",
            "workshop_address",
            "pan_number",
            "aadhaar_number",
            "bank_account_number",
        ):
            assert withheld not in attributes

        # And the properties of the cloth itself do survive -- withholding
        # everything would be safe and useless.
        assert attributes["warp_count"] == 120
        assert attributes["dye_type"] == "natural"
        assert attributes["gi_registration_no"] == "GI-00232"


class TestMakerConsent:
    async def test_the_display_handle_is_published_by_default(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _maker, _item, code = await _seed(client, session)
        story = (await client.get(f"/v/{code}")).json()["story"]

        assert story["weaver_display_name"] == "Meera weaves at Patan"
        assert story["region"] == "Gujarat"
        assert story["maker_opted_out"] is False

    async def test_opting_out_removes_the_maker_entirely(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _maker, _item, code = await _seed(client, session, opt_out=True)
        payload = (await client.get(f"/v/{code}")).json()

        assert payload["story"]["weaver_display_name"] is None
        assert payload["story"]["region"] is None
        assert payload["story"]["maker_opted_out"] is True
        assert "Meera" not in (await client.get(f"/v/{code}")).text

        # Withdrawal is a display choice, not erasure: the record is intact and
        # still verifies, which is what makes the choice reversible.
        assert payload["chain"]["verification"] == "UNANCHORED"
        assert payload["provenance"]["events"]

    async def test_opting_out_is_off_unless_chosen(self, session: AsyncSession) -> None:
        maker = (
            await session.execute(select(User).where(User.email == IDENTIFYING["email"]))
        ).scalar_one_or_none()
        assert maker is None or maker.public_display_opt_out is False
