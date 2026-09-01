"""Building, signing and broadcasting anchoring transactions.

The private key never leaves this process. Signing happens locally with
``eth_account``; the node receives a signed blob and nothing else. Anything that
takes a key over JSON-RPC -- ``eth_sendTransaction``, ``personal_sign`` -- assumes
a trusted local node, and an Alchemy endpoint is neither local nor a place to put
a key.

Four decisions in here are load-bearing:

**Nothing is checked after the nonce is spent.** Preflight ``eth_call``, gas
estimation, fee computation and the fee cap all run *before* a nonce is
allocated. A nonce taken and then abandoned because the gas price was too high
is a hole that blocks every later transaction, so the cheap checks go first and
the expensive commitment goes last.

**The transaction row is written before the broadcast, not after.** Crashing
between the two leaves a row for a transaction that may not exist, which the gap
detector and the confirmation sweep both know how to resolve. The opposite order
leaves a *real, in-flight transaction that nothing in the database knows about*
-- unresolvable, because there is no hash to poll for.

**The fee cap is absolute.** Above ``CHAIN_MAX_FEE_GWEI`` the send is refused and
the job is requeued, not sent at whatever the chain is asking. An uncapped gas
price is a story about a testnet and a bill about a mainnet.

**A revert is not a failure until it is understood.** ``AlreadyAnchored`` means
the work is done -- which is exactly what a reorg replay hits when the original
transaction was re-included -- so it resolves the job as anchored rather than
retrying it or dead-lettering it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chain.client import (
    AmbiguousSendError,
    ChainClient,
    ChainUnavailable,
    ContractRevert,
    RpcError,
    TransientRpcError,
    normalise_hex,
)
from app.chain.contract import ContractBinding, DecodedRevert
from app.chain.nonce import NonceAllocator
from app.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.models.chain import ChainTx
from app.db.models.enums import ChainTxStatus, OutboxJobType
from app.db.models.outbox import Outbox

__all__ = [
    "GWEI",
    "ChainWriter",
    "GasPlan",
    "SendOutcome",
    "SendResult",
    "SignerUnavailable",
    "signer_address",
]

logger = get_logger(__name__)

GWEI = 10**9

SessionFactory = async_sessionmaker[AsyncSession]

# Gas for a plain value transfer. A gap fill carries no calldata, so this is
# exact and estimating it would be a wasted round trip.
PLAIN_TRANSFER_GAS = 21_000

# Node responses that mean something specific about a send's fate. Matched as
# substrings because every client words them slightly differently.
ALREADY_KNOWN_HINTS = ("already known", "already in the pool", "duplicate transaction")
NONCE_TOO_LOW_HINTS = ("nonce too low", "invalid nonce", "nonce is too low")
UNDERPRICED_HINTS = ("underpriced", "replacement transaction", "fee too low")
INSUFFICIENT_FUNDS_HINTS = ("insufficient funds", "insufficient balance")


class SignerUnavailable(RuntimeError):
    """No relayer key is configured, so nothing can be signed."""


class SendOutcome(StrEnum):
    """What happened to one send attempt."""

    SENT = "SENT"
    # The chain already holds this anchor. The job is complete.
    ALREADY_ANCHORED = "ALREADY_ANCHORED"
    # Not attempted: writes disabled, no signer, fee above the cap, budget spent.
    # The caller requeues; nothing was spent and no nonce was consumed.
    REFUSED = "REFUSED"
    # Preflight showed the call reverts. Sending would burn gas to achieve
    # nothing, so it is not sent.
    WOULD_REVERT = "WOULD_REVERT"
    # Reached the node and was rejected. Distinguished from REFUSED because a
    # nonce was consumed and may need reconciling.
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class GasPlan:
    """The gas limit and EIP-1559 fees one attempt will be signed with."""

    gas_limit: int
    max_fee_per_gas: int
    max_priority_fee_per_gas: int


@dataclass(frozen=True, slots=True)
class SendResult:
    """The outcome of one attempt, and how to describe it in a log or a row."""

    outcome: SendOutcome
    reason: str = ""
    tx_hash: str | None = None
    nonce: int | None = None
    chain_tx_id: uuid.UUID | None = None

    @property
    def succeeded(self) -> bool:
        """True when the job is done, or actually in flight."""
        return self.outcome in (SendOutcome.SENT, SendOutcome.ALREADY_ANCHORED)

    @property
    def retryable(self) -> bool:
        """True when requeueing this job could plausibly work later."""
        return self.outcome in (SendOutcome.REFUSED, SendOutcome.REJECTED)


def signer_address(settings: Settings | None = None) -> str | None:
    """Checksummed address of the relayer key, or ``None`` when unconfigured."""
    resolved = settings or get_settings()
    if not resolved.chain_signer_configured:
        return None
    from eth_account import Account

    account = Account.from_key(resolved.chain_signer_private_key.strip())
    return str(account.address)


class ChainWriter:
    """Signs and broadcasts anchoring transactions for one relayer key."""

    def __init__(
        self,
        client: ChainClient,
        binding: ContractBinding,
        allocator: NonceAllocator,
        session_factory: SessionFactory,
        settings: Settings | None = None,
    ) -> None:
        self._client = client
        self._binding = binding
        self._allocator = allocator
        self._session_factory = session_factory
        self._settings = settings or get_settings()
        self._account: Any | None = None

    # -------------------------------------------------------------- signing

    def _require_signer(self) -> Any:
        if self._account is None:
            if not self._settings.chain_signer_configured:
                raise SignerUnavailable("CHAIN_SIGNER_PRIVATE_KEY is empty")
            from eth_account import Account

            self._account = Account.from_key(self._settings.chain_signer_private_key.strip())
        return self._account

    @property
    def address(self) -> str:
        return str(self._require_signer().address)

    # ------------------------------------------------------------ public API

    async def anchor_item(
        self, outbox_id: uuid.UUID | None, item_hash: str, issuer_hash: str
    ) -> SendResult:
        """Broadcast ``anchorItem(itemHash, issuerHash)``."""
        calldata = self._binding.encode_anchor_item(item_hash, issuer_hash)
        return await self._send(
            outbox_id=outbox_id,
            to=self._binding.address,
            calldata=calldata,
            label="anchorItem",
        )

    async def anchor_attestation(
        self, outbox_id: uuid.UUID | None, statement_hash: str, attestor_hash: str
    ) -> SendResult:
        """Broadcast an attestation's statement hash through ``anchorItem``.

        The same contract function, deliberately. ``anchorItem`` records "this
        32-byte hash, claimed by this 32-byte issuer, at this time" and knows
        nothing about what the hash is a hash *of* -- which is exactly the
        guarantee an attestation needs. A second function taking the same two
        arguments would be a redeployment, a second selector and a second code
        path, all to record a distinction the chain does not have to understand.

        What keeps the two apart is the preimage: every attestation preimage
        carries ``"kind": "attestation"``, so an item hash and a statement hash
        can never collide even if their other fields lined up. The off-chain
        index resolves which is which by looking the hash up in ``items`` and
        ``attestations``.
        """
        calldata = self._binding.encode_anchor_item(statement_hash, attestor_hash)
        return await self._send(
            outbox_id=outbox_id,
            to=self._binding.address,
            calldata=calldata,
            label="anchorAttestation",
        )

    async def anchor_batch(
        self, outbox_id: uuid.UUID | None, root: str, leaf_count: int
    ) -> SendResult:
        """Broadcast ``anchorBatch(root, leafCount)``."""
        calldata = self._binding.encode_anchor_batch(root, leaf_count)
        return await self._send(
            outbox_id=outbox_id,
            to=self._binding.address,
            calldata=calldata,
            label="anchorBatch",
        )

    async def fill_gap(self, nonce: int) -> SendResult:
        """Close a stranded nonce with a zero-value self-send.

        Costs 21000 gas and unblocks every transaction queued behind the hole.
        Logged at warning because it means a send was lost, and a system that
        does this regularly has a crash to find, not a gap-filler to be pleased
        with.
        """
        logger.warning(
            "chain.nonce.gap_fill",
            nonce=nonce,
            address=self.address,
            reason="a nonce was allocated but never broadcast; every later transaction is blocked",
        )
        return await self._send(
            outbox_id=None,
            to=self.address,
            calldata=b"",
            label="gapFill",
            nonce=nonce,
            skip_preflight=True,
            gas_limit=PLAIN_TRANSFER_GAS,
        )

    async def replace(self, stuck: ChainTx) -> SendResult:
        """Resend a stuck transaction at the same nonce with a fee bump.

        Same nonce is the whole point: this *replaces* the pending transaction
        rather than queueing behind it. Nodes require a minimum bump -- 12.5% on
        both the max fee and the tip -- and reject anything smaller as an
        underpriced duplicate, which is why the bump is a configured floor
        rather than a guess.
        """
        bump = self._settings.chain_rbf_bump_bps
        previous_max = stuck.max_fee_per_gas or 0
        previous_tip = stuck.max_priority_fee_per_gas or 0
        floor = GasPlan(
            gas_limit=0,
            max_fee_per_gas=_bump(previous_max, bump),
            max_priority_fee_per_gas=_bump(previous_tip, bump),
        )

        # The replacement must do the same work as the transaction it displaces.
        # Resending an empty payload at that nonce would report success while
        # anchoring nothing, which is the quiet kind of wrong this phase exists
        # to avoid.
        rebuilt = await self._rebuild_calldata(stuck)
        if rebuilt is None:
            logger.warning(
                "chain.tx.replace_abandoned",
                nonce=stuck.nonce,
                chain_tx_id=str(stuck.id),
                reason="the originating outbox job is gone; cannot reconstruct the calldata",
            )
            return SendResult(
                outcome=SendOutcome.REFUSED,
                reason=f"cannot rebuild calldata for chain_tx {stuck.id}",
                nonce=stuck.nonce,
            )

        is_gap_fill = stuck.outbox_id is None
        logger.info(
            "chain.tx.replacing",
            nonce=stuck.nonce,
            previous_tx_hash=stuck.tx_hash,
            previous_max_fee_gwei=round(previous_max / GWEI, 4),
            bumped_max_fee_gwei=round(floor.max_fee_per_gas / GWEI, 4),
            kind="gapFill" if is_gap_fill else "anchor",
        )
        return await self._send(
            outbox_id=stuck.outbox_id,
            to=self.address if is_gap_fill else self._binding.address,
            calldata=rebuilt,
            label="replace",
            nonce=stuck.nonce,
            skip_preflight=True,
            gas_limit=PLAIN_TRANSFER_GAS if is_gap_fill else None,
            fee_floor=floor,
        )

    # ----------------------------------------------------------------- core

    async def _send(
        self,
        *,
        outbox_id: uuid.UUID | None,
        to: str,
        calldata: bytes,
        label: str,
        nonce: int | None = None,
        skip_preflight: bool = False,
        gas_limit: int | None = None,
        fee_floor: GasPlan | None = None,
    ) -> SendResult:
        """The one path every transaction takes. Order of operations matters."""
        # 1. Cheap refusals first: nothing spent, no nonce taken.
        if not self._settings.chain_write_enabled:
            return SendResult(
                outcome=SendOutcome.REFUSED,
                reason="CHAIN_WRITE_ENABLED=false",
            )
        try:
            sender = self.address
        except SignerUnavailable as exc:
            return SendResult(outcome=SendOutcome.REFUSED, reason=str(exc))

        # 2. Preflight. A revert found here costs one eth_call instead of a
        #    mined, reverted transaction and the gas it burned.
        if not skip_preflight:
            preflight = await self._preflight(sender, to, calldata, label)
            if preflight is not None:
                return preflight

        # 3. Gas and fees, including the hard cap.
        try:
            plan = await self._plan_gas(
                sender=sender,
                to=to,
                calldata=calldata,
                fixed_gas_limit=gas_limit,
                fee_floor=fee_floor,
            )
        except ChainUnavailable as exc:
            return SendResult(outcome=SendOutcome.REFUSED, reason=exc.message)
        except _FeeCapExceeded as exc:
            logger.warning(
                "chain.tx.refused_fee_cap",
                label=label,
                required_gwei=round(exc.required / GWEI, 4),
                cap_gwei=self._settings.chain_max_fee_gwei,
                action="job requeued; nothing sent and no nonce consumed",
            )
            return SendResult(outcome=SendOutcome.REFUSED, reason=str(exc))
        except ContractRevert as exc:
            decoded = self._binding.decode_revert(exc.data)
            return self._revert_result(decoded, exc, label)
        except RpcError as exc:
            return SendResult(outcome=SendOutcome.REFUSED, reason=f"gas planning failed: {exc}")

        # 4. Commit to a nonce. Everything after this point either sends or has
        #    to be reconciled.
        allocated_here = nonce is None
        chosen = await self._allocator.allocate() if allocated_here else int(nonce or 0)

        signed_hash, raw = self._sign(
            sender=sender, to=to, calldata=calldata, nonce=chosen, plan=plan
        )

        # 5. Record before broadcasting. See the module docstring.
        chain_tx_id = await self._record_attempt(
            outbox_id=outbox_id, tx_hash=signed_hash, nonce=chosen, plan=plan
        )

        # 6. Broadcast.
        try:
            await self._client.send_raw_transaction(raw)
        except TransientRpcError as exc:
            # By construction this never reached the node, so the nonce is
            # provably unused and the recorded attempt describes nothing.
            await self._abandon(chain_tx_id, f"not submitted: {exc}")
            if allocated_here:
                await self._allocator.rewind(chosen)
            logger.info("chain.tx.not_submitted", label=label, nonce=chosen, error=str(exc))
            return SendResult(
                outcome=SendOutcome.REFUSED,
                reason=f"not submitted: {exc}",
                nonce=chosen,
                chain_tx_id=chain_tx_id,
            )
        except AmbiguousSendError as exc:
            # The node may be holding this transaction. Resending at this nonce
            # is the one thing that must not happen, so the row stays SENT and
            # the confirmation sweep decides.
            logger.warning(
                "chain.tx.ambiguous",
                label=label,
                nonce=chosen,
                tx_hash=signed_hash,
                error=str(exc),
                resolution="left as SENT; receipt polling will settle it",
            )
            return SendResult(
                outcome=SendOutcome.SENT,
                reason=f"submission ambiguous: {exc}",
                tx_hash=signed_hash,
                nonce=chosen,
                chain_tx_id=chain_tx_id,
            )
        except ChainUnavailable as exc:
            await self._abandon(chain_tx_id, exc.message)
            if allocated_here:
                await self._allocator.rewind(chosen)
            return SendResult(
                outcome=SendOutcome.REFUSED, reason=exc.message, nonce=chosen,
                chain_tx_id=chain_tx_id,
            )
        except RpcError as exc:
            return await self._classify_rejection(
                exc, chain_tx_id=chain_tx_id, nonce=chosen, tx_hash=signed_hash, label=label
            )

        logger.info(
            "chain.tx.sent",
            label=label,
            nonce=chosen,
            tx_hash=signed_hash,
            gas_limit=plan.gas_limit,
            max_fee_gwei=round(plan.max_fee_per_gas / GWEI, 4),
        )
        return SendResult(
            outcome=SendOutcome.SENT,
            tx_hash=signed_hash,
            nonce=chosen,
            chain_tx_id=chain_tx_id,
        )

    # ----------------------------------------------------------- sub-steps

    async def _preflight(
        self, sender: str, to: str, calldata: bytes, label: str
    ) -> SendResult | None:
        """``eth_call`` the transaction. Returns a result only when it will not send."""
        try:
            await self._client.call(
                {
                    "from": sender,
                    "to": to,
                    "data": normalise_hex(calldata),
                    "value": 0,
                }
            )
        except ContractRevert as exc:
            decoded = self._binding.decode_revert(exc.data)
            return self._revert_result(decoded, exc, label)
        except ChainUnavailable as exc:
            return SendResult(outcome=SendOutcome.REFUSED, reason=exc.message)
        except RpcError as exc:
            # An unreachable node during preflight is a refusal, not a failure:
            # the job goes back on the queue and nothing was spent.
            return SendResult(outcome=SendOutcome.REFUSED, reason=f"preflight failed: {exc}")
        return None

    def _revert_result(
        self, decoded: DecodedRevert | None, exc: ContractRevert, label: str
    ) -> SendResult:
        if decoded is not None and decoded.is_already_anchored:
            logger.info(
                "chain.tx.already_anchored",
                label=label,
                detail=str(decoded),
                interpretation="the chain already holds this anchor; the job is complete",
            )
            return SendResult(
                outcome=SendOutcome.ALREADY_ANCHORED, reason=str(decoded)
            )
        reason = str(decoded) if decoded is not None else (exc.data or str(exc))
        logger.warning("chain.tx.would_revert", label=label, reason=reason)
        return SendResult(outcome=SendOutcome.WOULD_REVERT, reason=reason)

    async def _plan_gas(
        self,
        *,
        sender: str,
        to: str,
        calldata: bytes,
        fixed_gas_limit: int | None,
        fee_floor: GasPlan | None,
    ) -> GasPlan:
        """Estimate gas, buffer it, compute EIP-1559 fees, enforce the cap."""
        if fixed_gas_limit is not None:
            gas_limit = fixed_gas_limit
        else:
            estimate = await self._client.estimate_gas(
                {"from": sender, "to": to, "data": normalise_hex(calldata), "value": 0}
            )
            buffer = self._settings.chain_gas_buffer_percent
            gas_limit = estimate * (100 + buffer) // 100

        fees = await self._client.fee_data()
        tip = max(fees.max_priority_fee_per_gas, 1)
        # Two base fees of headroom: the base fee can rise 12.5% per block, so
        # this survives roughly six consecutive full blocks before the
        # transaction stalls and replace-by-fee has to step in.
        max_fee = fees.base_fee_per_gas * 2 + tip

        if fee_floor is not None:
            tip = max(tip, fee_floor.max_priority_fee_per_gas)
            max_fee = max(max_fee, fee_floor.max_fee_per_gas, tip)

        cap = self._settings.chain_max_fee_gwei * GWEI
        if max_fee > cap:
            raise _FeeCapExceeded(required=max_fee, cap=cap)
        # The tip can never exceed the max fee; a node rejects that outright.
        tip = min(tip, max_fee)

        return GasPlan(
            gas_limit=gas_limit, max_fee_per_gas=max_fee, max_priority_fee_per_gas=tip
        )

    def _sign(
        self, *, sender: str, to: str, calldata: bytes, nonce: int, plan: GasPlan
    ) -> tuple[str, bytes]:
        """Sign locally. Returns ``(tx_hash, raw_bytes)``.

        The hash is known before broadcasting, which is what makes
        record-then-send possible at all.
        """
        account = self._require_signer()
        transaction: dict[str, Any] = {
            "type": 2,
            "chainId": self._settings.chain_id,
            "nonce": nonce,
            "to": to,
            "value": 0,
            "data": normalise_hex(calldata) if calldata else "0x",
            "gas": plan.gas_limit,
            "maxFeePerGas": plan.max_fee_per_gas,
            "maxPriorityFeePerGas": plan.max_priority_fee_per_gas,
        }
        signed = account.sign_transaction(transaction)
        # eth-account renamed ``rawTransaction`` to ``raw_transaction`` in 0.13.
        # Reading both keeps this working across the version boundary rather
        # than failing at the one moment a transaction is ready to send.
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
        if raw is None:
            raise RuntimeError("eth-account returned a signed transaction with no raw payload")
        return normalise_hex(signed.hash), bytes(raw)

    async def _record_attempt(
        self, *, outbox_id: uuid.UUID | None, tx_hash: str, nonce: int, plan: GasPlan
    ) -> uuid.UUID:
        """Insert the ``chain_txs`` row and commit it before broadcasting."""
        async with self._session_factory() as session:
            row = ChainTx(
                outbox_id=outbox_id,
                tx_hash=tx_hash,
                nonce=nonce,
                status=ChainTxStatus.SENT,
                max_fee_per_gas=plan.max_fee_per_gas,
                max_priority_fee_per_gas=plan.max_priority_fee_per_gas,
            )
            session.add(row)
            await session.commit()
            return row.id

    async def _abandon(self, chain_tx_id: uuid.UUID, reason: str) -> None:
        """Mark an attempt that provably never reached the node."""
        async with self._session_factory() as session:
            row = await session.get(ChainTx, chain_tx_id)
            if row is not None:
                row.status = ChainTxStatus.FAILED
                row.raw_receipt = {"abandoned": reason}
                await session.commit()

    async def _classify_rejection(
        self,
        exc: RpcError,
        *,
        chain_tx_id: uuid.UUID,
        nonce: int,
        tx_hash: str,
        label: str,
    ) -> SendResult:
        """Read a node's rejection message for what it says about the nonce."""
        message = str(exc).lower()

        if any(hint in message for hint in ALREADY_KNOWN_HINTS):
            # The node already holds this exact transaction. Nothing is wrong;
            # the row is already SENT and polling will settle it.
            logger.info("chain.tx.already_known", label=label, nonce=nonce, tx_hash=tx_hash)
            return SendResult(
                outcome=SendOutcome.SENT, tx_hash=tx_hash, nonce=nonce, chain_tx_id=chain_tx_id
            )

        if any(hint in message for hint in NONCE_TOO_LOW_HINTS):
            # This nonce is already mined. Either an earlier attempt landed, or
            # the stored nonce drifted behind the chain. Startup reconciliation
            # is the fix; this attempt is dead.
            await self._abandon(chain_tx_id, f"nonce too low: {exc}")
            logger.warning(
                "chain.tx.nonce_too_low",
                label=label,
                nonce=nonce,
                consequence="an earlier attempt at this nonce is already on chain",
            )
            return SendResult(
                outcome=SendOutcome.REJECTED, reason=str(exc), nonce=nonce,
                chain_tx_id=chain_tx_id,
            )

        if any(hint in message for hint in INSUFFICIENT_FUNDS_HINTS):
            await self._abandon(chain_tx_id, f"insufficient funds: {exc}")
            logger.error(
                "chain.tx.insufficient_funds",
                label=label,
                address=self.address,
                action="fund the relayer key; every anchor is blocked until then",
            )
            return SendResult(
                outcome=SendOutcome.REJECTED, reason=str(exc), nonce=nonce,
                chain_tx_id=chain_tx_id,
            )

        if any(hint in message for hint in UNDERPRICED_HINTS):
            await self._abandon(chain_tx_id, f"underpriced: {exc}")
            logger.warning("chain.tx.underpriced", label=label, nonce=nonce, error=str(exc))
            return SendResult(
                outcome=SendOutcome.REJECTED, reason=str(exc), nonce=nonce,
                chain_tx_id=chain_tx_id,
            )

        await self._abandon(chain_tx_id, str(exc))
        logger.warning("chain.tx.rejected", label=label, nonce=nonce, error=str(exc))
        return SendResult(
            outcome=SendOutcome.REJECTED, reason=str(exc), nonce=nonce, chain_tx_id=chain_tx_id
        )

    async def _rebuild_calldata(self, previous: ChainTx) -> bytes | None:
        """Reconstruct the calldata of a stuck attempt from its outbox job."""
        if previous.outbox_id is None:
            # A gap fill has no payload to rebuild; it is a self-send either way.
            return b""

        async with self._session_factory() as session:
            job = await session.get(Outbox, previous.outbox_id)
            if job is None:
                return None
            payload = dict(job.payload)

        if job.job_type == OutboxJobType.ANCHOR_ITEM:
            return self._binding.encode_anchor_item(
                str(payload["item_hash"]), str(payload["issuer_hash"])
            )
        if job.job_type == OutboxJobType.ANCHOR_ATTESTATION:
            return self._binding.encode_anchor_item(
                str(payload["statement_hash"]), str(payload["issuer_hash"])
            )
        if job.job_type == OutboxJobType.ANCHOR_BATCH:
            return self._binding.encode_anchor_batch(
                str(payload["root"]), int(str(payload["leaf_count"]))
            )
        # Reached by any job type this writer does not anchor -- PIN_MEDIA, for
        # one. Returning None stops the caller rebuilding empty calldata and
        # sending a transaction that anchors nothing.
        return None


class _FeeCapExceeded(RuntimeError):
    """Required fee is above ``CHAIN_MAX_FEE_GWEI``. Refuse, never send anyway."""

    def __init__(self, required: int, cap: int) -> None:
        self.required = required
        self.cap = cap
        super().__init__(
            f"required max fee {required / GWEI:.4f} gwei exceeds cap {cap / GWEI:.4f} gwei"
        )


def _bump(value: int, bps: int) -> int:
    """Raise *value* by *bps* basis points, rounding up.

    Rounding up matters: a 12.5% bump that rounds down lands one wei under the
    node's threshold and is rejected as underpriced.
    """
    if value <= 0:
        return 0
    return -(-value * (10_000 + bps) // 10_000)
