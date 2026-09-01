"""One object, from a Google sign-in to a fraud flag, through the real API.

Every other integration file tests one seam. This one is the whole rope, and it
exists to catch the failures that only appear where two phases meet: a category
added at runtime that the registration path cannot see, a hash that stops
matching once a tag is bound, a trust level that does not move when an actor is
flagged.

**Two variants of one test, and the primary one is the boring configuration.**
``chain_off`` is the system exactly as it runs today -- nothing deployed to
Amoy, ``CHAIN_WRITE_ENABLED=false``, no signer, no Pinata JWT -- and it must
pass first. ``chain_on`` wires the offline EVM from Phase 7 and proves the
anchored path also works. Ordering matters: if only the anchored variant existed,
the configuration that will actually be running during a demo would be the one
nobody tested.

**Only the network is faked.** Google is real RSA and real signatures, Pinata is
real HTTP against a mock socket, the chain is an in-memory EVM that mines and
reorgs. Everything above the socket -- the routers, the services, the hasher, the
outbox, the indexer -- is production code. PostgreSQL is real throughout.
"""

from __future__ import annotations

import io
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
import respx
import zxingcpp
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.oauth.google import reset_jwks_cache
from app.config import get_settings
from app.core.ids import normalize_tag_code
from app.db.models.catalog import Item
from app.db.models.enums import (
    ItemStatus,
    OutboxJobType,
    OutboxStatus,
    UserRole,
    UserStatus,
)
from app.db.models.media import Media
from app.db.models.outbox import Outbox
from app.db.models.user import User
from tests.fakes.chain_harness import ChainHarness, build_harness
from tests.fakes.fake_google import FakeGoogle, fake_google
from tests.fakes.fake_pinata import JPEG_BYTES, FakePinata, pinata_ok
from tests.integration.helpers import (
    API,
    ASSAM,
    GUJARAT,
    auth_headers,
    idempotency,
    make_user,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SETTINGS = get_settings()
OAUTH = f"{API}/auth/oauth"
CLIENT_ID = "test-client-id.apps.googleusercontent.com"

# The fourth category. Not one of the three seeded ones -- the point of step 3
# is that a category nobody anticipated becomes usable without a deploy, so it
# has to be a shape the running process has never seen.
BANARASI_SLUG = "banarasi-brocade"
BANARASI_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Banarasi brocade",
    "type": "object",
    "additionalProperties": False,
    "required": ["zari_type", "motif"],
    "properties": {
        "zari_type": {"type": "string", "enum": ["real", "tested", "imitation"]},
        "motif": {"type": "string"},
        "jangla": {"type": "boolean"},
        "gi_registration_no": {"type": "string"},
    },
}
BANARASI_ITEM: dict[str, Any] = {
    "zari_type": "real",
    "motif": "shikargah",
    "jangla": True,
    "gi_registration_no": "GI-00099",
}

# Words the public surface may never use about an object or a person. Kept here
# as well as in the unit guard because this file produces real payloads from a
# real flow, which is where a phrasing regression would actually show up.
FORBIDDEN_WORDS = (
    "counterfeit",
    "fake",
    "genuine",
    "authentic",
    "stolen",
    "fraud",
    "duplicate",
    "illegal",
)

# Values planted on the maker and never permitted to appear in a public payload.
IDENTIFYING = {
    "email_local": "kamala.devi.private",
    "display_name": "Kamala Devi",
}


@dataclass
class Flow:
    """Everything one variant of the walkthrough needs to talk to."""

    name: str
    client: httpx.AsyncClient
    harness: ChainHarness | None

    @property
    def anchors(self) -> bool:
        return self.harness is not None


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def google_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    get_settings.cache_clear()
    reset_jwks_cache()
    yield CLIENT_ID
    get_settings.cache_clear()
    reset_jwks_cache()


@pytest.fixture
def provider(google_enabled: str) -> FakeGoogle:
    return fake_google(google_enabled)


@pytest_asyncio.fixture(params=["chain_off", "chain_on"])
async def flow(
    request: pytest.FixtureRequest,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[Flow]:
    """The application, in one of the two configurations.

    ``chain_off`` builds no runtime at all, which is what
    ``app.main.lifespan`` produces when the scheduler is disabled -- and is
    therefore the state the public routes see today. ``chain_on`` attaches a
    runtime pointed at the offline EVM, so ``_chain_reader`` builds a real
    reader and the live comparison actually happens.
    """
    from app.db.session import get_session
    from app.main import create_app

    application = create_app()

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as db_session:
            yield db_session

    application.dependency_overrides[get_session] = override_session

    harness: ChainHarness | None = None
    if request.param == "chain_on":
        harness = build_harness(session_factory)
        await harness.client.connect()
        application.state.chain_runtime = harness.runtime

    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as made:
        yield Flow(name=request.param, client=made, harness=harness)

    application.dependency_overrides.clear()


# ---------------------------------------------------------------- the walk


class TestTheWholeThing:
    async def test_a_bolt_of_cloth_from_sign_in_to_fraud_flag(
        self,
        flow: Flow,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        provider: FakeGoogle,
    ) -> None:
        client = flow.client

        # -- 1. OAuth start and callback for an identity nobody has seen ------
        #
        # The callback must hand back a ticket and nothing else. A session here
        # would be a live account whose role nobody has chosen, and role is the
        # entire trust model: a self-declared WEAVER registers items other
        # people rely on.
        router = respx.mock(assert_all_called=False, assert_all_mocked=True)
        router.route(host="testserver").pass_through()
        provider.install(router)

        with router:
            start = await client.get(f"{OAUTH}/google/start")
            assert start.status_code == 302, start.text
            state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]

            callback = await client.get(
                f"{OAUTH}/google/callback",
                params={"code": "fake-auth-code", "state": state},
            )

            assert callback.status_code == 302, callback.text
            assert "set-cookie" not in {
                name.lower() for name in callback.headers
            }, "the callback issued a session cookie for an account with no role"
            location = callback.headers["location"]
            assert "access_token" not in location
            query = parse_qs(urlsplit(location).query)
            assert "pending_token" in query, location
            pending_token = query["pending_token"][0]

            # -- 2. Complete as a WEAVER -> session, PENDING_VERIFICATION -----
            #
            # A self-declared weaver is not a trusted weaver. The account is
            # usable and the status says a human has not checked the claim.
            completed = await client.post(
                f"{OAUTH}/complete",
                json={
                    "pending_token": pending_token,
                    "role": "WEAVER",
                    "display_name": IDENTIFYING["display_name"],
                    "region": "Varanasi, Uttar Pradesh",
                },
            )

        assert completed.status_code == 200, completed.text
        weaver_headers = {
            "Authorization": f"Bearer {completed.json()['access_token']}"
        }
        assert completed.json()["user"]["status"] == UserStatus.PENDING_VERIFICATION
        assert completed.json()["user"]["role"] == UserRole.WEAVER

        weaver = (
            await session.execute(
                select(User).where(User.role == UserRole.WEAVER)
            )
        ).scalar_one()
        # Read out now, as plain values. The chain workers below commit through
        # their own sessions and this one gets expired to see their writes; an
        # expired ORM attribute re-fetches on access, which from a synchronous
        # assertion helper is a MissingGreenlet rather than a value.
        secrets = _secrets_of(weaver)
        weaver_id = weaver.id

        # -- 3. An admin adds a fourth GI category, live -----------------------
        #
        # Timed because the claim being made is operational, not architectural:
        # a co-op turning up with a textile the system has never heard of is
        # onboarded during the conversation, not in the next release.
        admin = await make_user(session, UserRole.ADMIN, prefix="e2e")
        admin_headers = await auth_headers(client, admin)

        started = time.perf_counter()
        created = await client.post(
            f"{API}/admin/categories",
            json={
                "slug": BANARASI_SLUG,
                "display_name": "Banarasi Brocade",
                "is_textile": True,
                "quantity_unit": "metre",
                "attribute_schema": BANARASI_SCHEMA,
            },
            headers={**admin_headers, **idempotency()},
        )
        elapsed = time.perf_counter() - started

        assert created.status_code == 201, created.text
        assert elapsed < 30, f"adding a category took {elapsed:.1f}s"

        # Usable from the very next request, with no restart.
        listed = await client.get(f"{API}/categories", headers=weaver_headers)
        assert BANARASI_SLUG in [row["slug"] for row in listed.json()["data"]]

        # -- 4. Register an item in the brand new category --------------------
        registered = await client.post(
            f"{API}/items",
            json={
                "category_slug": BANARASI_SLUG,
                "attributes": BANARASI_ITEM,
                "quantity": "6.5000",
                "quantity_unit": "metre",
            },
            headers={**weaver_headers, **idempotency()},
        )
        assert registered.status_code == 201, registered.text
        item_id = uuid.UUID(registered.json()["id"])
        assert registered.json()["status"] == ItemStatus.PENDING

        jobs = list(
            (
                await session.execute(
                    select(Outbox).where(Outbox.job_type == OutboxJobType.ANCHOR_ITEM)
                )
            )
            .scalars()
            .all()
        )
        assert len(jobs) == 1, f"{len(jobs)} anchoring jobs for one registration"
        assert jobs[0].payload["item_id"] == str(item_id)

        # -- 5. Upload a loom photo and attach it ------------------------------
        #
        # The digest is the integrity proof and it is committed before anything
        # is attempted over the network. An empty PINATA_JWT must not fail the
        # upload -- a weaver's photograph is not contingent on a third party.
        recorder = FakePinata()
        with pinata_ok(recorder):
            uploaded = await client.post(
                f"{API}/media",
                files={"file": ("loom.jpg", JPEG_BYTES, "image/jpeg")},
                headers=weaver_headers,
            )

        assert uploaded.status_code == 201, uploaded.text
        media_id = uploaded.json()["id"]
        digest = uploaded.json()["sha256"]
        assert digest, "no digest, so nothing was proved about these bytes"
        assert recorder.never_called, "Pinata was called with no JWT configured"

        stored = (
            await session.execute(select(Media).where(Media.id == uuid.UUID(media_id)))
        ).scalar_one()
        assert stored.sha256 == digest, "the digest was not persisted"
        assert stored.cid is None

        attached = await client.post(
            f"{API}/items/{item_id}/media",
            json={"media_id": media_id, "kind": "LOOM_PHOTO"},
            headers=weaver_headers,
        )
        assert attached.status_code == 201, attached.text

        # -- 6. A co-op officer attests ---------------------------------------
        #
        # The level is derived on read, never stored. Nothing writes
        # CO_OP_ATTESTED anywhere; it is what the attestation set means.
        officer = await make_user(session, UserRole.COOP_OFFICER, prefix="e2e")
        officer_headers = await auth_headers(client, officer)

        attested = await client.post(
            f"{API}/items/{item_id}/attestations",
            json={"statement": {"loom_visited": True, "handloom": True}},
            headers=officer_headers,
        )
        assert attested.status_code == 201, attested.text

        trust = await client.get(f"{API}/items/{item_id}/trust", headers=officer_headers)
        assert trust.status_code == 200, trust.text
        assert trust.json()["level"] == "CO_OP_ATTESTED"
        assert "COOP_OFFICER" in trust.json()["contributing_roles"]
        await _assert_trust_is_not_stored(session)

        # -- 7. Issue a tag and decode the QR ---------------------------------
        #
        # Decoded from the rendered image rather than compared to the string the
        # API returned: the printed payload is the only artefact that outlives
        # this process, and the thing that has to be right is what a phone reads
        # off the picture.
        tagged = await client.post(
            f"{API}/items/{item_id}/tag", headers={**weaver_headers, **idempotency()}
        )
        assert tagged.status_code == 201, tagged.text
        code = tagged.json()["tag_code"]

        png = await client.get(
            f"{API}/items/{item_id}/tag/qr?format=png", headers=weaver_headers
        )
        assert png.status_code == 200, png.text
        assert png.headers["content-type"] == "image/png"

        decoded = zxingcpp.read_barcode(Image.open(io.BytesIO(png.content)))
        assert decoded is not None, "the rendered QR did not decode"
        assert decoded.text == f"{SETTINGS.public_base_url}/v/{code}", (
            f"the printed payload is {decoded.text!r}, which is not the URL a "
            "scan is supposed to open"
        )

        # -- 8. The public page ------------------------------------------------
        if flow.anchors:
            await _anchor_everything(flow, session)

        public = await client.get(f"/v/{code}")
        assert public.status_code == 200, public.text
        payload = public.json()
        chain = payload["chain"]

        if flow.anchors:
            assert chain["verification"] == "MATCH", chain
            assert chain["stale"] is False, (
                "a live chain read happened and the answer was still labelled stale"
            )
            assert chain["tx_hash"], chain
        else:
            # The ordinary state of this system today, and served as a 200.
            assert chain["verification"] == "UNANCHORED", chain
            assert chain["stale"] is True, chain
            assert chain["chain_checked_at"] is not None
            assert chain["tx_hash"] is None

        _assert_no_pii(public.text, secrets)
        assert payload["trust"]["level"] == "CO_OP_ATTESTED"
        assert payload["story"]["media"][0]["sha256"] == digest

        # -- 9. Scans: Gujarat, then Assam seconds later -----------------------
        first_scan = await client.post(
            f"/v/{code}/scan",
            json={"device_fingerprint": "shopper-phone-a"},
            headers=GUJARAT,
        )
        assert first_scan.status_code == 201, first_scan.text
        assert first_scan.json()["claim"]["is_your_claim"] is True
        assert first_scan.json()["scan"]["suspicion_level"] == "NONE"
        assert first_scan.json()["claim"]["message"] is None, (
            "the first device to scan was told something about its own claim"
        )

        second_scan = await client.post(
            f"/v/{code}/scan",
            json={"device_fingerprint": "shopper-phone-b"},
            headers=ASSAM,
        )
        assert second_scan.status_code == 201, second_scan.text
        scan_block = second_scan.json()["scan"]
        assert scan_block["suspicion_level"] == "SUSPICIOUS", scan_block
        assert "IMPOSSIBLE_VELOCITY" in scan_block["signals"], scan_block
        assert scan_block["reason"], "a signal fired with nothing to show a reader"

        # The sentence a shopper reads. It describes a pattern; it does not
        # accuse them of holding something.
        _assert_no_verdict_language(scan_block["reason"])
        _assert_no_verdict_language(second_scan.json()["claim"]["message"] or "")
        assert second_scan.json()["claim"]["status"] == "ALREADY_CLAIMED"
        _assert_no_pii(second_scan.text, secrets)

        # -- 10. The admin fraud-flags the weaver ------------------------------
        #
        # The level is derived, so this takes effect on the next read with no
        # cache to invalidate and no backfill to wait for.
        flagged = await client.post(
            f"{API}/admin/actors/{weaver_id}/fraud-flag",
            json={"reason": "duplicate registrations reported by the co-operative"},
            headers=admin_headers,
        )
        assert flagged.status_code == 200, flagged.text
        assert flagged.json()["items_affected"] >= 1

        after = await client.get(f"/v/{code}")
        assert after.status_code == 200, after.text
        final = after.json()
        assert final["trust"]["level"] == "DISPUTED", final["trust"]
        assert final["trust"]["disputed"] is True

        # The claim is somebody's record of an object they hold. A flag on the
        # maker is not a reason to take it away from them.
        assert final["claim"]["claimed"] is True
        assert final["claim"]["claimed_at"] == first_scan.json()["claim"]["claimed_at"]

        _assert_no_pii(after.text, secrets)


# ---------------------------------------------------------------- assertions


def _assert_no_verdict_language(text: str) -> None:
    """No sentence shown to the public may pronounce on an object or a person."""
    lowered = text.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered, (
            f"the public surface used the word {word!r} in: {text!r}. This system "
            "reports who vouched and what the scan pattern looks like; it does "
            "not deliver verdicts."
        )


def _secrets_of(weaver: User) -> dict[str, str]:
    """The values that must never reach a public payload, as plain strings."""
    salt = weaver.identity_salt
    return {
        "registrant email": weaver.email,
        "registrant id": str(weaver.id),
        # Stored as bytes; compared hex-encoded, because a payload that leaked
        # it would most likely do so that way.
        "identity salt": (
            salt.hex() if isinstance(salt, bytes | bytearray) else str(salt)
        ),
    }


def _assert_no_pii(raw: str, secrets: dict[str, str]) -> None:
    """Substring search over the exact bytes that went on the wire.

    Crude on purpose. A structural assertion about which fields are published
    only ever checks the fields somebody remembered; searching the payload
    catches the one added next year.
    """
    for label, value in secrets.items():
        assert value not in raw, f"{label} appeared in a public payload"

    # The maker chose to be shown, so their display name is allowed. Nothing
    # else about them is.
    assert IDENTIFYING["email_local"] not in raw


async def _assert_trust_is_not_stored(session: AsyncSession) -> None:
    """No column anywhere holds a trust level.

    The ladder is a pure function of the attestation and dispute sets, and this
    is what makes a fraud flag take effect on the next read everywhere at once.
    A stored level would be a cache with no invalidation.
    """
    from app.db.base import Base

    for table in Base.metadata.tables.values():
        for column in table.columns:
            assert "trust" not in column.name.lower(), (
                f"{table.name}.{column.name} looks like a stored trust level"
            )


# ---------------------------------------------------------------- chain_on


async def _anchor_everything(flow: Flow, session: AsyncSession) -> None:
    """Drain, mine, confirm and index, so the anchored path is really anchored."""
    from app.workers.jobs import drain_outbox, run_indexer, sweep_confirmations

    assert flow.harness is not None
    runtime = flow.harness.runtime

    handled = await drain_outbox(runtime)
    assert handled >= 1, "the drain claimed nothing with jobs queued"

    # One block to include the transactions, then the confirmation depth on top
    # of it. Two separate steps because they mean different things: a receipt is
    # not a confirmation, and an item that went CONFIRMED on inclusion alone
    # would be telling a consumer their record is settled while it is still one
    # reorg from not existing.
    flow.harness.chain.mine()
    flow.harness.confirm_depth()

    await sweep_confirmations(runtime)
    await run_indexer(runtime)

    # The workers committed through their own sessions. This one is holding
    # instances it loaded earlier, and with `expire_on_commit=False` those keep
    # the attribute values they had then -- so a re-query would hand back the
    # pre-drain status and the assertions below would be about nothing.
    session.expire_all()

    unfinished = list(
        (
            await session.execute(
                select(Outbox.job_type, Outbox.status, Outbox.last_error).where(
                    Outbox.status != OutboxStatus.DONE,
                    Outbox.job_type == OutboxJobType.ANCHOR_ITEM,
                )
            )
        ).all()
    )
    assert not unfinished, f"anchoring jobs did not complete: {unfinished}"

    confirmed = (
        await session.execute(
            select(Item.id).where(Item.status == ItemStatus.CONFIRMED)
        )
    ).all()
    assert confirmed, "nothing reached CONFIRMED after a full drain and sweep"


# ---------------------------------------------------------------- the tag code


class TestTheTagCodeItself:
    """A guard on the one string that gets printed on cloth."""

    async def test_the_stored_code_is_the_code_in_the_url(
        self, flow: Flow, session: AsyncSession
    ) -> None:
        """Stored bare, displayed grouped, compared normalised.

        A tag scanned with the printed hyphens must resolve to the same record
        as one typed without them, or half the labels in a print run stop
        working the first time somebody reads one aloud.
        """
        from tests.integration.helpers import (
            issue_tag,
            load_catalogue,
            register_item,
        )

        await load_catalogue(session)
        weaver = await make_user(session, UserRole.WEAVER, prefix="tagcode")
        headers = await auth_headers(flow.client, weaver)
        item_id = await register_item(flow.client, headers)
        code = await issue_tag(flow.client, headers, item_id)

        grouped = "-".join(code[index : index + 4] for index in range(0, len(code), 4))
        assert normalize_tag_code(grouped) == code

        bare = await flow.client.get(f"/v/{code}")
        hyphenated = await flow.client.get(f"/v/{grouped}")
        lowercased = await flow.client.get(f"/v/{grouped.lower()}")

        assert bare.status_code == 200, bare.text
        assert hyphenated.status_code == 200, hyphenated.text
        assert lowercased.status_code == 200, lowercased.text
        assert bare.json()["tag_code"] == hyphenated.json()["tag_code"] == code
