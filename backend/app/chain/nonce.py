"""Nonce allocation, serialised through Postgres rather than read from the node.

``eth_getTransactionCount(address, 'pending')`` is the obvious source for the
next nonce and it is wrong under concurrency. It reports what the *node* knows,
and a transaction this process signed a millisecond ago is not in that count
yet. Two workers asking at the same moment both get ``n``, both sign at ``n``,
and the second silently replaces the first on chain -- one anchor lost, no
exception anywhere. So the authority is a row in ``chain_nonce``, read with
``SELECT ... FOR UPDATE``, and the node's opinion is used only to detect that
the two have drifted apart.

Three operations, each with a failure mode worth naming:

**Allocate.** One row lock, held for a single statement. The lock window has to
stay tiny, because every send in the system queues behind it.

**Reconcile at startup.** If the chain is *ahead* of the database, transactions
went out that this system did not record -- a manual send, a restored backup, a
lost volume -- and the chain wins, because the chain is what the next
transaction has to fit behind. If the database is *ahead*, that is the normal
case for in-flight transactions the node has not surfaced yet, and the database
wins. Both are logged; the first is logged loudly, because it means something
happened outside this system.

**Fill a gap.** A nonce allocated but never sent -- process killed between the
two -- is a hole, and *every* later transaction sits in the mempool forever
waiting for it. Nothing throws. The queue just stops, and items stay PENDING
while the API reports healthy. The fix is a zero-value self-send at that nonce
to close the hole, logged as what it is.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chain.client import ChainClient
from app.config import get_settings
from app.core.clock import now
from app.core.logging import get_logger
from app.db.models.chain import ChainNonce, ChainTx
from app.db.models.enums import ChainTxStatus

__all__ = ["NonceAllocator", "NonceGap", "NonceReconciliation"]

logger = get_logger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]


@dataclass(frozen=True, slots=True)
class NonceReconciliation:
    """What startup reconciliation found and what it decided."""

    address: str
    db_next: int
    chain_pending: int
    adopted: int
    source: str  # "chain" | "db" | "created"


@dataclass(frozen=True, slots=True)
class NonceGap:
    """A nonce that was allocated but for which nothing was ever broadcast."""

    nonce: int
    reason: str


class NonceAllocator:
    """Serialised nonce allocation for one signing address."""

    def __init__(self, session_factory: SessionFactory, address: str) -> None:
        self.address = address
        self._session_factory = session_factory

    # ------------------------------------------------------------- allocate

    async def allocate(self) -> int:
        """Take the next nonce and commit the increment.

        Runs in its own transaction, deliberately. Holding this row lock inside
        the caller's longer transaction -- which also writes ``chain_txs`` and
        touches the outbox -- would serialise the entire outbox drain behind
        whichever worker is slowest to sign.
        """
        async with self._session_factory() as session:
            row = await self._lock_row(session)
            allocated = row.next_nonce
            row.next_nonce = allocated + 1
            row.updated_at = now()
            await session.commit()
        logger.debug("chain.nonce.allocated", address=self.address, nonce=allocated)
        return allocated

    async def rewind(self, nonce: int) -> bool:
        """Return an unused nonce, but only if nothing was allocated after it.

        A send refused before submission -- gas above the cap, writes disabled,
        budget spent -- leaves a nonce allocated and unusable. Giving it back
        avoids manufacturing a gap that the gap-filler then has to spend gas
        closing. If another worker has already allocated past it the rewind is
        declined, because rewinding then would hand out a nonce twice, which is
        strictly worse than a gap.
        """
        async with self._session_factory() as session:
            row = await self._lock_row(session)
            if row.next_nonce != nonce + 1:
                await session.commit()
                logger.info(
                    "chain.nonce.rewind_declined",
                    address=self.address,
                    nonce=nonce,
                    next_nonce=row.next_nonce,
                    reason="another allocation happened after this one",
                )
                return False
            row.next_nonce = nonce
            row.updated_at = now()
            await session.commit()
        logger.info("chain.nonce.rewound", address=self.address, nonce=nonce)
        return True

    async def current(self) -> int:
        """The next nonce that would be handed out. Read-only, no lock."""
        async with self._session_factory() as session:
            row = await self._ensure_row(session, 0)
            await session.commit()
            return row.next_nonce

    # ------------------------------------------------------------ reconcile

    async def reconcile(self, client: ChainClient) -> NonceReconciliation:
        """Align the stored nonce with the node's pending count at startup."""
        chain_pending = await client.get_transaction_count(self.address, "pending")

        async with self._session_factory() as session:
            existing = await session.get(ChainNonce, self.address)
            if existing is None:
                row = await self._ensure_row(session, chain_pending)
                await session.commit()
                logger.info(
                    "chain.nonce.initialised",
                    address=self.address,
                    next_nonce=row.next_nonce,
                    source="chain",
                )
                return NonceReconciliation(
                    address=self.address,
                    db_next=chain_pending,
                    chain_pending=chain_pending,
                    adopted=row.next_nonce,
                    source="created",
                )

            row = await self._lock_row(session)
            db_next = row.next_nonce

            if chain_pending > db_next:
                # Something sent from this address that this system did not
                # record. Keeping the lower value would sign every future
                # transaction at a nonce the node already considers used.
                row.next_nonce = chain_pending
                row.updated_at = now()
                await session.commit()
                logger.warning(
                    "chain.nonce.chain_ahead",
                    address=self.address,
                    db_next=db_next,
                    chain_pending=chain_pending,
                    adopted=chain_pending,
                    cause="transactions were sent from this key outside this system, "
                    "or persisted state was lost",
                )
                return NonceReconciliation(
                    address=self.address,
                    db_next=db_next,
                    chain_pending=chain_pending,
                    adopted=chain_pending,
                    source="chain",
                )

            await session.commit()

        if db_next > chain_pending:
            logger.info(
                "chain.nonce.db_ahead",
                address=self.address,
                db_next=db_next,
                chain_pending=chain_pending,
                reason="in-flight transactions the node has not surfaced; keeping the db value",
            )
        return NonceReconciliation(
            address=self.address,
            db_next=db_next,
            chain_pending=chain_pending,
            adopted=db_next,
            source="db",
        )

    # ----------------------------------------------------------------- gaps

    async def find_gaps(self, client: ChainClient) -> list[NonceGap]:
        """Nonces that block the queue: allocated, never broadcast, node unaware.

        Scanned from the node's pending count upward, because a nonce below that
        count has already been consumed on chain and cannot be blocking
        anything. Everything from there up to the stored next-nonce is either a
        transaction in flight or a hole.
        """
        settings = get_settings()
        chain_pending = await client.get_transaction_count(self.address, "pending")
        db_next = await self.current()
        if db_next <= chain_pending:
            return []

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(ChainTx)
                    .where(ChainTx.nonce >= chain_pending, ChainTx.nonce < db_next)
                    .order_by(ChainTx.nonce, ChainTx.created_at.desc())
                )
            ).scalars()
            by_nonce: dict[int, list[ChainTx]] = {}
            for row in rows:
                by_nonce.setdefault(row.nonce, []).append(row)

        gaps: list[NonceGap] = []
        for nonce in range(chain_pending, db_next):
            attempts = by_nonce.get(nonce, [])
            if not attempts:
                gaps.append(
                    NonceGap(nonce=nonce, reason="allocated but no transaction was ever recorded")
                )
                continue

            live = [tx for tx in attempts if tx.status in (ChainTxStatus.SENT, ChainTxStatus.MINED)]
            if not live:
                gaps.append(NonceGap(nonce=nonce, reason="every attempt at this nonce is terminal"))
                continue

            newest = max(live, key=lambda tx: tx.created_at)
            age = (now() - newest.created_at).total_seconds()
            if age < settings.chain_tx_timeout_seconds:
                continue

            # Recorded as sent and old enough to worry about. If the node has
            # never heard of it, it was dropped from the mempool -- or the
            # process died after writing the row and before broadcasting -- and
            # the nonce is a hole either way.
            known = False
            for tx in live:
                if tx.tx_hash and await client.transaction_exists(tx.tx_hash):
                    known = True
                    break
            if not known:
                gaps.append(
                    NonceGap(
                        nonce=nonce,
                        reason=f"no attempt is known to the node after {int(age)}s",
                    )
                )

        if gaps:
            logger.warning(
                "chain.nonce.gaps_detected",
                address=self.address,
                nonces=[gap.nonce for gap in gaps],
                chain_pending=chain_pending,
                db_next=db_next,
                consequence="every later transaction stays in the mempool until these are filled",
            )
        return gaps

    # -------------------------------------------------------------- helpers

    async def _ensure_row(self, session: AsyncSession, initial: int) -> ChainNonce:
        """Create the address row if it is missing, then return it."""
        await session.execute(
            insert(ChainNonce)
            .values(address=self.address, next_nonce=initial)
            .on_conflict_do_nothing(index_elements=[ChainNonce.address])
        )
        return (
            await session.execute(select(ChainNonce).where(ChainNonce.address == self.address))
        ).scalar_one()

    async def _lock_row(self, session: AsyncSession) -> ChainNonce:
        """Row-lock the address, creating it at zero if it does not exist yet."""
        await self._ensure_row(session, 0)
        return (
            await session.execute(
                select(ChainNonce).where(ChainNonce.address == self.address).with_for_update()
            )
        ).scalar_one()
