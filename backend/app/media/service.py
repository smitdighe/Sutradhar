"""The upload pipeline. The order of these steps is the design.

Read the numbered stages in :func:`ingest` as a sequence of refusals that get
progressively more expensive to reach. Everything cheap and local happens before
anything remote, and the integrity proof is committed before the least reliable
step is even attempted.

**1. Stream to disk with a hard ceiling.** An untrusted upload is never read
fully into memory. A client that promises 1 MB and sends 4 GB should cost this
process a bounded amount of disk and nothing else, and the ceiling is enforced
while reading rather than checked afterwards -- checking afterwards means the
4 GB is already in RAM.

**2. Sniff the type from the bytes.** ``Content-Type`` is a client assertion.
An executable renamed to ``.jpg`` arrives with ``image/jpeg`` on it if the
client says so, and a system that believes the header will happily store and
later serve it back. Magic numbers are the file's own claim about itself.

**3. Hash, and dedupe on the hash.** Content addressing makes deduplication
free: the same bytes uploaded twice are one row, and the second upload is a
lookup rather than a store.

**4. Check the budget before the network call.** Discovering a ceiling by
hitting it means the request has already spent the time and the provider has
already counted the bytes. Both budgets -- Pinata's and the database's -- are
checked while refusing is still free.

**5. Persist the SHA-256 before attempting to pin.** This is the load-bearing
one. The digest is what goes on chain and what proves the bytes were never
altered; the CID only says where a copy happens to live. A row with a digest and
no CID is a complete integrity record. A CID with no digest would be neither.

**6. A failed pin does not fail the upload.** Pinata being down is Pinata's
problem. The row lands ``PIN_PENDING``, a retry job is queued through the same
outbox that anchors hashes, and the weaver gets their 201.
"""

from __future__ import annotations

import hashlib
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chain.outbox import enqueue_job
from app.config import Settings, get_settings
from app.core.errors import (
    ConflictError,
    ErrorCode,
    ForbiddenError,
    InsufficientStorageError,
    NotFoundError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.quota import QuotaTracker
from app.db.models.catalog import Item
from app.db.models.enums import MediaKind, OutboxJobType, PinStatus, UserRole
from app.db.models.media import ItemMedia, Media
from app.db.models.user import User
from app.media.mirror import MirrorStore
from app.media.pinata import PinataClient, PinataError

__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "PINATA_QUOTA",
    "BLOB_QUOTA",
    "IngestResult",
    "attach_media",
    "blob_quota",
    "detach_media",
    "ingest",
    "load_media",
    "pinata_quota",
    "sniff_content_type",
]

logger = get_logger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]

PINATA_QUOTA = "pinata_storage_bytes"
BLOB_QUOTA = "media_blob_bytes"

# Read in 64 KiB chunks: large enough that the syscall overhead disappears,
# small enough that the ceiling is enforced long before memory is a concern.
CHUNK_BYTES = 65_536


# --------------------------------------------------------------- byte sniffing

# (offset, magic bytes, content type). Checked in order; the first match wins.
# Hand-rolled rather than pulled from a dependency: it is a dozen constants, and
# the allowlist being readable in one place is worth more here than the breadth
# a general-purpose library would add. Breadth is the opposite of what an
# allowlist wants.
_SIGNATURES: tuple[tuple[int, bytes, str], ...] = (
    (0, b"\xff\xd8\xff", "image/jpeg"),
    (0, b"\x89PNG\r\n\x1a\n", "image/png"),
)

# Container formats whose magic sits behind a length prefix or a brand.
_RIFF = b"RIFF"
_WEBP = b"WEBP"
_FTYP = b"ftyp"
# MP4 brands that mean "this is the MP4 family". A brand list rather than "any
# ftyp": ftyp also fronts HEIF, AVIF and QuickTime, which are not on the
# allowlist and must not be admitted by accident.
_MP4_BRANDS = frozenset(
    {b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"mmp4", b"dash"}
)

ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/webp", "video/mp4"}
)

# How many leading bytes the sniffer needs. The furthest thing it reads is the
# MP4 brand at offset 8.
SNIFF_BYTES = 32


def sniff_content_type(head: bytes) -> str | None:
    """Identify a file from its leading bytes, or ``None`` if unrecognised.

    Never consults a filename or a client-supplied header. The whole point is
    that neither of those is evidence: an attacker controls both.
    """
    for offset, magic, content_type in _SIGNATURES:
        if head[offset : offset + len(magic)] == magic:
            return content_type

    # WebP: 'RIFF' <4-byte little-endian size> 'WEBP'
    if head[0:4] == _RIFF and head[8:12] == _WEBP:
        return "image/webp"

    # MP4: <4-byte box size> 'ftyp' <4-byte major brand>
    if head[4:8] == _FTYP and head[8:12] in _MP4_BRANDS:
        return "video/mp4"

    return None


# ------------------------------------------------------------------- quotas


def pinata_quota(session_factory: SessionFactory, settings: Settings) -> QuotaTracker:
    """Bytes pinned to IPFS. Cumulative -- pinned bytes do not expire."""
    return QuotaTracker(
        name=PINATA_QUOTA,
        budget=settings.pinata_storage_budget_bytes,
        session_factory=session_factory,
        periodic=False,
    )


def blob_quota(session_factory: SessionFactory, settings: Settings) -> QuotaTracker:
    """Bytes stored inline in Postgres. Cumulative, and the one that bites first.

    A full database refuses *every* write, not just uploads -- registration,
    attestation, the outbox. So this budget is deliberately far below the
    Pinata one, and crossing it costs the blob copy rather than the upload.
    """
    return QuotaTracker(
        name=BLOB_QUOTA,
        budget=settings.media_blob_budget_bytes,
        session_factory=session_factory,
        periodic=False,
    )


# ------------------------------------------------------------------- ingest


@dataclass(frozen=True, slots=True)
class IngestResult:
    """One upload's outcome."""

    media: Media
    deduplicated: bool
    pinned: bool
    blob_stored: bool
    pin_error: str | None = None


@dataclass(frozen=True, slots=True)
class _Staged:
    """An upload that survived streaming: on disk, sized, typed and hashed."""

    path: Path
    byte_size: int
    sha256: str
    content_type: str


def _stage(upload: BinaryIO, limit: int) -> _Staged:
    """Stream to a temp file under a hard ceiling, sniffing and hashing as it goes.

    One pass. The digest and the type both come out of the same read, so a file
    cannot be typed from bytes that differ from the bytes that were hashed.
    """
    digest = hashlib.sha256()
    size = 0
    head = b""

    # mkstemp rather than NamedTemporaryFile: the file has to outlive the block
    # that writes it -- the caller reads it back after staging -- and the
    # `finally` in `ingest` is what removes it. A context manager that deleted
    # on close would take the staged upload with it.
    descriptor, name = tempfile.mkstemp(suffix=".upload")
    path = Path(name)
    try:
        with open(descriptor, "wb") as handle:
            while True:
                chunk = upload.read(CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    # Refused mid-stream, not after buffering. The client does
                    # not get to decide how much memory this costs.
                    raise ValidationError(
                        code=ErrorCode.MEDIA_TOO_LARGE,
                        status=413,
                        message=f"file exceeds the {limit} byte limit",
                        details={"limit_bytes": limit},
                    )
                if len(head) < SNIFF_BYTES:
                    head += chunk[: SNIFF_BYTES - len(head)]
                digest.update(chunk)
                handle.write(chunk)

        if size == 0:
            raise ValidationError(
                code=ErrorCode.VALIDATION_FAILED, status=422, message="the file is empty"
            )

        content_type = sniff_content_type(head)
        if content_type is None or content_type not in ALLOWED_CONTENT_TYPES:
            # The bytes disagree with whatever the client called this, or the
            # bytes are of a type nobody asked to accept.
            raise ValidationError(
                code=ErrorCode.UNSUPPORTED_MEDIA_TYPE,
                status=415,
                message="file type not accepted; the bytes are not jpeg, png, webp or mp4",
                details={"allowed": sorted(ALLOWED_CONTENT_TYPES)},
            )

        return _Staged(
            path=path, byte_size=size, sha256=digest.hexdigest(), content_type=content_type
        )
    except BaseException:
        path.unlink(missing_ok=True)
        raise


async def ingest(
    session: AsyncSession,
    session_factory: SessionFactory,
    upload: BinaryIO,
    uploader: User,
    settings: Settings | None = None,
    pinata: PinataClient | None = None,
    store: MirrorStore | None = None,
) -> IngestResult:
    """Run the whole pipeline. Caller commits.

    See the module docstring for why the steps are in this order.
    """
    resolved = settings or get_settings()
    client = pinata or PinataClient(resolved)
    mirror = store or MirrorStore(resolved)

    # 1-3. Stream, bound, sniff, hash.
    staged = _stage(upload, resolved.media_max_bytes)

    try:
        # 4. Content addressing gives deduplication for nothing. The same bytes
        #    uploaded twice are one row, and the second upload never reaches a
        #    budget check or a network call.
        existing = (
            await session.execute(select(Media).where(Media.sha256 == staged.sha256))
        ).scalar_one_or_none()
        if existing is not None:
            logger.info(
                "media.deduplicated", media_id=str(existing.id), sha256=staged.sha256
            )
            return IngestResult(
                media=existing,
                deduplicated=True,
                pinned=existing.pin_status is PinStatus.PINNED,
                blob_stored=existing.blob is not None,
            )

        # 5. Budgets, before anything is spent. Refusing is free right up until
        #    the network call, and never after it.
        pinata_budget = pinata_quota(session_factory, resolved)
        if client.enabled and await pinata_budget.would_exceed(staged.byte_size):
            remaining = await pinata_budget.remaining()
            raise InsufficientStorageError(
                message=(
                    "the IPFS pinning storage budget is exhausted; "
                    "no further uploads can be pinned until space is freed"
                ),
                details={
                    "budget_bytes": resolved.pinata_storage_budget_bytes,
                    "remaining_bytes": int(remaining),
                    "file_bytes": staged.byte_size,
                },
            )

        data = staged.path.read_bytes()

        # 6. Mirror first: a local write that cannot fail for anyone else's
        #    reasons, so the bytes exist somewhere before anything remote runs.
        mirror_path = mirror.write(staged.sha256, data)

        # The database copy is the only durable one, so it is also the only one
        # that can fill a database. Bounded twice: per file, and in aggregate.
        blob_budget = blob_quota(session_factory, resolved)
        store_blob = staged.byte_size <= resolved.media_blob_max_bytes and not (
            await blob_budget.would_exceed(staged.byte_size)
        )
        if not store_blob:
            logger.warning(
                "media.blob.skipped",
                sha256=staged.sha256,
                byte_size=staged.byte_size,
                inline_limit=resolved.media_blob_max_bytes,
                consequence="this file survives only while the mirror or the pin does",
            )

        media = Media(
            sha256=staged.sha256,
            byte_size=staged.byte_size,
            content_type=staged.content_type,
            mirror_path=mirror_path,
            blob=data if store_blob else None,
            pin_status=PinStatus.PIN_PENDING,
            uploaded_by=uploader.id,
        )
        session.add(media)
        # 7. The integrity proof is committed to this transaction before a pin
        #    is attempted. A row with a digest and no CID still proves the bytes
        #    were never altered; that is the whole guarantee.
        await session.flush()

        if store_blob:
            await blob_budget.consume(staged.byte_size)

        # 8. The least reliable step, last, and unable to fail the upload.
        pinned, pin_error = await _try_pin(client, media, data, pinata_budget)

        if not pinned:
            # Queued through the same outbox that anchors hashes: one retry and
            # dead-letter mechanism, not two that drift apart.
            await enqueue_job(
                session,
                job_type=OutboxJobType.PIN_MEDIA,
                payload={"media_id": str(media.id), "sha256": media.sha256},
                dedupe_key=f"pin:{media.sha256}",
            )

        return IngestResult(
            media=media,
            deduplicated=False,
            pinned=pinned,
            blob_stored=store_blob,
            pin_error=pin_error,
        )
    finally:
        staged.path.unlink(missing_ok=True)


async def _try_pin(
    client: PinataClient,
    media: Media,
    data: bytes,
    budget: QuotaTracker,
) -> tuple[bool, str | None]:
    """Attempt the pin. Never raises; a failure is a state, not an exception."""
    if not client.enabled:
        return False, "pinning is disabled (PINATA_JWT unset)"

    try:
        result = await client.pin(data, f"{media.sha256}", media.content_type)
    except PinataError as exc:
        logger.warning(
            "media.pin.deferred",
            sha256=media.sha256,
            error=str(exc),
            consequence="upload still succeeds; the retry job will try again",
        )
        return False, str(exc)

    media.cid = result.cid
    media.pin_status = PinStatus.PINNED
    # Consumed only on success. Counting bytes a provider never accepted would
    # exhaust the budget against uploads that are not stored anywhere remote.
    await budget.consume(media.byte_size)
    return True, None


# ------------------------------------------------------------------ linkage


async def load_media(session: AsyncSession, media_id: uuid.UUID) -> Media:
    media = await session.get(Media, media_id)
    if media is None:
        raise NotFoundError(code=ErrorCode.NOT_FOUND, message=f"no media with id {media_id}")
    return media


async def _load_item_for_write(session: AsyncSession, item_id: uuid.UUID, actor: User) -> Item:
    """Load an item the actor is allowed to change the media of."""
    item = await session.get(Item, item_id)
    if item is None:
        raise NotFoundError(
            code=ErrorCode.ITEM_NOT_FOUND, message=f"no item with id {item_id}"
        )
    if item.registered_by != actor.id and actor.role is not UserRole.ADMIN:
        raise ForbiddenError(
            code=ErrorCode.FORBIDDEN,
            message="only the item's registrant may change its media",
        )
    return item


async def attach_media(
    session: AsyncSession,
    item_id: uuid.UUID,
    media_id: uuid.UUID,
    kind: MediaKind,
    actor: User,
) -> ItemMedia:
    """Link existing media to an item. Caller commits."""
    item = await _load_item_for_write(session, item_id, actor)
    media = await load_media(session, media_id)

    existing = await session.get(ItemMedia, {"item_id": item.id, "media_id": media.id})
    if existing is not None:
        raise ConflictError(
            code=ErrorCode.CONFLICT,
            message="this media is already linked to this item",
        )

    link = ItemMedia(item_id=item.id, media_id=media.id, kind=kind)
    session.add(link)
    await session.flush()
    logger.info(
        "media.attached", item_id=str(item.id), media_id=str(media.id), kind=str(kind)
    )
    return link


async def detach_media(
    session: AsyncSession, item_id: uuid.UUID, media_id: uuid.UUID, actor: User
) -> bool:
    """Unlink media from an item. The media row itself is never deleted.

    The SHA-256 may already be anchored on chain, and a hash pointing at bytes
    this system threw away is exactly the dead reference the three-tier design
    exists to prevent. Unlinking says "this file no longer depicts this item";
    it does not say the file never existed.
    """
    await _load_item_for_write(session, item_id, actor)

    link = await session.get(ItemMedia, {"item_id": item_id, "media_id": media_id})
    if link is None:
        return False

    await session.delete(link)
    await session.flush()
    logger.info(
        "media.detached",
        item_id=str(item_id),
        media_id=str(media_id),
        note="link removed; the media row and its bytes are retained",
    )
    return True


async def list_for_item(
    session: AsyncSession, item_id: uuid.UUID
) -> list[tuple[Media, MediaKind]]:
    """Every piece of media linked to an item, with what it depicts."""
    rows = (
        await session.execute(
            select(Media, ItemMedia.kind)
            .join(ItemMedia, ItemMedia.media_id == Media.id)
            .where(ItemMedia.item_id == item_id)
            .order_by(Media.created_at)
        )
    ).all()
    return [(media, kind) for media, kind in rows]


def read_bytes(
    media: Media, store: MirrorStore, prefer: str | None = None
) -> tuple[bytes, str] | None:
    """Best available bytes for a media row, and which tier served them.

    Falls through the tiers in order rather than trusting any one of them: the
    mirror can have been wiped by a redeploy since the row was written, and a
    caller asking for a specific tier gets it only if it is actually there.
    """
    order = ["MIRROR", "BLOB"]
    if prefer in order:
        order.remove(prefer)
        order.insert(0, prefer)

    for tier in order:
        if tier == "MIRROR":
            data = store.read(media.mirror_path)
            if data is not None:
                return data, "MIRROR"
        elif tier == "BLOB" and media.blob is not None:
            return bytes(media.blob), "BLOB"

    return None
