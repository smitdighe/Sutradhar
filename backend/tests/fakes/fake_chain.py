"""An in-memory EVM that behaves badly on command.

This is not a mock. It implements ``app.chain.client.ChainRpc`` with real
semantics -- nonce ordering, block inclusion, receipts, logs, and the registry
contract's own storage and reverts -- so the code under test exercises its actual
encoding, decoding and state machine rather than a stubbed shape of them.

It exists because the failure modes that matter here cannot be provoked against
a real chain on demand. A testnet will not reorganise a block because a test
asked it to, will not drop exactly the third RPC call, and will not revert a
transaction on cue. Without those, every path in ``app/chain`` that handles a bad
day ships untested, and those are precisely the paths where a mistake produces a
system that looks correct and quietly lies.

What can be injected:

* ``mining_delay_blocks`` -- how long a transaction sits in the mempool.
* ``fail_next(n)`` -- the next *n* calls raise a transient error.
* ``fail_next_sends(n, ambiguous=...)`` -- sends fail, optionally in the way that
  means "this may already have been submitted".
* ``reorg(from_number)`` -- orphan a block and everything above it, giving those
  heights new hashes.
* ``force_revert`` / ``AlreadyAnchored`` -- reverted transactions, mined with
  ``status == 0``.
* Duplicate nonces are rejected exactly as a node rejects them.

Mining is explicit by default. A test that says ``chain.mine(3)`` is readable
and deterministic; one that sleeps is neither.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import rlp
from eth_abi.abi import decode as abi_decode, encode as abi_encode
from eth_account import Account

from app.chain.client import (
    AmbiguousSendError,
    BlockSummary,
    ContractRevert,
    FeeData,
    LogEntry,
    RpcError,
    TransientRpcError,
    TxReceipt,
    normalise_hex,
)
from app.chain.contract import event_topic, selector
from app.core.hashing import keccak256

__all__ = ["FakeBlock", "FakeChain", "PendingTx"]

ITEM_ANCHORED_TOPIC = event_topic("ItemAnchored", ("bytes32", "bytes32", "address", "uint256"))
BATCH_ANCHORED_TOPIC = event_topic("BatchAnchored", ("bytes32", "uint32", "address", "uint256"))

ANCHOR_ITEM_SELECTOR = normalise_hex(selector("anchorItem", ("bytes32", "bytes32")))
ANCHOR_BATCH_SELECTOR = normalise_hex(selector("anchorBatch", ("bytes32", "uint32")))
IS_ITEM_ANCHORED_SELECTOR = normalise_hex(selector("isItemAnchored", ("bytes32",)))
IS_BATCH_ANCHORED_SELECTOR = normalise_hex(selector("isBatchAnchored", ("bytes32",)))

ALREADY_ANCHORED_SELECTOR = selector("AlreadyAnchored", ("bytes32",))
NOT_WRITER_SELECTOR = selector("NotWriter", ())
ZERO_HASH_SELECTOR = selector("ZeroHash", ())

ANCHOR_GAS = 51_000
ZERO_BYTES32 = "0x" + "00" * 32


@dataclass(slots=True)
class PendingTx:
    """A signed transaction the node has accepted but not yet included."""

    tx_hash: str
    sender: str
    nonce: int
    to: str
    data: str
    value: int
    max_fee_per_gas: int
    max_priority_fee_per_gas: int
    submitted_at_height: int


@dataclass(slots=True)
class FakeBlock:
    """A mined block and the logs its transactions produced."""

    number: int
    hash: str
    parent_hash: str
    timestamp: int
    transactions: list[str] = field(default_factory=list)
    logs: list[LogEntry] = field(default_factory=list)


class FakeChain:
    """A deterministic EVM stand-in that implements the registry's semantics."""

    def __init__(
        self,
        contract_address: str,
        chain_id: int = 31_337,
        mining_delay_blocks: int = 0,
        writers: set[str] | None = None,
        base_fee_per_gas: int = 1_000_000_000,
        priority_fee_per_gas: int = 1_000_000_000,
    ) -> None:
        self.contract_address = normalise_hex(contract_address)
        # Stored under a private name: ``chain_id`` is a coroutine on the
        # ChainRpc protocol, and an attribute of the same name would shadow the
        # method on every instance.
        self._chain_id = chain_id
        self.mining_delay_blocks = mining_delay_blocks
        self.base_fee_per_gas = base_fee_per_gas
        self.priority_fee_per_gas = priority_fee_per_gas

        # Blank allowlist means permissionless, so a test does not have to
        # remember to authorise a key it never mentioned.
        self.writers: set[str] = {normalise_hex(w) for w in (writers or set())}

        # Bumped by a reorg so replayed heights get genuinely different hashes.
        # Set before the genesis block, which is hashed with it.
        self._hash_salt = 0

        self.blocks: list[FakeBlock] = [
            FakeBlock(number=0, hash=self._block_hash(0), parent_hash=ZERO_BYTES32, timestamp=0)
        ]
        self.mempool: dict[str, PendingTx] = {}
        self.receipts: dict[str, TxReceipt] = {}
        self.known: set[str] = set()
        # Every transaction ever accepted, so a reorg can put one back in the
        # mempool. A real node keeps this too; without it, orphaned transactions
        # would vanish instead of returning to the queue.
        self._accepted: dict[str, PendingTx] = {}

        # Next nonce the node will accept per sender, exactly as a real node
        # tracks it: mined count plus whatever is queued in the mempool.
        self.mined_nonces: dict[str, int] = {}

        # Registry state.
        self.item_anchors: dict[str, tuple[str, int]] = {}
        self.batch_anchors: dict[str, tuple[str, int, int]] = {}

        # --- injection knobs ---
        self.fail_calls_remaining = 0
        self.fail_sends_remaining = 0
        self.fail_sends_ambiguous = False
        self.force_revert = False
        self.reject_duplicate_nonce = True

        self.call_log: list[str] = []

    # ------------------------------------------------------------ injection

    def fail_next(self, count: int) -> None:
        """The next *count* RPC calls raise a transient error."""
        self.fail_calls_remaining = count

    def fail_next_sends(self, count: int, ambiguous: bool = False) -> None:
        """The next *count* sends fail.

        ``ambiguous=True`` produces the dangerous case: a timeout after
        submission, where the node may already hold the transaction and a resend
        at the same nonce would be a second competing transaction.
        """
        self.fail_sends_remaining = count
        self.fail_sends_ambiguous = ambiguous

    def reorg(self, from_number: int, return_to_mempool: bool = False) -> list[str]:
        """Orphan every block from *from_number* up and re-mine those heights empty.

        Returns the transaction hashes that were orphaned. Their receipts are
        withdrawn, because a receipt describes inclusion in a block, and that
        block no longer exists. The heights come back with different hashes,
        which is exactly the signal reorg detection looks for.
        """
        if from_number <= 0 or from_number >= len(self.blocks):
            raise ValueError(f"cannot reorg from block {from_number}")

        self._hash_salt += 1
        orphaned_blocks = self.blocks[from_number:]
        orphaned_txs = [tx for block in orphaned_blocks for tx in block.transactions]

        self.blocks = self.blocks[:from_number]
        for tx_hash in orphaned_txs:
            self.receipts.pop(tx_hash, None)

        # Recompute mined nonces from what is left, rather than adjusting them.
        # Those transactions are no longer mined, and rebuilding from the
        # surviving blocks cannot drift the way an incremental fix-up can.
        self.mined_nonces = {}
        for block in self.blocks:
            for tx_hash in block.transactions:
                mined = self._accepted.get(tx_hash)
                if mined is not None:
                    self.mined_nonces[mined.sender] = mined.nonce + 1

        # Contract storage is rolled back too. A real chain does this, and
        # skipping it here would leave an anchor recorded for a transaction that
        # no longer exists -- which would make a replay revert with
        # AlreadyAnchored and hide the bug the reorg test is looking for.
        self.item_anchors = {}
        self.batch_anchors = {}
        for block in self.blocks:
            for entry in block.logs:
                if not entry.topics:
                    continue
                if entry.topics[0] == ITEM_ANCHORED_TOPIC:
                    self.item_anchors[entry.topics[1]] = (
                        "0x" + entry.topics[3][-40:],
                        block.timestamp,
                    )
                elif entry.topics[0] == BATCH_ANCHORED_TOPIC:
                    leaf_count, issuer, _ = abi_decode(
                        ["uint32", "address", "uint256"], _hex_bytes(entry.data)
                    )
                    self.batch_anchors[entry.topics[1]] = (
                        normalise_hex(str(issuer)),
                        block.timestamp,
                        int(leaf_count),
                    )

        if return_to_mempool:
            for tx_hash in orphaned_txs:
                pending = self._accepted.get(tx_hash)
                if pending is None:
                    continue
                pending.submitted_at_height = len(self.blocks) - 1
                self.mempool[tx_hash] = pending

        # Replace the orphaned heights with empty blocks so the chain does not
        # get shorter. A shrinking chain would let a test pass by accident: a
        # missing block reads as "not found" rather than as a changed hash.
        for _ in orphaned_blocks:
            self._append_block(self._includable() if return_to_mempool else [])

        return orphaned_txs

    # --------------------------------------------------------------- mining

    @property
    def head(self) -> FakeBlock:
        return self.blocks[-1]

    def mine(self, count: int = 1) -> None:
        """Produce *count* blocks, including whatever the mempool allows."""
        for _ in range(count):
            self._append_block(self._includable())

    def _includable(self) -> list[PendingTx]:
        """Transactions eligible this block: past the delay, and next in nonce order."""
        height = len(self.blocks) - 1
        ready = [
            tx
            for tx in self.mempool.values()
            if height - tx.submitted_at_height >= self.mining_delay_blocks
        ]
        ready.sort(key=lambda tx: (tx.sender, tx.nonce))

        included: list[PendingTx] = []
        expected = dict(self.mined_nonces)
        # A nonce gap stops that sender's queue dead, which is the property the
        # gap-filling tests exist to observe.
        for tx in ready:
            if tx.nonce != expected.get(tx.sender, 0):
                continue
            included.append(tx)
            expected[tx.sender] = tx.nonce + 1
        return included

    def _append_block(self, transactions: list[PendingTx]) -> FakeBlock:
        number = len(self.blocks)
        block = FakeBlock(
            number=number,
            hash=self._block_hash(number),
            parent_hash=self.blocks[-1].hash,
            timestamp=1_700_000_000 + number * 2,
            transactions=[],
            logs=[],
        )
        self.blocks.append(block)

        for index, tx in enumerate(transactions):
            self.mempool.pop(tx.tx_hash, None)
            self.mined_nonces[tx.sender] = tx.nonce + 1
            block.transactions.append(tx.tx_hash)

            revert = self._evaluate(tx.sender, tx.to, tx.data)
            if revert is not None or self.force_revert:
                # Mined, gas burned, nothing done. Indistinguishable from
                # success at a glance, which is why it is worth testing.
                self.receipts[tx.tx_hash] = TxReceipt(
                    tx_hash=tx.tx_hash,
                    block_number=number,
                    block_hash=block.hash,
                    status=0,
                    gas_used=ANCHOR_GAS,
                    logs=(),
                )
                continue

            logs = self._apply(tx, block, index)
            block.logs.extend(logs)
            self.receipts[tx.tx_hash] = TxReceipt(
                tx_hash=tx.tx_hash,
                block_number=number,
                block_hash=block.hash,
                status=1,
                gas_used=ANCHOR_GAS,
                logs=tuple(logs),
            )
        return block

    def _block_hash(self, number: int) -> str:
        return normalise_hex(keccak256(f"block:{number}:{self._hash_salt}".encode()))

    # ------------------------------------------------------ contract state

    def _evaluate(self, sender: str, to: str, data: str) -> bytes | None:
        """Return encoded revert data if this call would revert, else ``None``."""
        if normalise_hex(to) != self.contract_address:
            return None
        payload = _hex_bytes(data)
        if len(payload) < 4:
            return None

        head = normalise_hex(payload[:4])
        if self.writers and normalise_hex(sender) not in self.writers:
            return NOT_WRITER_SELECTOR

        if head == ANCHOR_ITEM_SELECTOR:
            item_hash = normalise_hex(payload[4:36])
            if item_hash == ZERO_BYTES32:
                return ZERO_HASH_SELECTOR
            if item_hash in self.item_anchors:
                return ALREADY_ANCHORED_SELECTOR + abi_encode(
                    ["bytes32"], [payload[4:36]]
                )
            return None

        if head == ANCHOR_BATCH_SELECTOR:
            root = normalise_hex(payload[4:36])
            if root == ZERO_BYTES32:
                return ZERO_HASH_SELECTOR
            if root in self.batch_anchors:
                return ALREADY_ANCHORED_SELECTOR + abi_encode(["bytes32"], [payload[4:36]])
            return None

        return None

    def _apply(self, tx: PendingTx, block: FakeBlock, index: int) -> list[LogEntry]:
        """Mutate registry state and emit the logs the contract would emit."""
        if normalise_hex(tx.to) != self.contract_address:
            return []
        payload = _hex_bytes(tx.data)
        if len(payload) < 4:
            return []
        head = normalise_hex(payload[:4])

        if head == ANCHOR_ITEM_SELECTOR:
            item_hash = normalise_hex(payload[4:36])
            issuer_hash = normalise_hex(payload[36:68])
            self.item_anchors[item_hash] = (tx.sender, block.timestamp)
            return [
                LogEntry(
                    address=self.contract_address,
                    topics=(
                        ITEM_ANCHORED_TOPIC,
                        item_hash,
                        issuer_hash,
                        _address_topic(tx.sender),
                    ),
                    data=normalise_hex(abi_encode(["uint256"], [block.timestamp])),
                    block_number=block.number,
                    block_hash=block.hash,
                    tx_hash=tx.tx_hash,
                    log_index=index,
                )
            ]

        if head == ANCHOR_BATCH_SELECTOR:
            root = normalise_hex(payload[4:36])
            leaf_count = int.from_bytes(payload[36:68], "big")
            self.batch_anchors[root] = (tx.sender, block.timestamp, leaf_count)
            return [
                LogEntry(
                    address=self.contract_address,
                    topics=(BATCH_ANCHORED_TOPIC, root),
                    data=normalise_hex(
                        abi_encode(
                            ["uint32", "address", "uint256"],
                            [leaf_count, tx.sender, block.timestamp],
                        )
                    ),
                    block_number=block.number,
                    block_hash=block.hash,
                    tx_hash=tx.tx_hash,
                    log_index=index,
                )
            ]

        return []

    # ------------------------------------------------------------ ChainRpc

    def _tick(self, method: str) -> None:
        """Count the call and honour any injected transient failure."""
        self.call_log.append(method)
        if self.fail_calls_remaining > 0:
            self.fail_calls_remaining -= 1
            raise TransientRpcError(f"injected transient failure on {method}")

    async def chain_id(self) -> int:
        self._tick("eth_chainId")
        return self._chain_id

    async def block_number(self) -> int:
        self._tick("eth_blockNumber")
        return self.head.number

    async def get_block(self, identifier: int | str) -> BlockSummary | None:
        self._tick("eth_getBlockByNumber")
        if identifier in ("latest", "pending"):
            block = self.head
        elif isinstance(identifier, int):
            if identifier < 0 or identifier >= len(self.blocks):
                return None
            block = self.blocks[identifier]
        else:
            return None
        return BlockSummary(
            number=block.number,
            hash=block.hash,
            parent_hash=block.parent_hash,
            timestamp=block.timestamp,
        )

    async def get_transaction_count(self, address: str, block: str = "pending") -> int:
        self._tick("eth_getTransactionCount")
        sender = normalise_hex(address)
        mined = self.mined_nonces.get(sender, 0)
        if block != "pending":
            return mined
        queued = [tx.nonce for tx in self.mempool.values() if tx.sender == sender]
        # 'pending' counts contiguous queued nonces on top of the mined count,
        # the same way a node does. A gap stops the count, which is what makes
        # a hole visible to the gap detector.
        nonce = mined
        queued_set = set(queued)
        while nonce in queued_set:
            nonce += 1
        return nonce

    async def get_transaction_receipt(self, tx_hash: str) -> TxReceipt | None:
        self._tick("eth_getTransactionReceipt")
        return self.receipts.get(normalise_hex(tx_hash))

    async def transaction_exists(self, tx_hash: str) -> bool:
        self._tick("eth_getTransactionByHash")
        key = normalise_hex(tx_hash)
        return key in self.mempool or key in self.receipts

    async def send_raw_transaction(self, raw: bytes) -> str:
        self.call_log.append("eth_sendRawTransaction")
        if self.fail_sends_remaining > 0:
            self.fail_sends_remaining -= 1
            if self.fail_sends_ambiguous:
                raise AmbiguousSendError("injected timeout after submission")
            raise TransientRpcError("injected connection failure before submission")

        parsed = _decode_signed(raw)
        tx_hash = normalise_hex(keccak256(bytes(raw)))

        if tx_hash in self.known:
            # Not transient: retrying cannot change it, and the writer reads
            # this message as "the node already holds this transaction".
            raise RpcError("already known")
        self.known.add(tx_hash)

        # Read directly rather than through get_transaction_count: that method
        # ticks the failure injector, and a send must not be derailed halfway by
        # an injected read failure.
        expected = self.mined_nonces.get(parsed["sender"], 0)
        if parsed["nonce"] < expected:
            raise RpcError(f"nonce too low: next nonce {expected}")

        if self.reject_duplicate_nonce:
            clash = next(
                (
                    tx
                    for tx in self.mempool.values()
                    if tx.sender == parsed["sender"] and tx.nonce == parsed["nonce"]
                ),
                None,
            )
            if clash is not None:
                # A node accepts a same-nonce replacement only with a large
                # enough fee bump, and rejects it otherwise. Getting this wrong
                # in the fake would make the replace-by-fee tests meaningless.
                required = clash.max_fee_per_gas * 11_250 // 10_000
                if parsed["max_fee_per_gas"] < required:
                    raise RpcError("replacement transaction underpriced")
                self.mempool.pop(clash.tx_hash, None)

        self.mempool[tx_hash] = PendingTx(
            tx_hash=tx_hash,
            sender=parsed["sender"],
            nonce=parsed["nonce"],
            to=parsed["to"],
            data=parsed["data"],
            value=parsed["value"],
            max_fee_per_gas=parsed["max_fee_per_gas"],
            max_priority_fee_per_gas=parsed["max_priority_fee_per_gas"],
            submitted_at_height=len(self.blocks) - 1,
        )
        self._accepted[tx_hash] = self.mempool[tx_hash]
        return tx_hash

    async def estimate_gas(self, tx: dict[str, Any]) -> int:
        self._tick("eth_estimateGas")
        revert = self._evaluate(
            normalise_hex(str(tx.get("from", "0x" + "00" * 20))),
            str(tx.get("to", "")),
            str(tx.get("data", "0x")),
        )
        if revert is not None:
            raise ContractRevert(data=normalise_hex(revert), message="execution reverted")
        return ANCHOR_GAS

    async def call(self, tx: dict[str, Any]) -> bytes:
        self._tick("eth_call")
        sender = normalise_hex(str(tx.get("from", "0x" + "00" * 20)))
        to = str(tx.get("to", ""))
        data = str(tx.get("data", "0x"))

        payload = _hex_bytes(data)
        if len(payload) >= 4:
            head = normalise_hex(payload[:4])
            if head == IS_ITEM_ANCHORED_SELECTOR:
                anchored = normalise_hex(payload[4:36]) in self.item_anchors
                return abi_encode(["bool"], [anchored])
            if head == IS_BATCH_ANCHORED_SELECTOR:
                anchored = normalise_hex(payload[4:36]) in self.batch_anchors
                return abi_encode(["bool"], [anchored])

        revert = self._evaluate(sender, to, data)
        if revert is not None:
            raise ContractRevert(data=normalise_hex(revert), message="execution reverted")
        return b""

    async def fee_data(self) -> FeeData:
        self._tick("eth_feeHistory")
        return FeeData(
            base_fee_per_gas=self.base_fee_per_gas,
            max_priority_fee_per_gas=self.priority_fee_per_gas,
        )

    async def get_logs(
        self,
        address: str,
        topics: list[Any],
        from_block: int,
        to_block: int,
    ) -> list[LogEntry]:
        self._tick("eth_getLogs")
        wanted: set[str] | None = None
        if topics and topics[0] is not None:
            first = topics[0]
            wanted = {normalise_hex(t) for t in (first if isinstance(first, list) else [first])}

        target = normalise_hex(address)
        found: list[LogEntry] = []
        for block in self.blocks:
            if block.number < from_block or block.number > to_block:
                continue
            for entry in block.logs:
                if normalise_hex(entry.address) != target:
                    continue
                if wanted is not None and (not entry.topics or entry.topics[0] not in wanted):
                    continue
                found.append(entry)
        return found


def _address_topic(address: str) -> str:
    """An address as a 32-byte topic: 20 bytes right-aligned in 32."""
    raw = _hex_bytes(address)
    return normalise_hex(b"\x00" * (32 - len(raw)) + raw)


def _hex_bytes(value: str) -> bytes:
    cleaned = value[2:] if value.startswith(("0x", "0X")) else value
    if len(cleaned) % 2:
        cleaned = "0" + cleaned
    return bytes.fromhex(cleaned)


def _decode_signed(raw: bytes) -> dict[str, Any]:
    """Decode an EIP-1559 signed transaction envelope.

    ``0x02 || rlp([chainId, maxPriorityFee, maxFee, gas, to, value, data,
    accessList, v, r, s])`` -- decoded by hand rather than with a private
    eth-account helper, so this keeps working across library versions.
    """
    if not raw or raw[0] != 0x02:
        raise ValueError("the fake chain only accepts EIP-1559 (type 2) transactions")
    fields = rlp.decode(raw[1:])
    return {
        "sender": normalise_hex(Account.recover_transaction(raw)),
        "chain_id": int.from_bytes(fields[0], "big"),
        "nonce": int.from_bytes(fields[1], "big"),
        "max_priority_fee_per_gas": int.from_bytes(fields[2], "big"),
        "max_fee_per_gas": int.from_bytes(fields[3], "big"),
        "gas": int.from_bytes(fields[4], "big"),
        "to": normalise_hex(fields[5]) if fields[5] else "0x",
        "value": int.from_bytes(fields[6], "big"),
        "data": normalise_hex(fields[7]) if fields[7] else "0x",
    }
