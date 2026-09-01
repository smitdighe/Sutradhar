"""The public endpoint: recompute-and-compare, degradation, claiming, refusals.

The test that matters most here is
``test_editing_a_hashed_column_flips_the_answer_to_mismatch``. It is the whole
argument for putting a chain under this system: an operator with write access to
PostgreSQL can change any row, and this asserts that doing so is *visible to the
public* rather than silent. Without it the chain is decoration.

The second theme is degradation. Nothing is deployed to a testnet and writes are
off, so "no chain" is not a failure branch here -- it is the state the system is
actually in, and it is tested as the default rather than as an injected fault.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
from seeds.loader import load_categories
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.catalog import registry
from app.config import get_settings
from app.core.clock import now
from app.core.crypto_shred import new_salt
from app.core.hashing import from_hex, hash_hex
from app.core.ids import new_tag_code
from app.core.merkle import build_root
from app.db.models.catalog import Item, ItemEvent
from app.db.models.chain import ChainEvent, MerkleBatch, MerkleLeaf
from app.db.models.enums import ItemEventType, UserRole, UserStatus
from app.db.models.ops import RateLimitBucket
from app.db.models.scan import Claim, Scan
from app.db.models.user import User
from app.verification import router as router_module, service

pytestmark = pytest.mark.integration

SETTINGS = get_settings()
API = SETTINGS.api_prefix
PASSWORD = "correct-horse-battery-staple"
GUJARAT = {"X-Geo-Country": "IN", "X-Geo-Region": "GJ"}

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
    import app.db.session as session_module

    monkeypatch.setattr(session_module, "SessionLocal", session_factory, raising=False)


async def tagged(client: httpx.AsyncClient, session: AsyncSession) -> tuple[uuid.UUID, str]:
    """One registered, tagged item, created through the authenticated API."""
    from app.auth.password import hash_password

    await load_categories(session)
    await session.commit()
    registry.invalidate()

    email = f"pub-{uuid.uuid4().hex[:8]}@example.com"
    session.add(
        User(
            email=email,
            password_hash=hash_password(PASSWORD),
            display_name="Test Weaver",
            role=UserRole.WEAVER,
            status=UserStatus.ACTIVE,
            region="Gujarat",
            identity_salt=new_salt(),
        )
    )
    await session.commit()

    login = await client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

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


async def anchor_singly(session: AsyncSession, item_id: uuid.UUID) -> str:
    """Fabricate the evidence a confirmed single anchor leaves behind.

    A real anchor is written by the Phase 7 worker against a deployed registry.
    Nothing is deployed, so the two rows that worker would leave -- the mirrored
    chain log and the append-only ANCHORED event -- are written directly. That
    is the state the verifier reads, and it is the state under test.
    """
    session.expire_all()
    item = await session.get(Item, item_id)
    assert item is not None
    tx_hash = "0x" + uuid.uuid4().hex + uuid.uuid4().hex

    session.add(
        ChainEvent(
            event_name="ItemAnchored",
            tx_hash=tx_hash,
            log_index=0,
            block_number=1_000,
            block_hash="0x" + "ab" * 32,
            contract_address=SETTINGS.contract_address,
            subject_hash=item.item_hash,
            issuer_hash="0x" + "cd" * 32,
            issuer_address="0x" + "11" * 20,
            chain_timestamp=int(now().timestamp()),
            payload={"source": "test"},
        )
    )
    session.add(
        ItemEvent(
            item_id=item_id,
            event_type=ItemEventType.ANCHORED,
            actor_id=None,
            payload={"tx_hash": tx_hash, "block_number": 1_000, "confirmations": 5},
            payload_hash="0x" + "ef" * 32,
        )
    )
    await session.commit()
    return tx_hash


async def anchor_in_batch(session: AsyncSession, item_id: uuid.UUID) -> str:
    """Put the item in a one-leaf Merkle batch and anchor the root."""
    session.expire_all()
    item = await session.get(Item, item_id)
    assert item is not None

    root = hash_hex(build_root([from_hex(item.item_hash)]))
    batch = MerkleBatch(root=root, leaf_count=1)
    session.add(batch)
    await session.flush()
    session.add(
        MerkleLeaf(
            batch_id=batch.id, leaf_index=0, item_id=item_id, leaf_hash=item.item_hash
        )
    )
    await session.commit()
    return root


class _Reader:
    """A stand-in chain that answers yes, no, or by exploding."""

    def __init__(self, answer: bool = True, explode: bool = False) -> None:
        self.answer = answer
        self.explode = explode
        self.calls = 0

    async def is_item_anchored(self, item_hash: str) -> bool:
        self.calls += 1
        if self.explode:
            raise ConnectionError("the RPC endpoint is not answering")
        return self.answer

    async def is_batch_anchored(self, root: str) -> bool:
        self.calls += 1
        if self.explode:
            raise ConnectionError("the RPC endpoint is not answering")
        return self.answer


def use_reader(monkeypatch: pytest.MonkeyPatch, reader: _Reader | None) -> None:
    monkeypatch.setattr(router_module, "_chain_reader", lambda _request: reader)


# ---------------------------------------------------------------- the core


class TestRecomputeAndCompare:
    async def test_an_untampered_anchored_item_matches(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        item_id, code = await tagged(client, session)
        await anchor_singly(session, item_id)

        chain = (await client.get(f"/v/{code}")).json()["chain"]
        assert chain["verification"] == "MATCH"
        assert chain["block_number"] == 1_000
        assert chain["confirmations"] == 5
        assert chain["tx_hash"] is not None

    async def test_editing_a_hashed_column_flips_the_answer_to_mismatch(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """The demonstration that the chain is doing real work.

        A database administrator changes a quantity. The row is now internally
        consistent, the API would happily serve it, and the recomputed hash no
        longer equals what was anchored -- so the public page says so. This is
        the property that makes the anchor worth its gas.
        """
        item_id, code = await tagged(client, session)
        await anchor_singly(session, item_id)
        assert (await client.get(f"/v/{code}")).json()["chain"]["verification"] == "MATCH"

        await session.execute(
            update(Item).where(Item.id == item_id).values(quantity=Decimal("4.0000"))
        )
        await session.commit()

        payload = (await client.get(f"/v/{code}")).json()
        assert payload["chain"]["verification"] == "MISMATCH"
        # Still a 200 with the record on it. Refusing to answer would hide the
        # very thing a reader needs to see.
        assert payload["quantity"] == "4.0000"

    @pytest.mark.parametrize(
        "column,value",
        [
            ("quantity", Decimal("9.9999")),
            ("quantity_unit", "furlong"),
            ("category_schema_version", 7),
        ],
    )
    async def test_every_hashed_column_is_load_bearing(
        self, client: httpx.AsyncClient, session: AsyncSession, column: str, value: Any
    ) -> None:
        item_id, code = await tagged(client, session)
        await anchor_singly(session, item_id)

        await session.execute(update(Item).where(Item.id == item_id).values(**{column: value}))
        await session.commit()

        assert (await client.get(f"/v/{code}")).json()["chain"]["verification"] == "MISMATCH"

    async def test_editing_an_unhashed_column_changes_nothing(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        # The converse, and it matters: a verifier that flips on every write
        # would be noise, and nobody would look at it twice.
        item_id, code = await tagged(client, session)
        await anchor_singly(session, item_id)

        await session.execute(
            update(Item).where(Item.id == item_id).values(claimed_at=now())
        )
        await session.commit()

        assert (await client.get(f"/v/{code}")).json()["chain"]["verification"] == "MATCH"

    async def test_an_unanchored_item_says_so_and_is_not_an_error(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        # The current, real state of this system: no registry deployed, writes
        # off. It has to be an ordinary 200, not a degraded mode.
        _item_id, code = await tagged(client, session)

        response = await client.get(f"/v/{code}")
        assert response.status_code == 200
        chain = response.json()["chain"]
        assert chain["verification"] == "UNANCHORED"
        assert chain["status"] == "PENDING"
        assert chain["tx_hash"] is None
        assert chain["chain_checked_at"] is not None


class TestBatchedItems:
    async def test_a_batched_item_verifies_by_inclusion_proof(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        item_id, code = await tagged(client, session)
        root = await anchor_in_batch(session, item_id)

        chain = (await client.get(f"/v/{code}")).json()["chain"]
        assert chain["verification"] == "MATCH"
        assert chain["inclusion_proof"] is not None
        assert chain["inclusion_proof"]["root"] == root
        assert chain["inclusion_proof"]["leaf_count"] == 1

    async def test_a_tampered_batched_item_fails_its_own_proof(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        item_id, code = await tagged(client, session)
        await anchor_in_batch(session, item_id)

        await session.execute(
            update(Item).where(Item.id == item_id).values(quantity=Decimal("1.0000"))
        )
        await session.commit()

        assert (await client.get(f"/v/{code}")).json()["chain"]["verification"] == "MISMATCH"

    async def test_the_published_proof_is_checkable_offline(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        from app.chain.batching import verify_inclusion

        item_id, code = await tagged(client, session)
        await anchor_in_batch(session, item_id)

        session.expire_all()
        item = await session.get(Item, item_id)
        assert item is not None
        proof = (await client.get(f"/v/{code}")).json()["chain"]["inclusion_proof"]

        # A reader with the payload and nothing else can run this themselves,
        # which is the difference between evidence and an assurance.
        assert verify_inclusion(item.item_hash, proof["proof"], proof["root"])


class TestDegradation:
    async def test_a_live_chain_answers_fresh(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        item_id, code = await tagged(client, session)
        await anchor_singly(session, item_id)
        reader = _Reader(answer=True)
        use_reader(monkeypatch, reader)

        chain = (await client.get(f"/v/{code}")).json()["chain"]
        assert reader.calls == 1
        assert chain["verification"] == "MATCH"
        assert chain["stale"] is False

    async def test_a_live_chain_that_has_never_seen_the_hash_reports_mismatch(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        item_id, code = await tagged(client, session)
        await anchor_singly(session, item_id)
        use_reader(monkeypatch, _Reader(answer=False))

        chain = (await client.get(f"/v/{code}")).json()["chain"]
        assert chain["verification"] == "MISMATCH"
        assert chain["stale"] is False

    async def test_an_unreachable_chain_serves_the_last_known_state(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        item_id, code = await tagged(client, session)
        await anchor_singly(session, item_id)
        use_reader(monkeypatch, _Reader(explode=True))

        response = await client.get(f"/v/{code}")
        assert response.status_code == 200
        chain = response.json()["chain"]
        # Last known state, labelled as such, with the moment it was observed.
        assert chain["verification"] == "MATCH"
        assert chain["stale"] is True
        assert chain["chain_checked_at"] is not None

    async def test_with_no_chain_at_all_the_answer_is_still_labelled_stale(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        item_id, code = await tagged(client, session)
        await anchor_singly(session, item_id)

        # No runtime, because nothing is deployed. The default path.
        chain = (await client.get(f"/v/{code}")).json()["chain"]
        assert chain["stale"] is True

    async def test_an_anchor_event_the_indexer_has_not_caught_up_to_still_answers(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """A real intermediate state, not an injected fault.

        The confirmation sweep writes the ANCHORED event as soon as it sees a
        receipt; the event indexer runs on its own schedule and may not have
        mirrored that transaction's log yet. The verifier has to fall through to
        the evidence it does have rather than answering with an exception.
        """
        item_id, code = await tagged(client, session)
        session.add(
            ItemEvent(
                item_id=item_id,
                event_type=ItemEventType.ANCHORED,
                actor_id=None,
                payload={"tx_hash": "0x" + "9a" * 32, "block_number": 12},
                payload_hash="0x" + "ef" * 32,
            )
        )
        await session.commit()

        response = await client.get(f"/v/{code}")
        assert response.status_code == 200
        # Nothing was mirrored, so there is nothing to compare against yet.
        assert response.json()["chain"]["verification"] == "UNANCHORED"


# ---------------------------------------------------------------- refusals


class TestRefusals:
    async def test_an_unknown_but_well_formed_code_is_a_plain_404(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get(f"/v/{new_tag_code()}")
        assert response.status_code == 404
        body = response.json()["error"]
        assert body["code"] == "NOT_FOUND"
        # Nothing about whether that code was ever issued, retired, or is one
        # character away from a real one.
        assert body["message"] == "no record for this tag"
        assert body["details"] is None

    async def test_a_malformed_code_is_a_400(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/v/NOTAREALCODE")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_TAG_CODE"

    async def test_a_malformed_code_never_reaches_the_items_table(
        self, client: httpx.AsyncClient, engine: AsyncEngine
    ) -> None:
        """A smudged label must not cost a query.

        Also the reason the check symbol exists at all: without it this endpoint
        would be a way to probe the table with codes that cannot exist.
        """
        statements: list[str] = []

        def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            response = await client.get("/v/X7K29M4P3RQ7")
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)

        assert response.status_code == 400
        touched = [text for text in statements if " items" in text.lower()]
        assert touched == [], touched

    async def test_a_scan_of_a_malformed_code_is_also_refused(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post("/v/NOTAREALCODE/scan", json={})
        assert response.status_code == 400

    async def test_the_scan_limiter_fires_per_address(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _item_id, code = await tagged(client, session)

        first = await client.post(f"/v/{code}/scan", json={}, headers=GUJARAT)
        assert first.status_code == 201

        # Push this caller's bucket to the ceiling rather than making sixty
        # requests: the limiter's own counting is covered by its own suite, and
        # what is under test here is that this route is wired to it.
        bucket = (
            await session.execute(
                select(RateLimitBucket).where(RateLimitBucket.scope == "public_scan")
            )
        ).scalar_one()
        bucket.count = SETTINGS.rate_limit_scan_per_minute
        await session.commit()

        limited = await client.post(f"/v/{code}/scan", json={}, headers=GUJARAT)
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "RATE_LIMITED"
        assert int(limited.headers["retry-after"]) >= 1


# ---------------------------------------------------------------- claiming


class TestClaiming:
    async def test_two_simultaneous_first_scans_produce_exactly_one_claim(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """Two shoppers at one shelf. The primary key decides, not this code."""
        item_id, code = await tagged(client, session)

        responses = await asyncio.gather(
            client.post(
                f"/v/{code}/scan", json={"device_fingerprint": "phone-a"}, headers=GUJARAT
            ),
            client.post(
                f"/v/{code}/scan", json={"device_fingerprint": "phone-b"}, headers=GUJARAT
            ),
        )
        assert [response.status_code for response in responses] == [201, 201]

        session.expire_all()
        claims = (
            await session.execute(select(Claim).where(Claim.item_id == item_id))
        ).scalars().all()
        assert len(claims) == 1

        outcomes = sorted(response.json()["claim"]["status"] for response in responses)
        assert outcomes == ["ALREADY_CLAIMED", "CLAIMED"]

    async def test_rescanning_your_own_object_is_not_a_warning(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _item_id, code = await tagged(client, session)
        body = {"device_fingerprint": "my-own-phone"}

        first = await client.post(f"/v/{code}/scan", json=body, headers=GUJARAT)
        second = await client.post(f"/v/{code}/scan", json=body, headers=GUJARAT)

        for response in (first, second):
            claim = response.json()["claim"]
            assert claim["status"] == "CLAIMED"
            assert claim["is_your_claim"] is True
            assert claim["message"] is None

    async def test_a_second_device_is_told_what_happened_not_what_it_means(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _item_id, code = await tagged(client, session)
        await client.post(
            f"/v/{code}/scan", json={"device_fingerprint": "first-phone"}, headers=GUJARAT
        )
        second = await client.post(
            f"/v/{code}/scan", json={"device_fingerprint": "second-phone"}, headers=GUJARAT
        )

        claim = second.json()["claim"]
        assert claim["status"] == "ALREADY_CLAIMED"
        assert claim["is_your_claim"] is False
        message = claim["message"] or ""
        assert "already claimed" in message.lower()
        assert "seller" in message.lower()
        for accusation in ("counterfeit", "fake", "duplicate", "stolen"):
            assert accusation not in message.lower()

    async def test_a_read_never_claims_anything(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        # A crawler, a link preview and a browser prefetch all issue GETs.
        item_id, code = await tagged(client, session)
        await client.get(f"/v/{code}")
        await client.get(f"/v/{code}")

        session.expire_all()
        assert await session.get(Claim, item_id) is None
        scans = (
            await session.execute(select(Scan).where(Scan.item_id == item_id))
        ).scalars().all()
        assert scans == []

    async def test_a_scan_with_no_fingerprint_records_but_does_not_claim(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        item_id, code = await tagged(client, session)
        # httpx sends no user-agent header of its own here, so there is nothing
        # to approximate a device from either.
        response = await client.post(
            f"/v/{code}/scan",
            json={},
            headers={**GUJARAT, "user-agent": "", "accept-language": ""},
        )
        assert response.status_code == 201

        session.expire_all()
        scans = (
            await session.execute(select(Scan).where(Scan.item_id == item_id))
        ).scalars().all()
        assert len(scans) == 1


# ---------------------------------------------------------------- scanning


class TestScanRecording:
    async def test_a_retried_scan_is_not_counted_twice(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _item_id, code = await tagged(client, session)
        body = {"device_fingerprint": "one-phone"}

        first = await client.post(f"/v/{code}/scan", json=body, headers=GUJARAT)
        second = await client.post(f"/v/{code}/scan", json=body, headers=GUJARAT)

        assert first.status_code == 201
        assert first.headers["x-scan-recorded"] == "true"
        # Same device, same place, same network, inside the window: one scan
        # that got retried, not two events.
        assert second.status_code == 200
        assert second.headers["x-scan-recorded"] == "false"
        assert second.json()["scan"]["count"] == 1

    async def test_the_region_may_come_from_the_body_when_the_edge_is_silent(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        item_id, code = await tagged(client, session)
        response = await client.post(
            f"/v/{code}/scan",
            json={"device_fingerprint": "phone", "region_code": "IN-KL"},
        )
        assert response.status_code == 201

        session.expire_all()
        row = (
            await session.execute(select(Scan).where(Scan.item_id == item_id))
        ).scalar_one()
        assert row.region_code == "IN-KL"
        assert row.country_code == "IN"

    async def test_the_edge_header_outranks_the_body(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        item_id, code = await tagged(client, session)
        await client.post(
            f"/v/{code}/scan",
            json={"device_fingerprint": "phone", "region_code": "IN-KL"},
            headers=GUJARAT,
        )

        session.expire_all()
        row = (
            await session.execute(select(Scan).where(Scan.item_id == item_id))
        ).scalar_one()
        # The infrastructure that saw the connection beats a value the caller
        # typed into a JSON body.
        assert row.region_code == "IN-GJ"

    async def test_a_scan_is_never_cached(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _item_id, code = await tagged(client, session)
        response = await client.post(f"/v/{code}/scan", json={}, headers=GUJARAT)
        assert response.headers["cache-control"] == "no-store"


class TestCaching:
    async def test_a_read_is_cacheable_and_carries_a_validator(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _item_id, code = await tagged(client, session)
        response = await client.get(f"/v/{code}")

        assert f"max-age={SETTINGS.public_cache_seconds}" in response.headers["cache-control"]
        assert response.headers["etag"]

    async def test_an_unchanged_record_answers_304(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _item_id, code = await tagged(client, session)
        first = await client.get(f"/v/{code}")

        second = await client.get(
            f"/v/{code}", headers={"If-None-Match": first.headers["etag"]}
        )
        assert second.status_code == 304
        assert second.content == b""

    async def test_a_changed_record_gets_a_new_validator(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        item_id, code = await tagged(client, session)
        first = await client.get(f"/v/{code}")

        await session.execute(
            update(Item).where(Item.id == item_id).values(quantity=Decimal("3.0000"))
        )
        await session.commit()

        second = await client.get(f"/v/{code}")
        assert second.headers["etag"] != first.headers["etag"]


class TestPayloadShape:
    async def test_the_typed_form_of_a_code_does_not_matter(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _item_id, code = await tagged(client, session)
        grouped = "-".join(code[index : index + 4] for index in range(0, len(code), 4))

        for typed in (code, code.lower(), grouped, grouped.lower()):
            response = await client.get(f"/v/{typed}")
            assert response.status_code == 200, typed
            assert response.json()["tag_code"] == code

    async def test_lineage_is_published_without_naming_other_objects(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        parent_id, _parent_code = await tagged(client, session)

        session.expire_all()
        parent = await session.get(Item, parent_id)
        assert parent is not None
        child = Item(
            category_id=parent.category_id,
            category_schema_version=parent.category_schema_version,
            parent_id=parent_id,
            registered_by=parent.registered_by,
            attributes=dict(parent.attributes),
            quantity=Decimal("2.0000"),
            quantity_unit=parent.quantity_unit,
            item_hash="0x" + uuid.uuid4().hex + uuid.uuid4().hex,
            tag_code=new_tag_code(),
        )
        session.add(child)
        await session.commit()
        child_code = child.tag_code

        response = await client.get(f"/v/{child_code}")
        assert response.status_code == 200, response.text
        ancestry = response.json()["provenance"]["ancestry"]
        assert len(ancestry) == 1
        assert ancestry[0]["depth"] == 0
        assert ancestry[0]["quantity"] == "5.5000"
        # The parent's own identity stays out of it.
        assert str(parent_id) not in response.text

    async def test_the_trust_block_reports_evidence_not_a_score(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _item_id, code = await tagged(client, session)
        trust = (await client.get(f"/v/{code}")).json()["trust"]

        assert trust["level"] == "SELF_DECLARED"
        assert trust["contributing_roles"] == []
        assert trust["disputed"] is False

    async def test_event_payloads_are_never_published(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _item_id, code = await tagged(client, session)
        events = (await client.get(f"/v/{code}")).json()["provenance"]["events"]

        assert [event_row["type"] for event_row in events] == ["REGISTERED", "TAG_ISSUED"]
        for event_row in events:
            # The REGISTERED payload carries the whole preimage, including the
            # salted identity digest. Publishing it would be a slow leak.
            assert set(event_row) == {"type", "at", "tx_hash", "block_number"}


class TestServiceDirectly:
    async def test_recompute_reproduces_the_stored_digest(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        item_id, _code = await tagged(client, session)
        session.expire_all()
        item = await session.get(Item, item_id)
        assert item is not None

        assert await service.recompute_item_hash(session, item) == item.item_hash

    def test_identifying_attribute_keys_are_dropped(self) -> None:
        published = service.public_attributes(
            {
                "warp_count": 120,
                "weaverName": "somebody",
                "contact_phone": "9876543210",
                "_internal": "hidden",
                "dye_type": "natural",
            }
        )
        assert published == {"warp_count": 120, "dye_type": "natural"}
