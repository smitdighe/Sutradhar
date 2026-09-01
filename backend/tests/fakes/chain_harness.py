"""Wiring for the chain tests: a fake node, a real writer, and a real database.

Everything below the RPC boundary is the production code path. Only the node is
substituted, which is the point -- a test that stubs the writer proves the stub
works, and the failures worth catching here live in the writer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chain.client import ChainClient
from app.chain.confirmations import ConfirmationSweep
from app.chain.contract import ContractBinding, load_contract
from app.chain.indexer import EventIndexer
from app.chain.nonce import NonceAllocator
from app.chain.outbox import OutboxRepository, enqueue_job
from app.chain.writer import ChainWriter
from app.config import Settings, get_settings
from app.core.crypto_shred import new_salt
from app.core.hashing import hash_object
from app.db.models.catalog import GICategory, Item, ItemEvent
from app.db.models.enums import (
    ItemEventType,
    ItemStatus,
    OutboxJobType,
    UserRole,
    UserStatus,
)
from app.db.models.user import User
from app.provenance.item_hash import hash_item, registrant_hash
from app.workers.jobs import ChainRuntime
from tests.fakes.fake_chain import FakeChain

# A throwaway key with no funds on any network, checked in on purpose so the
# tests are reproducible. Never used outside them.
TEST_PRIVATE_KEY = "0x" + "42" * 32
TEST_CONTRACT_ADDRESS = "0x" + "5d" * 20

SessionFactory = async_sessionmaker[AsyncSession]


@dataclass
class ChainHarness:
    """A complete chain stack pointed at a fake node."""

    settings: Settings
    session_factory: SessionFactory
    chain: FakeChain
    client: ChainClient
    binding: ContractBinding
    allocator: NonceAllocator
    writer: ChainWriter
    outbox: OutboxRepository
    sweep: ConfirmationSweep
    indexer: EventIndexer

    @property
    def runtime(self) -> ChainRuntime:
        """The same object the scheduler drives, so job code is covered too."""
        return ChainRuntime(
            settings=self.settings,
            session_factory=self.session_factory,
            client=self.client,
            binding=self.binding,
            outbox=self.outbox,
            allocator=self.allocator,
            writer=self.writer,
            sweep=self.sweep,
            indexer=self.indexer,
            signer=self.writer.address,
        )

    def confirm_depth(self) -> None:
        """Mine exactly enough blocks to satisfy the confirmation threshold."""
        self.chain.mine(self.settings.chain_confirmations)


def build_settings(**overrides: Any) -> Settings:
    """A settings object for one test, without touching the process singleton."""
    base = get_settings()
    defaults: dict[str, Any] = {
        "chain_id": 31_337,
        "chain_write_enabled": True,
        "chain_signer_private_key": TEST_PRIVATE_KEY,
        "contract_address": TEST_CONTRACT_ADDRESS,
        "chain_confirmations": 3,
        "chain_max_fee_gwei": 100,
        "chain_tx_timeout_seconds": 120,
        "outbox_max_attempts": 3,
        "outbox_batch_size": 50,
        "outbox_lock_stale_seconds": 600,
        "outbox_backoff_cap_seconds": 300,
        "indexer_block_range": 2_000,
        "chain_rpc_max_retries": 5,
        "scheduler_enabled": False,
    }
    return base.model_copy(update={**defaults, **overrides})


def build_harness(
    session_factory: SessionFactory,
    chain: FakeChain | None = None,
    **overrides: Any,
) -> ChainHarness:
    """Assemble the stack. Pass a pre-configured ``FakeChain`` to inject failures."""
    settings = build_settings(**overrides)
    binding = load_contract(address=settings.contract_address)
    fake = chain or FakeChain(
        contract_address=binding.address, chain_id=settings.chain_id
    )
    # No quota meter: metering is exercised by its own tests, and threading a
    # live QuotaTracker through every chain test would put a Postgres write in
    # front of every simulated RPC call.
    client = ChainClient(fake, meter=None, settings=settings)

    from app.chain.writer import signer_address

    address = signer_address(settings)
    assert address is not None
    allocator = NonceAllocator(session_factory, address)
    writer = ChainWriter(client, binding, allocator, session_factory, settings)
    outbox = OutboxRepository(session_factory, settings, worker_id="test-worker")
    sweep = ConfirmationSweep(
        client, binding, outbox, session_factory, writer=writer, settings=settings
    )
    indexer = EventIndexer(client, binding, session_factory, settings)

    return ChainHarness(
        settings=settings,
        session_factory=session_factory,
        chain=fake,
        client=client,
        binding=binding,
        allocator=allocator,
        writer=writer,
        outbox=outbox,
        sweep=sweep,
        indexer=indexer,
    )


# ------------------------------------------------------------------ seeding

SAMPLE_ATTRIBUTES: dict[str, Any] = {
    "warp_count": 120,
    "weft_count": 116,
    "dye_type": "natural",
    "double_ikat": True,
    "loom_type": "pit",
    "weave_days": 210,
    "gi_registration_no": "GI-00232",
}


async def make_weaver(session: AsyncSession) -> User:
    """A minimal ACTIVE weaver. No HTTP round trip; these tests are below the API."""
    from app.auth.password import hash_password

    user = User(
        email=f"chain-{uuid.uuid4().hex[:10]}@example.com",
        password_hash=hash_password("correct-horse-battery-staple"),
        display_name="Chain Test Weaver",
        role=UserRole.WEAVER,
        status=UserStatus.ACTIVE,
        identity_salt=new_salt(),
    )
    session.add(user)
    await session.flush()
    return user


async def make_category(session: AsyncSession, slug: str = "patola-silk") -> GICategory:
    """A category with a permissive schema, enough for the hash to be well formed."""
    category = GICategory(
        slug=slug,
        display_name="Patola Silk",
        schema_version=1,
        attribute_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        },
        quantity_unit="metre",
        is_active=True,
    )
    session.add(category)
    await session.flush()
    return category


async def seed_item(
    session: AsyncSession,
    weaver: User,
    category: GICategory,
    *,
    quantity: str = "12.0000",
    enqueue: bool = True,
    attributes: dict[str, Any] | None = None,
) -> Item:
    """Create one PENDING item exactly the way registration does, outbox row and all.

    Uses the frozen preimage rather than a hand-written hash, so a change to
    ``item_hash.py`` breaks these tests too instead of leaving them agreeing with
    a stale format.
    """
    from app.core.clock import now
    from app.core.ids import new_uuid

    item_id = new_uuid()
    registered_at = now()
    issuer_hash = registrant_hash(weaver.id, weaver.identity_salt)
    payload = attributes if attributes is not None else dict(SAMPLE_ATTRIBUTES)

    item_hash, preimage = hash_item(
        item_id=item_id,
        category_slug=category.slug,
        category_schema_version=category.schema_version,
        parent_id=None,
        quantity=Decimal(quantity),
        quantity_unit=category.quantity_unit,
        attributes=payload,
        registered_by_hash=issuer_hash,
        registered_at=registered_at,
    )

    item = Item(
        id=item_id,
        category_id=category.id,
        category_schema_version=category.schema_version,
        parent_id=None,
        registered_by=weaver.id,
        attributes=payload,
        quantity=Decimal(quantity),
        quantity_unit=category.quantity_unit,
        item_hash=item_hash,
        status=ItemStatus.PENDING,
        created_at=registered_at,
        updated_at=registered_at,
    )
    session.add(item)
    await session.flush()

    session.add(
        ItemEvent(
            item_id=item.id,
            event_type=ItemEventType.REGISTERED,
            actor_id=weaver.id,
            payload={"preimage": preimage, "item_hash": item_hash},
            payload_hash=hash_object({"preimage": preimage, "item_hash": item_hash}),
        )
    )

    if enqueue:
        await enqueue_job(
            session,
            job_type=OutboxJobType.ANCHOR_ITEM,
            payload={
                "item_id": str(item.id),
                "item_hash": item_hash,
                "issuer_hash": issuer_hash,
            },
            dedupe_key=item_hash,
        )

    return item
