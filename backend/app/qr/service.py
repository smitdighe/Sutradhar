"""Tag issuance and QR rendering.

**Codes are issued by this service, never pre-printed.** A pre-printed range is
a list of valid codes sitting in a print shop: whoever holds it can put a
plausible tag on anything. Here a code exists only once a row in ``items``
claims it, so a label with no matching record resolves to nothing.

**A tag binding is a Postgres write and nothing else.** It is not anchored, and
it does not go through the ``outbox``. The item hash is what the chain commits
to and it does not include the tag code -- deliberately, because a bolt can be
re-tagged after a printer eats a label without that invalidating an anchor.

**The QR payload is a URL and nothing else**: ``{PUBLIC_BASE_URL}/v/{CODE}``.
No token, no signature, no query string. A QR code is a printed number; anything
secret inside one is public the moment it goes on fabric. ``PUBLIC_BASE_URL`` is
the *frontend* origin, so a scan does not depend on this process being awake.

Generation itself lives in :mod:`app.core.ids` and is not duplicated here.
"""

from __future__ import annotations

import io
import uuid
from dataclasses import dataclass, field
from typing import Any

import qrcode
from PIL import Image
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.auth.roles import Role
from app.config import get_settings
from app.core.errors import (
    AppError,
    ConflictError,
    ErrorCode,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from app.core.hashing import hash_object
from app.core.ids import TAG_CODE_LENGTH, new_tag_code, normalize_tag_code, validate_tag_code
from app.db.models.catalog import Item, ItemEvent
from app.db.models.enums import ItemEventType, ItemStatus
from app.db.models.user import User
from app.provenance.service import get_item

__all__ = [
    "BULK_MAX_ITEMS",
    "DEFAULT_PNG_SIZE",
    "MAX_ISSUE_ATTEMPTS",
    "MAX_PNG_SIZE",
    "MIN_PNG_SIZE",
    "QUIET_ZONE_MODULES",
    "BulkResult",
    "IssuedTag",
    "assign_tag_code",
    "bind_tag",
    "bulk_issue_tags",
    "format_tag_code",
    "issue_tag",
    "lookup_by_tag_code",
    "render_png",
    "render_svg",
    "tag_url",
]

# Five is a ceiling on a thing that cannot happen. 11 random symbols over a
# 29-symbol alphabet is ~53 bits, so at demo scale a collision is a rounding
# error away from impossible -- but "impossible" is a claim about a random
# number generator, and the one failure mode of a broken generator is that it
# stops being random. Retrying costs nothing and turns a silent duplicate tag
# into a loud 500.
MAX_ISSUE_ATTEMPTS = 5

# Grouping is display-only. Everything stored, compared or looked up is the
# bare 12 characters; the separators exist so a person reading a label out loud
# does not lose their place.
TAG_GROUP_SIZE = 4

# Error correction Q recovers 25% of the symbol. A tag on fabric gets creased,
# folded, rubbed against a bag and rained on; M (15%) is the usual default and
# is chosen for screens, which do none of those things.
ERROR_CORRECTION = qrcode.constants.ERROR_CORRECT_Q

# The white margin, measured in modules. Four is the spec minimum and scanners
# do fail without it -- a QR printed flush against dark fabric is a QR nobody
# can read.
QUIET_ZONE_MODULES = 4

DEFAULT_PNG_SIZE = 512
# Floor is above any module count this payload can produce, so one module is
# never forced below a whole pixel. Ceiling keeps a URL parameter from asking
# for a 100-megapixel bitmap.
MIN_PNG_SIZE = 128
MAX_PNG_SIZE = 2_048

# A sheet of labels for a co-op's morning output. Past this the request stops
# being a print run and starts being a way to hold a transaction open.
BULK_MAX_ITEMS = 500

_DARK = "#000000"
_LIGHT = "#ffffff"


# ---------------------------------------------------------------- formatting


def format_tag_code(code: str) -> str:
    """Group a stored code in fours for printing: ``X7K29M4P3RQ8`` -> ``X7K2-9M4P-3RQ8``."""
    canonical = normalize_tag_code(code)
    return "-".join(
        canonical[start : start + TAG_GROUP_SIZE]
        for start in range(0, len(canonical), TAG_GROUP_SIZE)
    )


def tag_url(code: str) -> str:
    """The exact string encoded in the QR. Nothing is appended to it, ever.

    The path is ``/v/`` rather than ``/verify/`` because every character is
    modules, and fewer modules means a coarser grid that survives a crease.
    """
    return f"{get_settings().public_base_url}/v/{normalize_tag_code(code)}"


# ---------------------------------------------------------------- rendering


def _matrix(code: str) -> list[list[bool]]:
    """Module grid for a tag's URL, quiet zone included."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECTION,
        box_size=1,
        border=QUIET_ZONE_MODULES,
    )
    qr.add_data(tag_url(code))
    # fit=True picks the smallest version that holds the payload. The payload is
    # fixed-length, so in practice every tag in a print run is the same version.
    qr.make(fit=True)
    return [[bool(cell) for cell in row] for row in qr.get_matrix()]


def clamp_png_size(size: int) -> int:
    """Bound a requested pixel size to something printable and renderable."""
    return max(MIN_PNG_SIZE, min(MAX_PNG_SIZE, size))


def render_png(code: str, size: int = DEFAULT_PNG_SIZE) -> bytes:
    """Render *code* as a square PNG of exactly *size* pixels.

    Modules are scaled by a whole number of pixels and the remainder is added to
    the margin, rather than resampling the grid to fit. A half-pixel module edge
    is how a code that decodes on a screen fails on paper.
    """
    bounded = clamp_png_size(size)
    matrix = _matrix(code)
    modules = len(matrix)

    scale = max(1, bounded // modules)
    drawn = modules * scale

    grid = Image.new("1", (modules, modules))
    grid.putdata([0 if dark else 255 for row in matrix for dark in row])
    # An integer scale factor, so NEAREST reproduces each module as an exact
    # square block rather than resampling module edges into grey.
    image = grid.resize((drawn, drawn), Image.Resampling.NEAREST)

    if drawn != bounded:
        # Centre the code on a white field. The leftover pixels become extra
        # quiet zone, which no scanner has ever objected to.
        canvas = Image.new("1", (bounded, bounded), 1)
        offset = (bounded - drawn) // 2
        canvas.paste(image, (offset, offset))
        image = canvas

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def render_svg(code: str, size: int = DEFAULT_PNG_SIZE) -> str:
    """Render *code* as an SVG.

    Built here rather than taken from ``qrcode``'s SVG factories so the output
    carries a ``viewBox`` in module units: the drawing is resolution-free and a
    print shop can set it to any physical size without resampling anything.

    Contains the tag code and nothing else. No identifiers, no names, no
    metadata -- the file is destined for a printer that belongs to somebody
    else.
    """
    matrix = _matrix(code)
    modules = len(matrix)
    bounded = clamp_png_size(size)

    segments: list[str] = []
    for row_index, row in enumerate(matrix):
        column_index = 0
        while column_index < modules:
            if not row[column_index]:
                column_index += 1
                continue
            run_start = column_index
            while column_index < modules and row[column_index]:
                column_index += 1
            run = column_index - run_start
            # One horizontal bar per run of dark modules: far fewer path
            # commands than one rect per module, and identical output.
            segments.append(f"M{run_start} {row_index}h{run}v1h-{run}z")

    label = f"QR code for tag {format_tag_code(code)}"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{bounded}" height="{bounded}" '
        f'viewBox="0 0 {modules} {modules}" shape-rendering="crispEdges" '
        f'role="img" aria-label="{label}">'
        f'<rect width="{modules}" height="{modules}" fill="{_LIGHT}"/>'
        f'<path fill="{_DARK}" d="{"".join(segments)}"/>'
        "</svg>"
    )


# ---------------------------------------------------------------- issuance


@dataclass(frozen=True, slots=True)
class IssuedTag:
    """One tag binding, plus anything the caller should be told about it."""

    item_id: uuid.UUID
    tag_code: str
    display_code: str
    payload_url: str
    # True when this call created the binding, False when it already existed.
    # The 409 path carries the existing code and uses this to say so.
    newly_issued: bool
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class BulkResult:
    """Per-item outcome in a batch. Partial success is success."""

    item_id: uuid.UUID
    # "issued" | "already_tagged" | "failed"
    outcome: str
    tag_code: str | None = None
    display_code: str | None = None
    payload_url: str | None = None
    reason_code: str | None = None
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)


async def _has_children(session: AsyncSession, item_id: uuid.UUID) -> bool:
    count = (
        await session.execute(
            select(func.count()).select_from(Item).where(Item.parent_id == item_id)
        )
    ).scalar_one()
    return bool(count)


def _split_warning(item_id: uuid.UUID) -> str:
    return (
        f"item {item_id} has been split into child items: a tag belongs on the smallest "
        "sellable unit, and one tag covering several pieces is the substitution path this "
        "system exists to close. Tag the children instead unless this item is sold whole."
    )


def _assert_may_tag(item: Item, actor: User) -> None:
    """Role and ownership. A weaver tags their own work, nobody else's."""
    if actor.role is Role.WEAVER and item.registered_by != actor.id:
        raise ForbiddenError(
            code=ErrorCode.FORBIDDEN,
            message="you may only issue tags for items you registered",
        )


def _assert_issuable(item: Item) -> None:
    """A record whose anchor failed does not get a physical label.

    ``FAILED`` means the hash never made it on chain. Printing a tag for it
    would put a scannable claim of provenance on an object whose provenance was
    never recorded -- the one thing a tag must never be able to do.
    """
    if item.status is ItemStatus.FAILED:
        raise ValidationError(
            code=ErrorCode.TAG_NOT_ISSUABLE,
            status=422,
            message=(
                "this item's anchoring failed, so it has no recorded provenance to tag. "
                "Resolve the anchoring failure first."
            ),
            details={"item_id": str(item.id), "status": str(item.status)},
        )


def _already_issued(item_id: uuid.UUID, code: str) -> ConflictError:
    """The 409 a caller gets when the item already wears a tag.

    Carries the existing code because the caller's next move is to print it,
    not to retry.
    """
    return ConflictError(
        code=ErrorCode.TAG_ALREADY_ISSUED,
        message="this item already carries a tag",
        details={
            "item_id": str(item_id),
            "tag_code": code,
            "display_code": format_tag_code(code),
            "payload_url": tag_url(code),
        },
    )


async def assign_tag_code(session: AsyncSession, item: Item) -> str:
    """Generate and bind a code, retrying past a unique violation.

    **The binding is a conditional UPDATE, not an assignment.**
    ``WHERE tag_code IS NULL`` is what makes one item wear one tag: two
    simultaneous issuance requests both read the item as untagged -- there is
    no read that could have told them otherwise -- and without the predicate
    the second ``UPDATE`` would simply overwrite the first, minting a second
    code and orphaning a label that may already be on cloth. With it, exactly
    one request updates a row; the other updates none and is told the item is
    already tagged. The database decides, the same way ``claims.item_id`` and
    ``uq_attestations_item_attestor`` decide elsewhere in this system.

    Each attempt runs in its own SAVEPOINT. A duplicate key aborts the
    transaction it happens in, so without the savepoint the first collision
    would poison the caller's whole transaction -- including, in a bulk run,
    every item after it. That retry is for *generator* collisions, which are a
    different failure from contention on the item and stay separate from it.
    """
    for _ in range(MAX_ISSUE_ATTEMPTS):
        candidate = new_tag_code()
        try:
            async with session.begin_nested():
                bound = (
                    await session.execute(
                        update(Item)
                        .where(Item.id == item.id, Item.tag_code.is_(None))
                        .values(tag_code=candidate)
                        .returning(Item.tag_code)
                        # The in-session instance is put back in step below,
                        # without the extra SELECT a synchronised update costs.
                        .execution_options(synchronize_session=False)
                    )
                ).scalar_one_or_none()
        except IntegrityError:
            # The code collided with another item's. Nothing is wrong with the
            # request; try another code.
            continue

        if bound is None:
            # Zero rows updated: this item already had a code when the UPDATE
            # ran. One extra read, only on the losing path, to name it.
            existing = (
                await session.execute(select(Item.tag_code).where(Item.id == item.id))
            ).scalar_one_or_none()
            raise _already_issued(item.id, existing or "")

        # Not an assignment: `set_committed_value` marks the attribute as
        # loaded-and-clean, so the object matches the row without becoming
        # dirty and emitting a second UPDATE at the next flush.
        set_committed_value(item, "tag_code", bound)
        return bound

    raise AppError(
        code=ErrorCode.TAG_GENERATION_EXHAUSTED,
        status=500,
        message=(
            f"could not generate an unused tag code in {MAX_ISSUE_ATTEMPTS} attempts; "
            "this is a fault in the code generator, not in your request"
        ),
    )


async def issue_tag(session: AsyncSession, item_id: uuid.UUID, actor: User) -> IssuedTag:
    """Bind a freshly generated tag code to one item. Caller commits.

    Raises 409 when the item already carries a tag; the error details carry the
    existing code, because the caller's next move is to print it, not to retry.

    This check is the courteous path, not the enforcement. It reads state that
    a concurrent request may be about to change, so it cannot be what decides;
    :func:`assign_tag_code` raises the same 409 from a conditional UPDATE when
    two requests reach here together.
    """
    item = await get_item(session, item_id)
    _assert_may_tag(item, actor)
    _assert_issuable(item)

    if item.tag_code is not None:
        raise _already_issued(item.id, item.tag_code)

    warnings = [_split_warning(item.id)] if await _has_children(session, item_id) else []
    return await bind_tag(session, item, actor, warnings=warnings)


async def bind_tag(
    session: AsyncSession, item: Item, actor: User, warnings: list[str] | None = None
) -> IssuedTag:
    """Assign a code to an already-loaded, already-checked item and log it.

    Split out from :func:`issue_tag` so the seed loader binds tags through the
    same code path the API uses, rather than growing a second one that could
    drift -- a seeded tag that skipped the event log would be a tag with no
    issuance history, which is a state the application can never produce.
    """
    advisories = list(warnings or [])
    code = await assign_tag_code(session, item)

    payload: dict[str, Any] = {
        "tag_code": code,
        "payload_url": tag_url(code),
        "issued_by_role": str(actor.role),
        # Recorded because a tag issued on a parent is the state an auditor
        # would otherwise have to reconstruct from timestamps.
        "had_children": bool(advisories),
    }
    session.add(
        ItemEvent(
            item_id=item.id,
            event_type=ItemEventType.TAG_ISSUED,
            actor_id=actor.id,
            payload=payload,
            payload_hash=hash_object(payload),
        )
    )
    await session.flush()

    return IssuedTag(
        item_id=item.id,
        tag_code=code,
        display_code=format_tag_code(code),
        payload_url=tag_url(code),
        newly_issued=True,
        warnings=advisories,
    )


async def bulk_issue_tags(
    session: AsyncSession, item_ids: list[uuid.UUID], actor: User
) -> list[BulkResult]:
    """Issue tags for many items, reporting each one separately. Caller commits.

    One ineligible item does not fail the batch. An operator preparing a
    morning's labels wants the 47 that worked and a note about the three that
    did not, not a rejection of all fifty.

    The size ceiling is enforced by the caller *before* anything is written, so
    an oversized request issues nothing at all.
    """
    results: list[BulkResult] = []
    # Preserve caller order, drop repeats: the same id twice is one item, and
    # the second pass would otherwise report a spurious "already tagged".
    seen: set[uuid.UUID] = set()

    for item_id in item_ids:
        if item_id in seen:
            continue
        seen.add(item_id)

        try:
            issued = await issue_tag(session, item_id, actor)
        except ConflictError as exc:
            details = exc.details or {}
            results.append(
                BulkResult(
                    item_id=item_id,
                    outcome="already_tagged",
                    tag_code=str(details.get("tag_code")) if details.get("tag_code") else None,
                    display_code=(
                        str(details.get("display_code")) if details.get("display_code") else None
                    ),
                    payload_url=(
                        str(details.get("payload_url")) if details.get("payload_url") else None
                    ),
                    reason_code=str(exc.code),
                    reason=exc.message,
                )
            )
        except AppError as exc:
            results.append(
                BulkResult(
                    item_id=item_id,
                    outcome="failed",
                    reason_code=str(exc.code),
                    reason=exc.message,
                )
            )
        else:
            results.append(
                BulkResult(
                    item_id=item_id,
                    outcome="issued",
                    tag_code=issued.tag_code,
                    display_code=issued.display_code,
                    payload_url=issued.payload_url,
                    warnings=issued.warnings,
                )
            )

    return results


# ---------------------------------------------------------------- lookup


async def lookup_by_tag_code(session: AsyncSession, code: str) -> Item:
    """Resolve any human rendering of a tag code to its item.

    Every lookup normalises first: uppercased, separators stripped, and the
    characters the alphabet excludes folded onto what the reader meant. The
    checksum is verified before the query, so a mistyped code is told it is
    mistyped rather than reported as an unknown tag -- those are different
    problems and only one of them means the textile is not in the system.
    """
    canonical = normalize_tag_code(code)
    if not validate_tag_code(canonical):
        raise ValidationError(
            code=ErrorCode.INVALID_TAG_CODE,
            status=422,
            message=(
                f"'{code}' is not a valid tag code: it must be {TAG_CODE_LENGTH} characters "
                "and pass its check symbol"
            ),
        )

    item = (
        await session.execute(select(Item).where(Item.tag_code == canonical))
    ).scalar_one_or_none()
    if item is None:
        raise NotFoundError(
            code=ErrorCode.ITEM_NOT_FOUND,
            message="no item carries that tag code",
            details={"tag_code": canonical},
        )
    return item
