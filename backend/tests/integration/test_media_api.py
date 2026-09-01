"""The endpoints, the item linkage, and the pin retry worker.

The retry tests drive the real drain against the real outbox, because the whole
point of reusing Phase 7's mechanism is that pins get the same backoff and the
same dead-lettering anchors get. A second retry loop asserted against a stub
would prove nothing about that.
"""

from __future__ import annotations

import io
import shutil
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.clock import now
from app.db.models.enums import (
    MediaKind,
    OutboxJobType,
    OutboxStatus,
    PinStatus,
    UserRole,
    UserStatus,
)
from app.db.models.media import ItemMedia, Media
from app.db.models.ops import DeadLetter
from app.db.models.outbox import Outbox
from app.db.models.user import User
from app.media.mirror import MirrorStore
from app.media.pinata import PinataClient
from app.media.service import ingest
from app.workers.jobs import build_runtime, drain_pin_queue
from tests.fakes.chain_harness import make_category, seed_item
from tests.fakes.fake_pinata import (
    EXE_BYTES,
    JPEG_BYTES,
    PNG_BYTES,
    FakePinata,
    fake_cid,
    pinata_down,
    pinata_ok,
)

pytestmark = pytest.mark.integration

API = get_settings().api_prefix
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def mirror_root(tmp_path: Any) -> Any:
    root = tmp_path / "mirror"
    root.mkdir()
    return root


@pytest.fixture(autouse=True)
def media_settings(monkeypatch: Any, mirror_root: Any) -> Any:
    """Point every media module at a throwaway mirror and a fake JWT.

    The router resolves settings through ``get_settings`` at call time, so the
    cached singleton is patched rather than threaded through: the endpoints take
    no settings argument, and giving them one only for tests would change the
    production signature to suit the test suite.
    """
    from app.config import get_settings as real

    base = real()
    patched = base.model_copy(
        update={
            "ipfs_mirror_dir": mirror_root,
            "pinata_jwt": "test-jwt-never-real",
        }
    )
    import app.config as config_module
    import app.media.mirror as mirror_module
    import app.media.pinata as pinata_module
    import app.media.router as router_module
    import app.media.service as service_module

    for module in (config_module, mirror_module, pinata_module, service_module, router_module):
        monkeypatch.setattr(module, "get_settings", lambda: patched, raising=False)
    return patched


async def make_user(session: AsyncSession, role: UserRole) -> tuple[User, str]:
    from app.auth.password import hash_password
    from app.core.crypto_shred import new_salt

    email = f"{role.lower()}-{uuid.uuid4().hex[:10]}@example.com"
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


async def auth(client: httpx.AsyncClient, email: str) -> dict[str, str]:
    response = await client.post(
        f"{API}/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def upload_file(data: bytes, name: str = "loom.jpg", content_type: str = "image/jpeg"):
    return {"file": (name, data, content_type)}


class TestUploadEndpoint:
    async def test_upload_returns_201_with_every_tier(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _, email = await make_user(session, UserRole.WEAVER)
        headers = await auth(client, email)

        with pinata_ok():
            response = await client.post(
                f"{API}/media", files=upload_file(JPEG_BYTES), headers=headers
            )

        assert response.status_code == 201, response.text
        body = response.json()
        assert len(body["sha256"]) == 64
        assert body["cid"] == fake_cid(JPEG_BYTES)
        assert body["pin_status"] == PinStatus.PINNED
        assert [tier["tier"] for tier in body["tiers"]] == ["IPFS", "MIRROR", "BLOB"]
        assert body["primary_tier"] == "IPFS"
        # Only the database copy survives a redeploy, and the payload says so.
        assert body["durable"] is True

    async def test_an_exe_renamed_to_jpg_is_415_despite_the_header(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _, email = await make_user(session, UserRole.WEAVER)
        headers = await auth(client, email)

        response = await client.post(
            f"{API}/media",
            # The client swears this is a JPEG. It is not.
            files=upload_file(EXE_BYTES, name="loom.jpg", content_type="image/jpeg"),
            headers=headers,
        )

        assert response.status_code == 415
        assert response.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"

    async def test_a_consumer_may_not_upload(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _, email = await make_user(session, UserRole.CONSUMER)

        response = await client.post(
            f"{API}/media", files=upload_file(JPEG_BYTES), headers=await auth(client, email)
        )

        assert response.status_code == 403

    async def test_uploading_requires_authentication(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post(f"{API}/media", files=upload_file(JPEG_BYTES))
        assert response.status_code == 401

    async def test_the_same_file_twice_returns_the_same_id(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _, email = await make_user(session, UserRole.WEAVER)
        headers = await auth(client, email)

        with pinata_ok() as recorder:
            first = await client.post(
                f"{API}/media", files=upload_file(JPEG_BYTES), headers=headers
            )
            second = await client.post(
                f"{API}/media", files=upload_file(JPEG_BYTES, name="other.jpg"), headers=headers
            )

        assert first.status_code == 201
        assert second.status_code == 201
        # Content addressing: the filename is irrelevant, the bytes are the key.
        assert first.json()["id"] == second.json()["id"]
        assert len((await session.execute(select(Media))).scalars().all()) == 1
        assert recorder.call_count == 1


class TestPinataUnconfiguredEndToEnd:
    async def test_upload_succeeds_and_readyz_says_unconfigured(
        self, client: httpx.AsyncClient, session: AsyncSession, monkeypatch: Any
    ) -> None:
        from app.config import get_settings as real

        blank = real().model_copy(update={"pinata_jwt": ""})
        import app.api.health as health_module
        import app.media.pinata as pinata_module
        import app.media.service as service_module

        for module in (health_module, pinata_module, service_module):
            monkeypatch.setattr(module, "get_settings", lambda: blank, raising=False)

        _, email = await make_user(session, UserRole.WEAVER)
        headers = await auth(client, email)

        recorder = FakePinata()
        with pinata_ok(recorder):
            response = await client.post(
                f"{API}/media", files=upload_file(JPEG_BYTES), headers=headers
            )

        assert response.status_code == 201, response.text
        assert response.json()["pin_status"] == PinStatus.PIN_PENDING
        assert response.json()["cid"] is None
        assert recorder.never_called

        ready = await client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["checks"]["pinata"]["status"] == "unconfigured"


class TestServingBytes:
    async def test_raw_serves_from_the_mirror_first(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _, email = await make_user(session, UserRole.WEAVER)
        headers = await auth(client, email)

        with pinata_down():
            uploaded = await client.post(
                f"{API}/media", files=upload_file(PNG_BYTES, name="x.png"), headers=headers
            )
        media_id = uploaded.json()["id"]

        response = await client.get(f"{API}/media/{media_id}/raw", headers=headers)

        assert response.status_code == 200
        assert response.content == PNG_BYTES
        assert response.headers["x-sutradhar-tier"] == "MIRROR"
        assert response.headers["content-type"] == "image/png"
        # Content-addressed bytes never change, so they cache forever.
        assert "immutable" in response.headers["cache-control"]

    async def test_raw_falls_back_to_the_blob_when_the_mirror_is_wiped(
        self, client: httpx.AsyncClient, session: AsyncSession, mirror_root: Any
    ) -> None:
        _, email = await make_user(session, UserRole.WEAVER)
        headers = await auth(client, email)

        with pinata_down():
            uploaded = await client.post(
                f"{API}/media", files=upload_file(JPEG_BYTES), headers=headers
            )
        media_id = uploaded.json()["id"]

        # Exactly what a redeploy does to Render's ephemeral disk.
        shutil.rmtree(mirror_root)
        mirror_root.mkdir()

        response = await client.get(f"{API}/media/{media_id}/raw", headers=headers)

        assert response.status_code == 200
        assert response.content == JPEG_BYTES
        # The tier the design calls mandatory, doing the job it exists for.
        assert response.headers["x-sutradhar-tier"] == "BLOB"

    async def test_asking_for_a_wiped_tier_still_falls_through(
        self, client: httpx.AsyncClient, session: AsyncSession, mirror_root: Any
    ) -> None:
        _, email = await make_user(session, UserRole.WEAVER)
        headers = await auth(client, email)
        with pinata_down():
            uploaded = await client.post(
                f"{API}/media", files=upload_file(JPEG_BYTES), headers=headers
            )
        media_id = uploaded.json()["id"]
        shutil.rmtree(mirror_root)
        mirror_root.mkdir()

        response = await client.get(
            f"{API}/media/{media_id}/raw?tier=MIRROR", headers=headers
        )

        # A preference, not an instruction. 404ing on a tier that used to exist
        # would be the API insisting on a stale fact.
        assert response.status_code == 200
        assert response.headers["x-sutradhar-tier"] == "BLOB"

    async def test_metadata_lists_the_available_tiers(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _, email = await make_user(session, UserRole.WEAVER)
        headers = await auth(client, email)
        with pinata_down():
            uploaded = await client.post(
                f"{API}/media", files=upload_file(JPEG_BYTES), headers=headers
            )

        response = await client.get(f"{API}/media/{uploaded.json()['id']}", headers=headers)

        assert response.status_code == 200
        body = response.json()
        # No IPFS tier: the pin failed, so offering the gateway would hand the
        # frontend a fallback chain with a hole in it.
        assert [tier["tier"] for tier in body["tiers"]] == ["MIRROR", "BLOB"]
        assert body["primary_tier"] == "MIRROR"


class TestItemLinkage:
    async def test_the_registrant_can_attach_and_detach(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        weaver, email = await make_user(session, UserRole.WEAVER)
        category = await make_category(session)
        item = await seed_item(session, weaver, category, enqueue=False)
        await session.commit()
        headers = await auth(client, email)

        with pinata_ok():
            uploaded = await client.post(
                f"{API}/media", files=upload_file(JPEG_BYTES), headers=headers
            )
        media_id = uploaded.json()["id"]

        attached = await client.post(
            f"{API}/items/{item.id}/media",
            json={"media_id": media_id, "kind": MediaKind.LOOM_PHOTO},
            headers=headers,
        )
        assert attached.status_code == 201, attached.text
        assert attached.json()["kind"] == MediaKind.LOOM_PHOTO

        listed = await client.get(f"{API}/items/{item.id}/media", headers=headers)
        assert len(listed.json()) == 1

        detached = await client.delete(
            f"{API}/items/{item.id}/media/{media_id}", headers=headers
        )
        assert detached.status_code == 204

        assert (await session.execute(select(ItemMedia))).first() is None
        # Never hard-deleted: the SHA-256 may already be anchored, and a hash
        # pointing at bytes this system threw away is the dead reference the
        # three-tier design exists to prevent.
        remaining = (await session.execute(select(Media))).scalar_one()
        assert str(remaining.id) == media_id
        assert remaining.blob is not None

    async def test_a_non_owner_may_not_attach(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        weaver, _ = await make_user(session, UserRole.WEAVER)
        _, other_email = await make_user(session, UserRole.WEAVER)
        category = await make_category(session)
        item = await seed_item(session, weaver, category, enqueue=False)
        await session.commit()
        headers = await auth(client, other_email)

        with pinata_ok():
            uploaded = await client.post(
                f"{API}/media", files=upload_file(JPEG_BYTES), headers=headers
            )

        response = await client.post(
            f"{API}/items/{item.id}/media",
            json={"media_id": uploaded.json()["id"], "kind": MediaKind.LOOM_PHOTO},
            headers=headers,
        )

        assert response.status_code == 403

    async def test_an_admin_may_attach_to_anyone_s_item(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        weaver, _ = await make_user(session, UserRole.WEAVER)
        _, admin_email = await make_user(session, UserRole.ADMIN)
        category = await make_category(session)
        item = await seed_item(session, weaver, category, enqueue=False)
        await session.commit()
        headers = await auth(client, admin_email)

        with pinata_ok():
            uploaded = await client.post(
                f"{API}/media", files=upload_file(JPEG_BYTES), headers=headers
            )

        response = await client.post(
            f"{API}/items/{item.id}/media",
            json={"media_id": uploaded.json()["id"], "kind": MediaKind.CERTIFICATE},
            headers=headers,
        )

        assert response.status_code == 201

    async def test_weave_macro_is_accepted_and_stored(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        weaver, email = await make_user(session, UserRole.WEAVER)
        category = await make_category(session)
        item = await seed_item(session, weaver, category, enqueue=False)
        await session.commit()
        headers = await auth(client, email)

        with pinata_ok():
            uploaded = await client.post(
                f"{API}/media", files=upload_file(JPEG_BYTES), headers=headers
            )

        response = await client.post(
            f"{API}/items/{item.id}/media",
            json={"media_id": uploaded.json()["id"], "kind": MediaKind.WEAVE_MACRO},
            headers=headers,
        )

        # Stored for the fingerprinting roadmap. Nothing in this phase matches
        # on it; the corpus just has to exist before anything can.
        assert response.status_code == 201
        link = (await session.execute(select(ItemMedia))).scalar_one()
        assert link.kind is MediaKind.WEAVE_MACRO

    async def test_attaching_the_same_media_twice_is_409(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        weaver, email = await make_user(session, UserRole.WEAVER)
        category = await make_category(session)
        item = await seed_item(session, weaver, category, enqueue=False)
        await session.commit()
        headers = await auth(client, email)

        with pinata_ok():
            uploaded = await client.post(
                f"{API}/media", files=upload_file(JPEG_BYTES), headers=headers
            )
        payload = {"media_id": uploaded.json()["id"], "kind": MediaKind.LOOM_PHOTO}

        assert (
            await client.post(f"{API}/items/{item.id}/media", json=payload, headers=headers)
        ).status_code == 201
        second = await client.post(
            f"{API}/items/{item.id}/media", json=payload, headers=headers
        )

        assert second.status_code == 409

    async def test_detaching_something_not_linked_is_404(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        weaver, email = await make_user(session, UserRole.WEAVER)
        category = await make_category(session)
        item = await seed_item(session, weaver, category, enqueue=False)
        await session.commit()

        response = await client.delete(
            f"{API}/items/{item.id}/media/{uuid.uuid4()}",
            headers=await auth(client, email),
        )

        assert response.status_code == 404


class TestPinRetryWorker:
    async def test_a_pending_pin_is_retried_and_succeeds(
        self, session: AsyncSession, session_factory: Any, media_settings: Any
    ) -> None:
        uploader, _ = await make_user(session, UserRole.WEAVER)

        with pinata_down():
            await ingest(
                session,
                session_factory,
                io.BytesIO(JPEG_BYTES),
                uploader,
                settings=media_settings,
                pinata=PinataClient(media_settings),
                store=MirrorStore(media_settings),
            )
        await session.commit()

        media = (await session.execute(select(Media))).scalar_one()
        assert media.pin_status is PinStatus.PIN_PENDING

        runtime = build_runtime(session_factory, media_settings)
        with pinata_ok() as recorder:
            handled = await drain_pin_queue(runtime)

        assert handled == 1
        assert recorder.call_count == 1
        await session.refresh(media)
        assert media.pin_status is PinStatus.PINNED
        assert media.cid == fake_cid(JPEG_BYTES)

        job = (await session.execute(select(Outbox))).scalar_one()
        await session.refresh(job)
        assert job.status is OutboxStatus.DONE

    async def test_the_retry_uses_the_phase_seven_backoff_and_dead_letters(
        self, session: AsyncSession, session_factory: Any, media_settings: Any
    ) -> None:
        settings = media_settings.model_copy(update={"outbox_max_attempts": 2})
        uploader, _ = await make_user(session, UserRole.WEAVER)

        with pinata_down():
            await ingest(
                session,
                session_factory,
                io.BytesIO(JPEG_BYTES),
                uploader,
                settings=settings,
                pinata=PinataClient(settings),
                store=MirrorStore(settings),
            )
        await session.commit()

        runtime = build_runtime(session_factory, settings)
        for _ in range(2):
            job = (await session.execute(select(Outbox))).scalar_one()
            await session.refresh(job)
            job.next_attempt_at = now()
            await session.commit()
            with pinata_down():
                await drain_pin_queue(runtime)

        job = (await session.execute(select(Outbox))).scalar_one()
        await session.refresh(job)
        assert job.status is OutboxStatus.DEAD

        letter = (await session.execute(select(DeadLetter))).scalar_one()
        # The same dead-letter record anchoring failures produce: every attempt,
        # not just the last.
        assert letter.attempts == 2
        assert letter.error_chain.count("attempt ") == 2

        media = (await session.execute(select(Media))).scalar_one()
        await session.refresh(media)
        assert media.pin_status is PinStatus.PIN_FAILED
        # Nothing is lost. The file still resolves; it is simply not on IPFS.
        assert media.blob == JPEG_BYTES
        assert MirrorStore(settings).exists(media.mirror_path)

    async def test_the_pin_drain_does_not_claim_anchor_jobs(
        self, session: AsyncSession, session_factory: Any, media_settings: Any
    ) -> None:
        weaver, _ = await make_user(session, UserRole.WEAVER)
        category = await make_category(session)
        await seed_item(session, weaver, category)
        await session.commit()

        anchor_job = (await session.execute(select(Outbox))).scalar_one()
        assert anchor_job.job_type is OutboxJobType.ANCHOR_ITEM

        runtime = build_runtime(session_factory, media_settings)
        handled = await drain_pin_queue(runtime)

        # A drain that claimed a job it cannot run would kill it as unsupported.
        assert handled == 0
        await session.refresh(anchor_job)
        assert anchor_job.status is OutboxStatus.QUEUED

    async def test_a_pin_with_no_local_copy_left_is_parked_not_retried(
        self, session: AsyncSession, session_factory: Any, media_settings: Any, mirror_root: Any
    ) -> None:
        settings = media_settings.model_copy(update={"media_blob_max_bytes": 1})
        uploader, _ = await make_user(session, UserRole.WEAVER)

        with pinata_down():
            await ingest(
                session,
                session_factory,
                io.BytesIO(JPEG_BYTES),
                uploader,
                settings=settings,
                pinata=PinataClient(settings),
                store=MirrorStore(settings),
            )
        await session.commit()

        shutil.rmtree(mirror_root)
        mirror_root.mkdir()

        runtime = build_runtime(session_factory, settings)
        with pinata_ok() as recorder:
            await drain_pin_queue(runtime)

        # No bytes left to upload, so no number of retries can help.
        assert recorder.never_called
        job = (await session.execute(select(Outbox))).scalar_one()
        await session.refresh(job)
        assert job.status is OutboxStatus.DEAD
        media = (await session.execute(select(Media))).scalar_one()
        await session.refresh(media)
        assert media.pin_status is PinStatus.PIN_FAILED

    async def test_with_no_jwt_the_queue_is_released_not_exhausted(
        self, session: AsyncSession, session_factory: Any, media_settings: Any
    ) -> None:
        settings = media_settings.model_copy(update={"pinata_jwt": ""})
        uploader, _ = await make_user(session, UserRole.WEAVER)

        await ingest(
            session,
            session_factory,
            io.BytesIO(JPEG_BYTES),
            uploader,
            settings=settings,
            pinata=PinataClient(settings),
            store=MirrorStore(settings),
        )
        await session.commit()

        runtime = build_runtime(session_factory, settings)
        await drain_pin_queue(runtime)

        job = (await session.execute(select(Outbox))).scalar_one()
        await session.refresh(job)
        # A deployment with no JWT must not dead-letter its whole media queue.
        assert job.status is OutboxStatus.QUEUED
        assert job.attempts == 0
