"""THE LIVE DEMO SCENARIO. This file is the stage moment; keep it runnable.

The problem it answers, in one sentence: **a tag is a printed number.** Someone
who owns one real object can photograph its label and print ten thousand more,
and every one of them carries a correct code with a correct check symbol. The
same thing happens with no printer at all when a label is peeled off one object
and stuck onto another -- the tag never changed, only what is underneath it did.
No signature scheme catches either case, because in both the number is right.

What is left is the *pattern of scans*, and that is what this test exercises:

    1. Register an object, issue its tag through the Phase 10 path.
    2. Scan it in Gujarat.        -> nothing unusual, the scanner claims it
    3. Sixty seconds pass.
    4. Scan the same tag in Assam. -> two places, one minute, ~2000 km

Step 4 is the demo. The response reports SUSPICIOUS, explains why in a sentence
a shopper can read, and tells the second scanner the tag was already claimed --
as a fact with a date, never as an accusation. That last part is not decoration:
a shop display gets scanned by a dozen people a day and none of them are doing
anything wrong.

The item is deliberately left UNANCHORED. No registry is deployed and
``CHAIN_WRITE_ENABLED`` is false, so that is the real state of this system
today, and the scenario has to work in the state it is actually in.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from seeds.loader import load_categories
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.catalog import registry
from app.config import get_settings
from app.core.crypto_shred import new_salt
from app.db.models.enums import SuspicionLevel, UserRole, UserStatus
from app.db.models.scan import Claim, Scan
from app.db.models.user import User

pytestmark = pytest.mark.integration

API = get_settings().api_prefix
PASSWORD = "correct-horse-battery-staple"

GUJARAT = {"X-Geo-Country": "IN", "X-Geo-Region": "GJ"}
ASSAM = {"X-Geo-Country": "IN", "X-Geo-Region": "AS"}

# The same discipline Phase 8 enforces on the trust vocabulary, applied to the
# one message a person reads when something looks wrong. Naming an object as
# illegitimate is a claim this system cannot support, and saying it to the
# customer who bothered to check is the worst possible place to be wrong.
FORBIDDEN_WORDS = (
    "counterfeit",
    "fake",
    "duplicate",
    "stolen",
    "is_fake",
    "is_real",
    "genuine",
    "authentic",
    "guaranteed",
)

PATOLA: dict[str, Any] = {
    "warp_count": 120,
    "weft_count": 116,
    "dye_type": "natural",
    "double_ikat": True,
    "loom_type": "pit",
    "weave_days": 210,
    "gi_registration_no": "GI-00232",
}


@pytest.fixture(autouse=True)
async def _limiter_on_test_db(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the public limiter's own writes inside the test database."""
    import app.db.session as session_module

    monkeypatch.setattr(session_module, "SessionLocal", session_factory, raising=False)


async def _weaver(client: httpx.AsyncClient, session: AsyncSession) -> dict[str, str]:
    from app.auth.password import hash_password

    email = f"demo-weaver-{uuid.uuid4().hex[:8]}@example.com"
    session.add(
        User(
            email=email,
            password_hash=hash_password(PASSWORD),
            display_name="Kanubhai Patel",
            role=UserRole.WEAVER,
            status=UserStatus.ACTIVE,
            region="Patan, Gujarat",
            identity_salt=new_salt(),
        )
    )
    await session.commit()

    login = await client.post(
        f"{API}/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _tagged_item(
    client: httpx.AsyncClient, session: AsyncSession
) -> tuple[uuid.UUID, str]:
    """Register an object and issue its tag through the real Phase 10 endpoint."""
    await load_categories(session)
    await session.commit()
    registry.invalidate()

    headers = await _weaver(client, session)
    registered = await client.post(
        f"{API}/items",
        json={
            "category_slug": "patola-silk",
            "attributes": PATOLA,
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
    return item_id, str(issued.json()["tag_code"])


async def _age_scans(session: AsyncSession, item_id: uuid.UUID, by: timedelta) -> None:
    """Move every recorded scan back in time, so 'sixty seconds later' is exact."""
    await session.execute(
        update(Scan)
        .where(Scan.item_id == item_id)
        .values(created_at=Scan.created_at - by)
    )
    await session.commit()


class TestTheClonedTagDemo:
    async def test_the_same_tag_in_two_places_one_minute_apart(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        item_id, code = await _tagged_item(client, session)

        # ---- before any scan: the record reads clean and unclaimed -------
        opening = await client.get(f"/v/{code}")
        assert opening.status_code == 200, opening.text
        assert opening.json()["scan"]["count"] == 0
        assert opening.json()["claim"]["claimed"] is False
        # Nothing is deployed and writes are off, so this is the true state.
        assert opening.json()["chain"]["verification"] == "UNANCHORED"

        # ---- 1. the buyer scans it in Gujarat ---------------------------
        first = await client.post(
            f"/v/{code}/scan",
            json={"device_fingerprint": "demo-phone-gujarat"},
            headers=GUJARAT,
        )
        assert first.status_code == 201, first.text
        opened = first.json()

        assert opened["scan"]["suspicion_level"] == SuspicionLevel.NONE
        assert opened["scan"]["reason"] is None
        assert opened["claim"]["status"] == "CLAIMED"
        assert opened["claim"]["claimed"] is True
        assert opened["claim"]["is_your_claim"] is True
        assert opened["claim"]["message"] is None

        # ---- 2. sixty seconds pass --------------------------------------
        await _age_scans(session, item_id, timedelta(seconds=60))

        # ---- 3. the same tag is scanned in Assam ------------------------
        second = await client.post(
            f"/v/{code}/scan",
            json={"device_fingerprint": "demo-phone-assam"},
            headers=ASSAM,
        )
        assert second.status_code == 201, second.text
        flagged = second.json()

        # ---- the demo assertions ----------------------------------------
        assert flagged["scan"]["suspicion_level"] == SuspicionLevel.SUSPICIOUS
        assert "IMPOSSIBLE_VELOCITY" in flagged["scan"]["signals"]

        reason = flagged["scan"]["reason"]
        assert reason is not None
        assert "km/h" in reason
        assert "IN-GJ" in reason and "IN-AS" in reason

        assert flagged["claim"]["status"] == "ALREADY_CLAIMED"
        assert flagged["claim"]["is_your_claim"] is False
        assert flagged["claim"]["claimed_at"] == opened["claim"]["claimed_at"]

        message = flagged["claim"]["message"]
        assert message is not None
        assert "already claimed" in message.lower()
        assert "seller" in message.lower()

    async def test_neither_message_accuses_anybody(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """The wording check, asserted against the whole serialised response.

        Not just the claim message: a forbidden word anywhere in the payload --
        an anomaly reason, a trust level, a field name -- would reach the same
        reader with the same force.
        """
        _item_id, code = await _tagged_item(client, session)

        await client.post(
            f"/v/{code}/scan",
            json={"device_fingerprint": "demo-phone-gujarat"},
            headers=GUJARAT,
        )
        second = await client.post(
            f"/v/{code}/scan",
            json={"device_fingerprint": "demo-phone-assam"},
            headers=ASSAM,
        )

        blob = second.text.lower()
        offenders = [word for word in FORBIDDEN_WORDS if word in blob]
        assert offenders == [], f"the public response says {offenders}"

    async def test_the_first_claim_survives_the_second_scan(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        item_id, code = await _tagged_item(client, session)

        await client.post(
            f"/v/{code}/scan",
            json={"device_fingerprint": "demo-phone-gujarat"},
            headers=GUJARAT,
        )
        session.expire_all()
        original = await session.get(Claim, item_id)
        assert original is not None
        first_owner, first_time = original.device_fingerprint, original.claimed_at

        await _age_scans(session, item_id, timedelta(seconds=60))
        await client.post(
            f"/v/{code}/scan",
            json={"device_fingerprint": "demo-phone-assam"},
            headers=ASSAM,
        )

        session.expire_all()
        after = await session.get(Claim, item_id)
        assert after is not None
        # Never overwritten. A record the most recent scanner can rewrite is not
        # a record of anything.
        assert (after.device_fingerprint, after.claimed_at) == (first_owner, first_time)

        rows = (
            await session.execute(select(Claim).where(Claim.item_id == item_id))
        ).scalars().all()
        assert len(rows) == 1

    async def test_the_raw_fingerprint_is_never_stored(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        item_id, code = await _tagged_item(client, session)
        await client.post(
            f"/v/{code}/scan",
            json={"device_fingerprint": "demo-phone-gujarat"},
            headers=GUJARAT,
        )

        session.expire_all()
        scans = (
            await session.execute(select(Scan).where(Scan.item_id == item_id))
        ).scalars().all()
        assert scans
        for row in scans:
            assert row.device_fingerprint != "demo-phone-gujarat"
            assert row.device_fingerprint is not None
            # A sha256 hex digest, not the string the client sent.
            assert len(row.device_fingerprint) == 64
            # And no address anywhere near it.
            assert row.ip_hash is None or len(row.ip_hash) == 64
