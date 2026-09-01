"""Idempotent seed loading.

There is no dry-run branch in here on purpose. ``bootstrap_db.py --dry-run``
runs these functions for real inside a transaction and rolls back, so the dry
run exercises the same code as the real thing and reports what would actually
happen -- a parallel "pretend" path would be the one place a seeding bug could
hide from its own preview.

Every loader here upserts on a natural key -- category slug + schema version,
user email, item key -- so running the bootstrap twice is a no-op rather than a
unique-violation crash. That property is what makes the script safe to run
against a database somebody is already using, which on demo day is the only
kind there is.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.password import hash_password
from app.core.clock import now
from app.core.crypto_shred import new_salt
from app.core.hashing import hash_object, sha256_hex
from app.core.ids import new_uuid
from app.db.models.attestation import Attestation
from app.db.models.catalog import GICategory, Item, ItemEvent
from app.db.models.enums import (
    ItemEventType,
    ItemStatus,
    SuspicionLevel,
    UserRole,
    UserStatus,
)
from app.db.models.scan import Scan
from app.db.models.user import User
from app.provenance.item_hash import hash_item, registrant_hash
from app.qr.service import bind_tag
from seeds import SEEDS_DIR, seed_password
from seeds.hashing import SEED_HASH_VERSION, seed_statement_hash

__all__ = [
    "SeedReport",
    "load_categories",
    "load_items",
    "load_reputation",
    "load_users",
]


@dataclass
class SeedReport:
    """What one loader did, for the summary table."""

    label: str
    created: int = 0
    existed: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.created + self.existed + self.skipped


def _read(name: str) -> Any:
    return json.loads((SEEDS_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- categories


async def load_categories(session: AsyncSession) -> SeedReport:
    """Upsert every category in ``seeds/categories/``, keyed on (slug, version)."""
    report = SeedReport(label="categories")

    for path in sorted((SEEDS_DIR / "categories").glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        schema = doc["attribute_schema"]

        # A malformed schema would be accepted by JSONB and only explode later,
        # at item registration, a long way from the file that caused it.
        Draft202012Validator.check_schema(schema)

        existing = (
            await session.execute(
                select(GICategory).where(
                    GICategory.slug == doc["slug"],
                    GICategory.schema_version == doc["schema_version"],
                )
            )
        ).scalar_one_or_none()

        if existing is not None:
            report.existed += 1
            continue
        session.add(
            GICategory(
                slug=doc["slug"],
                display_name=doc["display_name"],
                is_textile=doc["is_textile"],
                attribute_schema=schema,
                schema_version=doc["schema_version"],
                quantity_unit=doc["quantity_unit"],
                is_active=True,
            )
        )
        report.created += 1

    await session.flush()
    return report


# ---------------------------------------------------------------- users


async def load_users(session: AsyncSession) -> SeedReport:
    """Upsert seeded users, keyed on email. Never touches an existing row."""
    report = SeedReport(label="users")
    # Hashed once: argon2 at the configured cost is ~100ms, and seven identical
    # hashes of the same password buy nothing.
    shared_hash = hash_password(seed_password())

    for spec in _read("users.json")["users"]:
        email = spec["email"]
        existing = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()

        if existing is not None:
            report.existed += 1
            continue
        user = User(
            email=email,
            password_hash=shared_hash,
            display_name=spec["display_name"],
            role=UserRole(spec["role"]),
            status=UserStatus(spec["status"]),
            region=spec.get("region"),
            org_name=spec.get("org_name"),
            identity_salt=new_salt(),
        )
        # Deliberately NOT flagged here, even for the weaver the seed file marks
        # as fraud_flagged. Users load before items, so a flag applied now would
        # find nothing to dispute and leave the actor flagged with every one of
        # their records still reading clean -- a state the application itself can
        # never produce. `load_reputation` applies it after the items exist, by
        # the same code path an admin uses.
        session.add(user)
        report.created += 1

    await session.flush()
    return report


# ---------------------------------------------------------------- items


async def _user_index(session: AsyncSession) -> dict[str, User]:
    """Map the seed file's ``key`` onto the loaded User rows."""
    by_email = {
        user.email.lower(): user
        for user in (await session.execute(select(User))).scalars().all()
    }
    return {
        spec["key"]: by_email[spec["email"].lower()]
        for spec in _read("users.json")["users"]
        if spec["email"].lower() in by_email
    }


async def load_items(session: AsyncSession) -> SeedReport:
    """Load the item tree, its attestations, and its scan history.

    Idempotent on the seed ``key``, which is recorded in each item's REGISTERED
    event payload -- the items table has no natural key of its own, and matching
    on a hash would break the moment Phase 6 changes the payload shape.
    """
    report = SeedReport(label="items")
    document = _read("items.json")

    users = await _user_index(session)
    categories = {
        category.slug: category
        for category in (await session.execute(select(GICategory))).scalars().all()
    }

    already = {
        str(row.payload.get("seed_key"))
        for row in (
            await session.execute(
                select(ItemEvent).where(ItemEvent.event_type == ItemEventType.REGISTERED)
            )
        )
        .scalars()
        .all()
        if isinstance(row.payload, dict) and row.payload.get("seed_key")
    }

    created_items: dict[str, Item] = {}

    for spec in document["items"]:
        key = spec["key"]
        if key in already:
            report.existed += 1
            continue

        category = categories.get(spec["category"])
        if category is None:
            report.skipped += 1
            report.notes.append(f"{key}: category {spec['category']} not loaded")
            continue

        registrant = users.get(spec["registered_by"])
        if registrant is None:
            report.skipped += 1
            report.notes.append(f"{key}: user {spec['registered_by']} not loaded")
            continue

        # Attributes are validated against the category's own schema, so a seed
        # file that drifts from its schema fails here rather than shipping a row
        # the API would reject.
        Draft202012Validator(category.attribute_schema).validate(spec["attributes"])

        parent = created_items.get(spec["parent"]) if spec.get("parent") else None
        registered_at = now()
        item_id = new_uuid()
        quantity = Decimal(spec["quantity"])

        # The real hasher, same code path the API uses. Phase 4 shipped a
        # provisional shape here with a TODO; this is that TODO resolved.
        item_hash, _preimage = hash_item(
            item_id=item_id,
            category_slug=category.slug,
            category_schema_version=category.schema_version,
            parent_id=parent.id if parent else None,
            quantity=quantity,
            quantity_unit=category.quantity_unit,
            attributes=spec["attributes"],
            registered_by_hash=registrant_hash(registrant.id, registrant.identity_salt),
            registered_at=registered_at,
        )

        item = Item(
            id=item_id,
            category_id=category.id,
            category_schema_version=category.schema_version,
            parent_id=parent.id if parent else None,
            registered_by=registrant.id,
            attributes=spec["attributes"],
            quantity=quantity,
            quantity_unit=category.quantity_unit,
            item_hash=item_hash,
            # Bound below through app.qr.service, not here: a tag is an event as
            # well as a column, and setting the column alone would seed a tag
            # with no issuance history.
            tag_code=None,
            # PENDING, not CONFIRMED: nothing has been anchored on chain yet,
            # and claiming otherwise would make the Phase 7 demo a lie.
            status=ItemStatus.PENDING,
        )
        session.add(item)
        await session.flush()
        created_items[key] = item

        if spec.get("issue_tag"):
            # The same function the API calls: collision retry, TAG_ISSUED
            # event, canonical code. Demo tags are real tags.
            await bind_tag(session, item, registrant)

        registered_payload: dict[str, Any] = {
            "seed_key": key,
            "seed_hash_version": SEED_HASH_VERSION,
            "category": category.slug,
            "quantity": str(quantity),
            "parent": spec.get("parent"),
        }
        session.add(
            ItemEvent(
                item_id=item.id,
                event_type=ItemEventType.REGISTERED,
                actor_id=registrant.id,
                payload=registered_payload,
                payload_hash=hash_object(registered_payload),
            )
        )
        if parent is not None:
            split_payload = {
                "seed_key": f"{key}:split",
                "parent": parent.item_hash,
                "quantity": str(quantity),
            }
            session.add(
                ItemEvent(
                    item_id=item.id,
                    event_type=ItemEventType.SPLIT,
                    actor_id=registrant.id,
                    payload=split_payload,
                    payload_hash=hash_object(split_payload),
                )
            )

        for attestation in spec.get("attestations", []):
            attestor = users.get(attestation["by"])
            if attestor is None:
                report.notes.append(f"{key}: attestor {attestation['by']} not loaded")
                continue
            session.add(
                Attestation(
                    item_id=item.id,
                    attestor_id=attestor.id,
                    attestor_role=attestor.role,
                    statement=attestation["statement"],
                    statement_hash=seed_statement_hash(
                        item_hash=item.item_hash,
                        attestor_id=attestor.id,
                        role=str(attestor.role),
                        statement=attestation["statement"],
                    ),
                )
            )

        for index, scan in enumerate(spec.get("scans", [])):
            if item.tag_code is None:
                report.notes.append(f"{key}: scans seeded but no tag issued")
                break
            session.add(
                Scan(
                    item_id=item.id,
                    tag_code=item.tag_code,
                    country_code=scan["country_code"],
                    region_code=scan["region_code"],
                    # No raw IP is ever stored -- see app/db/models/scan.py.
                    ip_hash=sha256_hex(f"seed-scan-{key}-{index}".encode()),
                    device_fingerprint=f"seed-device-{key}-{index % 2}",
                    suspicion_level=SuspicionLevel.NONE,
                    created_at=now() - timedelta(days=int(scan["days_ago"])),
                )
            )

        report.created += 1

    await session.flush()
    return report


# ------------------------------------------------------------------ reputation


async def load_reputation(session: AsyncSession) -> SeedReport:
    """Apply the seed file's fraud flags, after items exist, via the real code path.

    Runs last on purpose. Flagging an actor is supposed to dispute everything
    they registered, so flagging one before their items are loaded produces a
    half-state the application has no way of reaching: flagged actor, clean
    records. Seed data that cannot arise from using the product is a trap for
    whoever debugs against it later.

    Uses :func:`app.attestation.reputation.flag_actor` rather than setting the
    column, so the seeded database ends up with the dispute rows, the item
    statuses, the provenance events and the audit event that a real flag
    produces -- and the Phase 8 demo can clear the flag and watch it reverse.
    """
    from app.attestation.reputation import flag_actor

    report = SeedReport(label="reputation")
    specs = _read("users.json")["users"]
    by_email = {
        user.email.lower(): user
        for user in (await session.execute(select(User))).scalars().all()
    }

    for spec in specs:
        if not spec.get("fraud_flagged"):
            continue
        user = by_email.get(str(spec["email"]).lower())
        if user is None:
            report.skipped += 1
            continue
        if user.fraud_flagged_at is not None:
            report.existed += 1
            continue
        await flag_actor(
            session,
            user.id,
            str(spec.get("fraud_reason") or "flagged during seeding"),
            None,
        )
        report.created += 1

    await session.flush()
    return report
