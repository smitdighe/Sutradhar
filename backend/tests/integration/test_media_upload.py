"""The upload pipeline: refusals, deduplication, budgets, and a pin that may fail.

Every test here is about the pipeline being in the right *order*. Sniffing after
storing, budgeting after the network call, or pinning before persisting the
digest would all still pass a naive "does an upload work" test, and each one is
a different way to be wrong when something goes sideways.
"""

from __future__ import annotations

import io
import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InsufficientStorageError, ValidationError
from app.db.models.enums import OutboxJobType, OutboxStatus, PinStatus, UserRole, UserStatus
from app.db.models.media import Media
from app.db.models.outbox import Outbox
from app.db.models.user import User
from app.media import service
from app.media.mirror import MirrorStore, Tier, resolve
from app.media.pinata import PinataClient
from app.media.service import ingest, sniff_content_type
from tests.fakes.fake_pinata import (
    EXE_BYTES,
    JPEG_BYTES,
    MP4_BYTES,
    PNG_BYTES,
    WEBP_BYTES,
    FakePinata,
    fake_cid,
    pinata_down,
    pinata_ok,
    pinata_rejects,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def mirror_root(tmp_path: Any) -> Any:
    """A throwaway mirror directory, so tests never touch media_mirror/."""
    root = tmp_path / "mirror"
    root.mkdir()
    return root


def settings_for(mirror_root: Any, **overrides: Any) -> Any:
    from app.config import get_settings

    base = get_settings()
    defaults = {
        "ipfs_mirror_dir": mirror_root,
        "pinata_jwt": "test-jwt-never-real",
        "media_max_bytes": 5_242_880,
        "media_blob_max_bytes": 2_097_152,
        "media_blob_budget_bytes": 268_435_456,
        "pinata_storage_budget_bytes": 1_073_741_824,
    }
    return base.model_copy(update={**defaults, **overrides})


async def make_uploader(session: AsyncSession) -> User:
    from app.auth.password import hash_password
    from app.core.crypto_shred import new_salt

    user = User(
        email=f"weaver-{uuid.uuid4().hex[:10]}@example.com",
        password_hash=hash_password("correct-horse-battery-staple"),
        display_name="Uploading Weaver",
        role=UserRole.WEAVER,
        status=UserStatus.ACTIVE,
        identity_salt=new_salt(),
    )
    session.add(user)
    await session.flush()
    return user


async def upload_bytes(
    session: AsyncSession,
    session_factory: Any,
    data: bytes,
    uploader: User,
    settings: Any,
) -> Any:
    return await ingest(
        session,
        session_factory,
        io.BytesIO(data),
        uploader,
        settings=settings,
        pinata=PinataClient(settings),
        store=MirrorStore(settings),
    )


class TestByteSniffing:
    @pytest.mark.parametrize(
        "data,expected",
        [
            (JPEG_BYTES, "image/jpeg"),
            (PNG_BYTES, "image/png"),
            (WEBP_BYTES, "image/webp"),
            (MP4_BYTES, "video/mp4"),
        ],
    )
    def test_the_allowlist_is_recognised_from_magic_numbers(
        self, data: bytes, expected: str
    ) -> None:
        assert sniff_content_type(data) == expected

    def test_an_executable_is_not_recognised(self) -> None:
        assert sniff_content_type(EXE_BYTES) is None

    def test_a_quicktime_file_is_not_admitted_as_mp4(self) -> None:
        # 'ftyp' also fronts QuickTime, HEIF and AVIF. Accepting any ftyp box
        # would let three unlisted formats in through the mp4 entry.
        quicktime = (32).to_bytes(4, "big") + b"ftypqt  " + b"\x00" * 32
        assert sniff_content_type(quicktime) is None

    async def test_an_exe_renamed_to_jpg_is_refused_with_415(
        self, session: AsyncSession, session_factory: Any, mirror_root: Any
    ) -> None:
        settings = settings_for(mirror_root)
        uploader = await make_uploader(session)

        with pytest.raises(ValidationError) as caught:
            await upload_bytes(session, session_factory, EXE_BYTES, uploader, settings)

        # The client's Content-Type is not evidence. The bytes are.
        assert caught.value.status == 415
        assert str(caught.value.code) == "UNSUPPORTED_MEDIA_TYPE"
        assert (await session.execute(select(Media))).first() is None


class TestSizeCeiling:
    async def test_a_file_over_the_ceiling_is_refused_with_413(
        self, session: AsyncSession, session_factory: Any, mirror_root: Any
    ) -> None:
        settings = settings_for(mirror_root, media_max_bytes=1024)
        uploader = await make_uploader(session)
        oversized = JPEG_BYTES + b"\x00" * 4096

        with pytest.raises(ValidationError) as caught:
            await upload_bytes(session, session_factory, oversized, uploader, settings)

        assert caught.value.status == 413
        assert str(caught.value.code) == "MEDIA_TOO_LARGE"
        assert (await session.execute(select(Media))).first() is None

    async def test_an_empty_file_is_refused(
        self, session: AsyncSession, session_factory: Any, mirror_root: Any
    ) -> None:
        settings = settings_for(mirror_root)
        uploader = await make_uploader(session)

        with pytest.raises(ValidationError) as caught:
            await upload_bytes(session, session_factory, b"", uploader, settings)

        assert caught.value.status == 422

    async def test_the_ceiling_is_enforced_while_reading_not_after(
        self, session: AsyncSession, session_factory: Any, mirror_root: Any
    ) -> None:
        """A refusal must not require buffering the whole upload first."""
        settings = settings_for(mirror_root, media_max_bytes=1024)
        uploader = await make_uploader(session)

        consumed = 0

        class CountingStream(io.BytesIO):
            def read(self, size: int = -1) -> bytes:
                nonlocal consumed
                chunk = super().read(size)
                consumed += len(chunk)
                return chunk

        stream = CountingStream(JPEG_BYTES + b"\x00" * 5_000_000)
        with pytest.raises(ValidationError):
            await ingest(
                session,
                session_factory,
                stream,
                uploader,
                settings=settings,
                pinata=PinataClient(settings),
                store=MirrorStore(settings),
            )

        # Stopped within one chunk of the limit, not after the whole 5 MB.
        assert consumed <= 1024 + service.CHUNK_BYTES


class TestIntegrityBeforePinning:
    async def test_the_sha256_is_persisted_even_when_pinning_fails(
        self, session: AsyncSession, session_factory: Any, mirror_root: Any
    ) -> None:
        import hashlib

        settings = settings_for(mirror_root)
        uploader = await make_uploader(session)

        with pinata_down():
            result = await upload_bytes(
                session, session_factory, JPEG_BYTES, uploader, settings
            )
        await session.commit()

        media = (await session.execute(select(Media))).scalar_one()
        # A row with a digest and no CID is a complete integrity record. That is
        # the whole guarantee: the CID only says where a copy happens to live.
        assert media.sha256 == hashlib.sha256(JPEG_BYTES).hexdigest()
        assert media.cid is None
        assert media.pin_status is PinStatus.PIN_PENDING
        assert result.pinned is False

    async def test_a_failed_pin_still_returns_a_usable_upload(
        self, session: AsyncSession, session_factory: Any, mirror_root: Any
    ) -> None:
        settings = settings_for(mirror_root)
        uploader = await make_uploader(session)

        with pinata_down():
            result = await upload_bytes(
                session, session_factory, PNG_BYTES, uploader, settings
            )
        await session.commit()

        # Mirror written, blob stored, retry queued. Nothing lost.
        assert result.media.mirror_path is not None
        assert MirrorStore(settings).exists(result.media.mirror_path)
        assert result.media.blob == PNG_BYTES

        job = (await session.execute(select(Outbox))).scalar_one()
        assert job.job_type is OutboxJobType.PIN_MEDIA
        assert job.status is OutboxStatus.QUEUED

    async def test_a_rejected_pin_is_recorded_without_the_credential(
        self, session: AsyncSession, session_factory: Any, mirror_root: Any
    ) -> None:
        settings = settings_for(mirror_root)
        uploader = await make_uploader(session)

        with pinata_rejects(429) as recorder:
            result = await upload_bytes(
                session, session_factory, JPEG_BYTES, uploader, settings
            )
        await session.commit()

        assert result.pinned is False
        assert recorder.call_count == 1
        # The token reached the wire, as it must -- and not the error message.
        assert recorder.saw_authorization()
        assert result.pin_error is not None
        assert "test-jwt-never-real" not in result.pin_error

    async def test_a_successful_pin_records_the_cid(
        self, session: AsyncSession, session_factory: Any, mirror_root: Any
    ) -> None:
        settings = settings_for(mirror_root)
        uploader = await make_uploader(session)

        with pinata_ok():
            result = await upload_bytes(
                session, session_factory, JPEG_BYTES, uploader, settings
            )
        await session.commit()

        assert result.pinned is True
        assert result.media.cid == fake_cid(JPEG_BYTES)
        assert result.media.pin_status is PinStatus.PINNED
        # No retry job: there is nothing to retry.
        assert (await session.execute(select(Outbox))).first() is None


class TestPinataUnconfigured:
    async def test_uploads_work_with_no_jwt_and_nothing_is_called(
        self, session: AsyncSession, session_factory: Any, mirror_root: Any
    ) -> None:
        settings = settings_for(mirror_root, pinata_jwt="")
        uploader = await make_uploader(session)

        recorder = FakePinata()
        with pinata_ok(recorder):
            result = await upload_bytes(
                session, session_factory, JPEG_BYTES, uploader, settings
            )
        await session.commit()

        # A feature nobody switched on is not a failure.
        assert result.media.sha256
        assert result.pinned is False
        assert result.media.pin_status is PinStatus.PIN_PENDING
        assert recorder.never_called
        assert "disabled" in str(result.pin_error)


class TestDeduplication:
    async def test_the_same_bytes_twice_produce_one_row(
        self, session: AsyncSession, session_factory: Any, mirror_root: Any
    ) -> None:
        settings = settings_for(mirror_root)
        uploader = await make_uploader(session)

        with pinata_ok() as recorder:
            first = await upload_bytes(
                session, session_factory, JPEG_BYTES, uploader, settings
            )
            await session.commit()
            second = await upload_bytes(
                session, session_factory, JPEG_BYTES, uploader, settings
            )
            await session.commit()

        assert first.media.id == second.media.id
        assert second.deduplicated is True
        rows = (await session.execute(select(Media))).scalars().all()
        assert len(rows) == 1
        # Content addressing means the second upload never reaches the network.
        assert recorder.call_count == 1

    async def test_different_bytes_produce_different_rows(
        self, session: AsyncSession, session_factory: Any, mirror_root: Any
    ) -> None:
        settings = settings_for(mirror_root)
        uploader = await make_uploader(session)

        with pinata_ok():
            first = await upload_bytes(
                session, session_factory, JPEG_BYTES, uploader, settings
            )
            await session.commit()
            second = await upload_bytes(
                session, session_factory, PNG_BYTES, uploader, settings
            )
            await session.commit()

        assert first.media.id != second.media.id
        assert len((await session.execute(select(Media))).scalars().all()) == 2


class TestBudgets:
    async def test_an_upload_past_the_pinata_budget_is_refused_before_the_call(
        self, session: AsyncSession, session_factory: Any, mirror_root: Any
    ) -> None:
        settings = settings_for(mirror_root, pinata_storage_budget_bytes=10)
        uploader = await make_uploader(session)

        recorder = FakePinata()
        with pinata_ok(recorder), pytest.raises(InsufficientStorageError) as caught:
            await upload_bytes(session, session_factory, JPEG_BYTES, uploader, settings)

        assert caught.value.status == 507
        assert str(caught.value.code) == "STORAGE_BUDGET_EXCEEDED"
        # The whole point of checking first: the provider was never contacted.
        assert recorder.never_called
        assert (await session.execute(select(Media))).first() is None

    async def test_the_507_says_what_is_exhausted(
        self, session: AsyncSession, session_factory: Any, mirror_root: Any
    ) -> None:
        settings = settings_for(mirror_root, pinata_storage_budget_bytes=10)
        uploader = await make_uploader(session)

        with pinata_ok(), pytest.raises(InsufficientStorageError) as caught:
            await upload_bytes(session, session_factory, JPEG_BYTES, uploader, settings)

        details = caught.value.details or {}
        assert details["budget_bytes"] == 10
        assert details["file_bytes"] == len(JPEG_BYTES)

    async def test_a_file_over_the_inline_ceiling_skips_the_blob_but_uploads(
        self, session: AsyncSession, session_factory: Any, mirror_root: Any
    ) -> None:
        settings = settings_for(mirror_root, media_blob_max_bytes=16)
        uploader = await make_uploader(session)

        with pinata_ok():
            result = await upload_bytes(
                session, session_factory, JPEG_BYTES, uploader, settings
            )
        await session.commit()

        # Refusing the blob copy is not refusing the upload. The file still has
        # a mirror and a pin; it is simply not durable across a redeploy.
        assert result.blob_stored is False
        assert result.media.blob is None
        assert result.media.mirror_path is not None
        assert result.media.cid is not None

    async def test_an_exhausted_blob_budget_does_not_fail_the_upload(
        self, session: AsyncSession, session_factory: Any, mirror_root: Any
    ) -> None:
        settings = settings_for(mirror_root, media_blob_budget_bytes=1)
        uploader = await make_uploader(session)

        with pinata_ok():
            result = await upload_bytes(
                session, session_factory, JPEG_BYTES, uploader, settings
            )
        await session.commit()

        # A full database must not take uploads down with it; it costs the
        # durable copy, and that is said out loud rather than hidden.
        assert result.blob_stored is False
        assert result.media.blob is None

    async def test_a_successful_pin_consumes_the_pinata_budget(
        self, session: AsyncSession, session_factory: Any, mirror_root: Any
    ) -> None:
        settings = settings_for(mirror_root)
        uploader = await make_uploader(session)

        with pinata_ok():
            await upload_bytes(session, session_factory, JPEG_BYTES, uploader, settings)
        await session.commit()

        used = await service.pinata_quota(session_factory, settings).used()
        assert int(used) == len(JPEG_BYTES)

    async def test_a_failed_pin_consumes_nothing(
        self, session: AsyncSession, session_factory: Any, mirror_root: Any
    ) -> None:
        settings = settings_for(mirror_root)
        uploader = await make_uploader(session)

        with pinata_down():
            await upload_bytes(session, session_factory, JPEG_BYTES, uploader, settings)
        await session.commit()

        # Counting bytes the provider never accepted would exhaust the budget
        # against files that are not stored remotely at all.
        used = await service.pinata_quota(session_factory, settings).used()
        assert int(used) == 0


class TestResolution:
    async def test_all_three_tiers_are_offered_when_all_three_exist(
        self, session: AsyncSession, session_factory: Any, mirror_root: Any
    ) -> None:
        settings = settings_for(mirror_root)
        uploader = await make_uploader(session)

        with pinata_ok():
            result = await upload_bytes(
                session, session_factory, JPEG_BYTES, uploader, settings
            )
        await session.commit()

        resolved = resolve(result.media, settings, MirrorStore(settings))

        assert [option.tier for option in resolved.tiers] == [
            Tier.IPFS,
            Tier.MIRROR,
            Tier.BLOB,
        ]
        assert resolved.primary is not None
        assert resolved.primary.tier is Tier.IPFS
        # Only the database copy claims durability.
        assert [option.durable for option in resolved.tiers] == [False, False, True]

    async def test_an_unpinned_file_falls_back_to_mirror_then_blob(
        self, session: AsyncSession, session_factory: Any, mirror_root: Any
    ) -> None:
        settings = settings_for(mirror_root)
        uploader = await make_uploader(session)

        with pinata_down():
            result = await upload_bytes(
                session, session_factory, JPEG_BYTES, uploader, settings
            )
        await session.commit()

        resolved = resolve(result.media, settings, MirrorStore(settings))

        assert [option.tier for option in resolved.tiers] == [Tier.MIRROR, Tier.BLOB]

    async def test_a_wiped_mirror_leaves_only_the_blob(
        self, session: AsyncSession, session_factory: Any, mirror_root: Any
    ) -> None:
        import shutil

        settings = settings_for(mirror_root)
        uploader = await make_uploader(session)

        with pinata_down():
            result = await upload_bytes(
                session, session_factory, JPEG_BYTES, uploader, settings
            )
        await session.commit()

        # Exactly what a Render redeploy does to an ephemeral disk.
        shutil.rmtree(mirror_root)
        mirror_root.mkdir()

        resolved = resolve(result.media, settings, MirrorStore(settings))

        # The tier the phase prompt calls mandatory, being the reason the file
        # still exists at all.
        assert [option.tier for option in resolved.tiers] == [Tier.BLOB]
        data = service.read_bytes(result.media, MirrorStore(settings))
        assert data is not None
        assert data == (JPEG_BYTES, "BLOB")

    async def test_nothing_left_resolves_to_nothing(
        self, session: AsyncSession, session_factory: Any, mirror_root: Any
    ) -> None:
        import shutil

        settings = settings_for(mirror_root, media_blob_max_bytes=1)
        uploader = await make_uploader(session)

        with pinata_down():
            result = await upload_bytes(
                session, session_factory, JPEG_BYTES, uploader, settings
            )
        await session.commit()
        shutil.rmtree(mirror_root)
        mirror_root.mkdir()

        resolved = resolve(result.media, settings, MirrorStore(settings))

        # No tiers, reported honestly, rather than a URL that would 404.
        assert resolved.tiers == ()
        assert resolved.primary is None
        assert service.read_bytes(result.media, MirrorStore(settings)) is None


class TestMirrorIntegrity:
    async def test_a_mirrored_file_rehashes_to_its_own_name(
        self, session: AsyncSession, session_factory: Any, mirror_root: Any
    ) -> None:
        settings = settings_for(mirror_root)
        uploader = await make_uploader(session)

        with pinata_down():
            result = await upload_bytes(
                session, session_factory, JPEG_BYTES, uploader, settings
            )
        await session.commit()

        store = MirrorStore(settings)
        assert result.media.mirror_path is not None
        assert store.verify(result.media.mirror_path, result.media.sha256)

    async def test_a_corrupted_mirror_fails_verification(
        self, session: AsyncSession, session_factory: Any, mirror_root: Any
    ) -> None:
        settings = settings_for(mirror_root)
        uploader = await make_uploader(session)

        with pinata_down():
            result = await upload_bytes(
                session, session_factory, JPEG_BYTES, uploader, settings
            )
        await session.commit()

        store = MirrorStore(settings)
        assert result.media.mirror_path is not None
        (store.root / result.media.mirror_path).write_bytes(PNG_BYTES)

        # Wrong bytes under a correct digest is worse than a missing file.
        assert not store.verify(result.media.mirror_path, result.media.sha256)
