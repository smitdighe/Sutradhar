"""The only door between this codebase and an EVM JSON-RPC endpoint.

Everything that talks to a chain goes through :class:`ChainClient`. Nothing else
constructs a provider, counts compute units, or decides whether a failure is
worth retrying, because those three decisions have to be made the same way every
time or the failure modes stop being predictable.

Four properties this module exists to guarantee:

**Booting does not require a chain.** If the RPC endpoint is unreachable at
startup the application still serves traffic; ``/readyz`` reports the chain as
down and the outbox simply does not drain. A provenance API that refuses to
accept a registration because a testnet is having a bad afternoon has confused a
dependency for a prerequisite.

**Retries never resend a transaction that may already exist.** A connection
error raised *before* the request reached the node means nothing was submitted
and retrying is free. A timeout raised *after* submission means the node may
hold that transaction already, and resending it at the same nonce is how one
logical anchor becomes two competing transactions. Those two cases get different
exception types -- :class:`TransientRpcError` and :class:`AmbiguousSendError` --
and only the first is retried. The second is resolved by polling for a receipt,
which is the only source of truth about whether a transaction exists.

**The quota ceiling is never silently crossed.** Every call is priced in Alchemy
compute units and metered. Past the budget, reads fall back to the last known
value and say so; writes refuse with 503. Neither crashes, and neither keeps
spending.

**Types leave web3 at the boundary.** The rest of the package sees the frozen
dataclasses below, never ``AttributeDict``/``HexBytes``. That is what lets
``tests/fakes/fake_chain.py`` be a real substitute rather than a mock of a mock.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

import tenacity
from eth_utils.address import to_checksum_address

from app.config import Settings, get_settings
from app.core.clock import now
from app.core.errors import ErrorCode, UnavailableError
from app.core.logging import get_logger
from app.core.quota import QuotaTracker

__all__ = [
    "ALCHEMY_CU_COSTS",
    "AmbiguousSendError",
    "BlockSummary",
    "ChainClient",
    "ChainRpc",
    "ChainUnavailable",
    "ContractRevert",
    "FeeData",
    "LogEntry",
    "QuotaMeter",
    "RpcError",
    "TransientRpcError",
    "TxReceipt",
    "TxStatus",
    "Web3Rpc",
    "build_client",
    "normalise_hex",
]

logger = get_logger(__name__)

# Alchemy prices each method in compute units. These track the published table
# closely enough to keep a free tier honest; they are an accounting estimate,
# not a billing oracle, and drift in the vendor's table only shifts how early
# the backpressure kicks in.
ALCHEMY_CU_COSTS: dict[str, int] = {
    "eth_chainId": 0,
    "eth_blockNumber": 10,
    "eth_getBlockByNumber": 16,
    "eth_getBlockByHash": 21,
    "eth_getTransactionCount": 26,
    "eth_getTransactionByHash": 17,
    "eth_getTransactionReceipt": 15,
    "eth_call": 26,
    "eth_estimateGas": 87,
    "eth_feeHistory": 10,
    "eth_gasPrice": 10,
    "eth_maxPriorityFeePerGas": 10,
    "eth_getLogs": 75,
    "eth_sendRawTransaction": 250,
}

DEFAULT_CU_COST = 20

# Reads whose last known value is still worth serving once the budget is spent.
# A block number that is a few minutes old, labelled stale, is more useful than
# an exception; a receipt that is a few minutes old is not, because its absence
# is exactly what the caller is asking about.
CACHEABLE_READS = frozenset({"chain_id", "block_number", "get_block"})

# Ceiling on the startup reachability probe. Nothing is served until the
# lifespan finishes, so this is downtime, not patience.
STARTUP_PROBE_SECONDS = 5.0


# --------------------------------------------------------------------- errors


class RpcError(Exception):
    """The node answered, and the answer was a permanent failure."""


class TransientRpcError(RpcError):
    """The request never reached the node, or the node asked to be retried.

    Safe to retry for *any* method, including a send: nothing was submitted.
    """


class ContractRevert(RpcError):
    """The call reached the contract and the contract rejected it.

    Permanent by construction: the same calldata against the same state reverts
    the same way every time, so retrying is pure waste. ``data`` carries the
    ABI-encoded reason when the node returned one, which is what lets
    ``app.chain.contract.decode_revert`` distinguish "already anchored" -- a
    completed job -- from "not a writer", which is a misconfiguration.
    """

    def __init__(self, data: str | None = None, message: str = "") -> None:
        super().__init__(message or "execution reverted")
        self.data = data


class AmbiguousSendError(RpcError):
    """A send whose fate is unknown -- it may or may not have been accepted.

    Never retried. The nonce is already committed to whatever the node did with
    it, and a second send at that nonce would either be rejected as a duplicate
    or, worse, replace a transaction that was going to be mined. Resolution is
    by receipt polling, never by resending.
    """


class ChainUnavailable(UnavailableError):
    """A chain operation could not be served. Maps to 503, never 500."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(code=ErrorCode.CHAIN_UNAVAILABLE, message=message, details=details)


# ---------------------------------------------------------------------- types


class TxStatus:
    """EVM receipt status. Not an enum -- these are the literal on-chain values."""

    REVERTED = 0
    SUCCESS = 1


@dataclass(frozen=True, slots=True)
class BlockSummary:
    """The only parts of a block this system reads."""

    number: int
    hash: str
    parent_hash: str
    timestamp: int


@dataclass(frozen=True, slots=True)
class LogEntry:
    """One emitted event, pre-decoding.

    ``(tx_hash, log_index)`` is the identity the indexer upserts on, which is
    what makes replaying a block range idempotent.
    """

    address: str
    topics: tuple[str, ...]
    data: str
    block_number: int
    block_hash: str
    tx_hash: str
    log_index: int


@dataclass(frozen=True, slots=True)
class TxReceipt:
    """A mined transaction's receipt.

    ``status == 0`` is a *reverted* transaction: it was mined, it consumed gas,
    and it did nothing. Treating that as success is the single easiest way to
    tell a weaver their record is anchored when the chain rejected it.
    """

    tx_hash: str
    block_number: int
    block_hash: str
    status: int
    gas_used: int
    logs: tuple[LogEntry, ...] = ()

    @property
    def reverted(self) -> bool:
        return self.status == TxStatus.REVERTED


@dataclass(frozen=True, slots=True)
class FeeData:
    """Current EIP-1559 fee conditions."""

    base_fee_per_gas: int
    max_priority_fee_per_gas: int


# ----------------------------------------------------------------- normaliser


def normalise_hex(value: Any) -> str:
    """Render bytes, ``HexBytes``, ints or strings as ``0x``-prefixed lowercase hex.

    ``HexBytes.hex()`` dropped its ``0x`` prefix between hexbytes 0.x and 1.x, so
    calling ``.hex()`` directly produces a value that compares unequal to the
    same hash read from a different library version. Everything crossing this
    boundary is normalised here instead.
    """
    if isinstance(value, str):
        return value.lower() if value.startswith("0x") else "0x" + value.lower()
    if isinstance(value, bytes | bytearray):
        return "0x" + bytes(value).hex()
    if isinstance(value, int):
        return hex(value)
    raise TypeError(f"cannot render {type(value).__name__} as hex")


def _as_int(value: Any) -> int:
    """Coerce a JSON-RPC quantity, which may arrive as int or as a hex string."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 16) if value.startswith(("0x", "0X")) else int(value)
    raise TypeError(f"cannot read {type(value).__name__} as an integer quantity")


# ------------------------------------------------------------------- protocol


class ChainRpc(Protocol):
    """The narrow slice of JSON-RPC this system actually uses.

    Deliberately small. Every method here has to be implemented twice -- once
    over web3, once in the fake -- so anything that does not earn its place makes
    the fake harder to trust.
    """

    async def chain_id(self) -> int: ...

    async def block_number(self) -> int: ...

    async def get_block(self, identifier: int | str) -> BlockSummary | None: ...

    async def get_transaction_count(self, address: str, block: str = "pending") -> int: ...

    async def get_transaction_receipt(self, tx_hash: str) -> TxReceipt | None: ...

    async def transaction_exists(self, tx_hash: str) -> bool: ...

    async def send_raw_transaction(self, raw: bytes) -> str: ...

    async def estimate_gas(self, tx: dict[str, Any]) -> int: ...

    async def call(self, tx: dict[str, Any]) -> bytes: ...

    async def fee_data(self) -> FeeData: ...

    async def get_logs(
        self,
        address: str,
        topics: list[str | None],
        from_block: int,
        to_block: int,
    ) -> list[LogEntry]: ...


# ------------------------------------------------------------------ web3 impl


def _classify(exc: BaseException, *, sending: bool) -> RpcError:
    """Sort a provider exception into retryable, ambiguous, or permanent.

    The distinction only matters for sends, but it is drawn the same way for
    every method so there is one rule to reason about rather than two.
    """
    from web3.exceptions import ContractLogicError, Web3RPCError

    if isinstance(exc, RpcError):
        # Already classified -- a fake, or an inner call that raised our own type.
        return exc

    if isinstance(exc, ContractLogicError):
        # The call reached the contract and was rejected. Never retryable, and
        # never ambiguous: a revert consumed nothing and changed nothing.
        data = getattr(exc, "data", None)
        return ContractRevert(
            data=data if isinstance(data, str) else None,
            message=str(exc),
        )

    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
        # A timeout on a send is the dangerous case: the node may be holding the
        # transaction already. On a read it is merely slow.
        return AmbiguousSendError(str(exc)) if sending else TransientRpcError(str(exc))

    if isinstance(exc, ConnectionError | ConnectionRefusedError | OSError):
        # Never reached the node. Nothing was submitted, whatever the method.
        return TransientRpcError(str(exc))

    if isinstance(exc, Web3RPCError):
        message = str(exc).lower()
        if any(hint in message for hint in ("rate limit", "too many requests", "429")):
            return TransientRpcError(str(exc))
        if any(hint in message for hint in ("timeout", "503", "502", "service unavailable")):
            return TransientRpcError(str(exc))
        return RpcError(str(exc))

    return RpcError(f"{type(exc).__name__}: {exc}")


class Web3Rpc:
    """:class:`ChainRpc` over ``web3.AsyncWeb3``.

    Thin on purpose: translate types, translate exceptions, nothing else. Policy
    -- retries, metering, degradation -- lives in :class:`ChainClient`, so the
    fake does not have to reimplement any of it to be a faithful stand-in.
    """

    def __init__(self, rpc_url: str, timeout_seconds: int = 20) -> None:
        from web3 import AsyncHTTPProvider, AsyncWeb3

        self.rpc_url = rpc_url
        provider = AsyncHTTPProvider(rpc_url, request_kwargs={"timeout": timeout_seconds})
        self._w3 = AsyncWeb3(provider)

    async def _guard(self, coro: Any, *, sending: bool = False) -> Any:
        """Translate provider exceptions, and only provider exceptions.

        ``Exception``, deliberately not ``BaseException``.
        ``asyncio.CancelledError`` is a ``BaseException`` and must propagate
        untouched: catching it turns a cancelled call into an application error,
        which defeats every timeout wrapped around this and makes shutdown hang
        while the caller retries something the event loop already gave up on.
        """
        try:
            return await coro
        except Exception as exc:  # re-raised below, classified
            raise _classify(exc, sending=sending) from exc

    async def chain_id(self) -> int:
        return int(await self._guard(self._w3.eth.chain_id))

    async def block_number(self) -> int:
        return int(await self._guard(self._w3.eth.block_number))

    async def get_block(self, identifier: int | str) -> BlockSummary | None:
        from web3.exceptions import BlockNotFound

        try:
            block = await self._guard(self._w3.eth.get_block(identifier))  # type: ignore[arg-type]
        except BlockNotFound:
            return None
        except RpcError as exc:
            # A pruned or reorged-away height comes back as "not found" on some
            # nodes and as an error on others. Absent is absent either way, and
            # the caller reads absence as "this height no longer holds what we
            # recorded", which is the correct reorg conclusion.
            if "not found" in str(exc).lower():
                return None
            raise
        if block is None:
            return None
        return BlockSummary(
            number=_as_int(block["number"]),
            hash=normalise_hex(block["hash"]),
            parent_hash=normalise_hex(block["parentHash"]),
            timestamp=_as_int(block["timestamp"]),
        )

    async def get_transaction_count(self, address: str, block: str = "pending") -> int:
        checksummed = to_checksum_address(address)
        return int(await self._guard(self._w3.eth.get_transaction_count(checksummed, block)))  # type: ignore[arg-type]

    async def get_transaction_receipt(self, tx_hash: str) -> TxReceipt | None:
        from web3.exceptions import TransactionNotFound

        try:
            receipt = await self._guard(self._w3.eth.get_transaction_receipt(tx_hash))  # type: ignore[arg-type]
        except TransactionNotFound:
            return None
        if receipt is None:
            return None
        return TxReceipt(
            tx_hash=normalise_hex(receipt["transactionHash"]),
            block_number=_as_int(receipt["blockNumber"]),
            block_hash=normalise_hex(receipt["blockHash"]),
            status=_as_int(receipt.get("status", 1)),
            gas_used=_as_int(receipt.get("gasUsed", 0)),
            logs=tuple(_log_from_web3(entry) for entry in receipt.get("logs", ())),
        )

    async def transaction_exists(self, tx_hash: str) -> bool:
        """True when the node knows this transaction, mined or still pending.

        Gap detection needs to distinguish "sent and waiting in the mempool"
        from "never sent, and the nonce is now a hole".
        """
        from web3.exceptions import TransactionNotFound

        try:
            await self._guard(self._w3.eth.get_transaction(tx_hash))  # type: ignore[arg-type]
        except TransactionNotFound:
            return False
        return True

    async def send_raw_transaction(self, raw: bytes) -> str:
        result = await self._guard(self._w3.eth.send_raw_transaction(raw), sending=True)
        return normalise_hex(result)

    async def estimate_gas(self, tx: dict[str, Any]) -> int:
        return int(await self._guard(self._w3.eth.estimate_gas(tx)))  # type: ignore[arg-type]

    async def call(self, tx: dict[str, Any]) -> bytes:
        result = await self._guard(self._w3.eth.call(tx))  # type: ignore[arg-type]
        return bytes(result)

    async def fee_data(self) -> FeeData:
        latest = await self._guard(self._w3.eth.get_block("latest"))
        base_fee = _as_int(latest.get("baseFeePerGas", 0)) if latest else 0
        try:
            tip = int(await self._guard(self._w3.eth.max_priority_fee))
        except RpcError as exc:
            # Not every node implements eth_maxPriorityFeePerGas. 1 gwei is the
            # conventional floor and is logged, so a silently wrong tip on a
            # congested chain is at least visible in the trace.
            logger.info("chain.fee_data.tip_fallback", error=str(exc))
            tip = 1_000_000_000
        return FeeData(base_fee_per_gas=base_fee, max_priority_fee_per_gas=tip)

    async def get_logs(
        self,
        address: str,
        topics: list[str | None],
        from_block: int,
        to_block: int,
    ) -> list[LogEntry]:
        params: dict[str, Any] = {
            "address": to_checksum_address(address),
            "fromBlock": from_block,
            "toBlock": to_block,
        }
        if topics:
            params["topics"] = topics
        entries = await self._guard(self._w3.eth.get_logs(params))  # type: ignore[arg-type]
        return [_log_from_web3(entry) for entry in entries]


def _log_from_web3(entry: Any) -> LogEntry:
    return LogEntry(
        address=normalise_hex(entry["address"]),
        topics=tuple(normalise_hex(topic) for topic in entry["topics"]),
        data=normalise_hex(entry["data"]),
        block_number=_as_int(entry["blockNumber"]),
        block_hash=normalise_hex(entry["blockHash"]),
        tx_hash=normalise_hex(entry["transactionHash"]),
        log_index=_as_int(entry["logIndex"]),
    )


# --------------------------------------------------------------------- meter


class QuotaMeter:
    """Compute-unit accounting with a write-behind buffer.

    The naive version -- one ``UPDATE quota_usage`` per RPC call -- puts a
    Postgres round trip in front of every ``eth_getBlockByNumber`` in a loop that
    runs every five seconds. Instead usage accrues in process and is flushed on a
    unit threshold or a time threshold, and the remaining budget is decremented
    locally between flushes.

    The cost is a bounded overshoot: a hard kill can lose up to one flush window
    of accounting, and the ceiling can be crossed by at most the flush threshold
    before the meter notices. Both are stated rather than hidden, and both are
    small next to a monthly budget in the hundreds of millions.
    """

    def __init__(
        self,
        tracker: QuotaTracker,
        flush_units: int = 500,
        flush_seconds: int = 30,
    ) -> None:
        self._tracker = tracker
        self._flush_units = flush_units
        self._flush_seconds = flush_seconds
        self._pending = 0
        self._last_flush = now()
        self._remaining: Decimal | None = None
        self._lock = asyncio.Lock()

    @property
    def pending_units(self) -> int:
        """Units recorded but not yet written to Postgres."""
        return self._pending

    def cost(self, method: str) -> int:
        return ALCHEMY_CU_COSTS.get(method, DEFAULT_CU_COST)

    async def remaining(self) -> Decimal:
        """Budget left, refreshed from Postgres on first use then tracked locally."""
        if self._remaining is None:
            self._remaining = await self._tracker.remaining()
        return max(Decimal(0), self._remaining - Decimal(self._pending))

    async def would_exceed(self, method: str) -> bool:
        """True when charging *method* now would cross the budget."""
        return Decimal(self.cost(method)) > await self.remaining()

    async def record(self, method: str) -> None:
        """Charge one call, flushing if either threshold has been reached."""
        self._pending += self.cost(method)
        elapsed = (now() - self._last_flush).total_seconds()
        if self._pending >= self._flush_units or elapsed >= self._flush_seconds:
            await self.flush()

    async def flush(self) -> None:
        """Write accrued units to Postgres and refresh the remaining budget."""
        async with self._lock:
            amount, self._pending = self._pending, 0
            self._last_flush = now()
            if amount <= 0:
                self._remaining = await self._tracker.remaining()
                return
            await self._tracker.consume(amount)
            self._remaining = await self._tracker.remaining()


# -------------------------------------------------------------------- client


class ChainClient:
    """Metered, retrying, degradation-aware access to one chain.

    Construct with :func:`build_client` in application code; the constructor
    takes an explicit :class:`ChainRpc` so tests can hand it a fake.
    """

    def __init__(
        self,
        rpc: ChainRpc,
        meter: QuotaMeter | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._rpc = rpc
        self._meter = meter
        self._settings = settings or get_settings()
        self._cache: dict[str, tuple[Any, datetime]] = {}
        self._degraded = False
        self._available = False
        self._last_error: str | None = None

    # ------------------------------------------------------------- lifecycle

    @property
    def available(self) -> bool:
        """Whether the last probe reached the node. Never gates construction."""
        return self._available

    @property
    def degraded(self) -> bool:
        """True once any read has been served from cache instead of the chain."""
        return self._degraded

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def connect(self, timeout: float = STARTUP_PROBE_SECONDS) -> bool:
        """Probe the endpoint. Returns reachability; never raises.

        One attempt, hard-bounded. The normal retry policy is right for a worker
        that has time to spare and wrong here: uvicorn does not accept a single
        request until the lifespan finishes, so five retries with backoff against
        a dead endpoint turn a degraded dependency into a minute of downtime on
        every deploy. The answer this probe wants is "is it up right now", and
        "no" is a perfectly good answer -- the outbox simply does not drain, and
        ``/readyz`` says so.
        """
        try:
            if self._meter is not None:
                await self._meter.record("eth_chainId")
            reported = int(await asyncio.wait_for(self._rpc.chain_id(), timeout))
        except Exception as exc:  # noqa: BLE001 - startup must not be gated on a chain
            self._available = False
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "chain.connect.unreachable",
                rpc_url=self._settings.chain_rpc_url,
                error=self._last_error,
                consequence="API serves normally; outbox will not drain until the chain returns",
            )
            return False

        self._available = True
        self._last_error = None
        self._cache["chain_id"] = (reported, now())
        if reported != self._settings.chain_id:
            logger.error(
                "chain.connect.chain_id_mismatch",
                configured=self._settings.chain_id,
                reported=reported,
                consequence="anchors would be written to the wrong network; writes stay refused",
            )
            self._available = False
            self._last_error = (
                f"chain id mismatch: rpc={reported} configured={self._settings.chain_id}"
            )
            return False

        logger.info("chain.connect.ok", chain_id=reported, rpc_url=self._settings.chain_rpc_url)
        return True

    async def flush_quota(self) -> None:
        """Persist any accrued compute units. Called on shutdown."""
        if self._meter is not None:
            await self._meter.flush()

    # ----------------------------------------------------------------- guts

    def _retry_policy(self, *, retryable: type[BaseException]) -> tenacity.AsyncRetrying:
        return tenacity.AsyncRetrying(
            retry=tenacity.retry_if_exception_type(retryable),
            stop=tenacity.stop_after_attempt(self._settings.chain_rpc_max_retries),
            wait=tenacity.wait_exponential_jitter(initial=0.25, max=8.0, jitter=0.25),
            reraise=True,
        )

    async def _invoke(
        self,
        method: str,
        operation: Any,
        *,
        cache_key: str | None = None,
        sending: bool = False,
    ) -> Any:
        """Meter, retry, time and log one RPC call.

        *operation* is a zero-argument coroutine function rather than a coroutine
        so each retry attempt builds a fresh awaitable.
        """
        if self._meter is not None and await self._meter.would_exceed(method):
            return await self._on_budget_exhausted(method, cache_key)

        # A send is retried only on TransientRpcError, which by construction
        # means the request never reached the node. AmbiguousSendError is not
        # retryable and propagates for receipt polling to resolve.
        retryable: type[BaseException] = TransientRpcError
        started = asyncio.get_running_loop().time()
        attempts = 0

        async for attempt in self._retry_policy(retryable=retryable):
            with attempt:
                attempts += 1
                if self._meter is not None:
                    await self._meter.record(method)
                result = await operation()
                elapsed_ms = round((asyncio.get_running_loop().time() - started) * 1000, 2)
                logger.debug(
                    "chain.rpc",
                    method=method,
                    attempts=attempts,
                    latency_ms=elapsed_ms,
                    stale=False,
                )
                self._available = True
                if cache_key is not None:
                    self._cache[cache_key] = (result, now())
                return result

        # tenacity with reraise=True always exits via the exception, so this is
        # unreachable; it exists so the function has no implicit None return.
        raise RpcError(f"{method} exhausted retries without raising")

    async def _on_budget_exhausted(self, method: str, cache_key: str | None) -> Any:
        """Serve a stale read, or refuse a write, once the CU budget is spent."""
        if cache_key is not None and cache_key in self._cache:
            value, as_of = self._cache[cache_key]
            self._degraded = True
            logger.warning(
                "chain.rpc.stale",
                method=method,
                stale=True,
                as_of=as_of.isoformat(),
                reason="alchemy compute-unit budget exhausted",
            )
            return value

        self._degraded = True
        logger.error("chain.rpc.refused", method=method, reason="compute-unit budget exhausted")
        raise ChainUnavailable(
            "chain compute-unit budget exhausted",
            details={"method": method, "quota": "alchemy_cu"},
        )

    def _assert_writes_enabled(self) -> None:
        """Refuse a write the configuration has switched off.

        ``CHAIN_WRITE_ENABLED=false`` is not an error state. It is the switch
        that keeps registration working when the RPC is dead or unfunded: the
        outbox keeps filling, items stay honestly PENDING, and nothing is sent.
        """
        if not self._settings.chain_write_enabled:
            raise ChainUnavailable(
                "chain writes are disabled (CHAIN_WRITE_ENABLED=false)",
                details={"reason": "writes_disabled"},
            )

    # ---------------------------------------------------------------- reads

    async def chain_id(self) -> int:
        return int(await self._invoke("eth_chainId", self._rpc.chain_id, cache_key="chain_id"))

    async def block_number(self) -> int:
        return int(
            await self._invoke("eth_blockNumber", self._rpc.block_number, cache_key="block_number")
        )

    async def get_block(self, identifier: int | str) -> BlockSummary | None:
        result = await self._invoke(
            "eth_getBlockByNumber",
            lambda: self._rpc.get_block(identifier),
            cache_key=f"get_block:{identifier}" if identifier != "latest" else None,
        )
        return result if result is None else result

    async def get_transaction_count(self, address: str, block: str = "pending") -> int:
        return int(
            await self._invoke(
                "eth_getTransactionCount",
                lambda: self._rpc.get_transaction_count(address, block),
            )
        )

    async def get_transaction_receipt(self, tx_hash: str) -> TxReceipt | None:
        result = await self._invoke(
            "eth_getTransactionReceipt",
            lambda: self._rpc.get_transaction_receipt(tx_hash),
        )
        assert result is None or isinstance(result, TxReceipt)
        return result

    async def transaction_exists(self, tx_hash: str) -> bool:
        return bool(
            await self._invoke(
                "eth_getTransactionByHash",
                lambda: self._rpc.transaction_exists(tx_hash),
            )
        )

    async def call(self, tx: dict[str, Any]) -> bytes:
        result = await self._invoke("eth_call", lambda: self._rpc.call(tx))
        return bytes(result)

    async def estimate_gas(self, tx: dict[str, Any]) -> int:
        return int(await self._invoke("eth_estimateGas", lambda: self._rpc.estimate_gas(tx)))

    async def fee_data(self) -> FeeData:
        result = await self._invoke("eth_feeHistory", self._rpc.fee_data, cache_key="fee_data")
        assert isinstance(result, FeeData)
        return result

    async def get_logs(
        self,
        address: str,
        topics: list[str | None],
        from_block: int,
        to_block: int,
    ) -> list[LogEntry]:
        result = await self._invoke(
            "eth_getLogs",
            lambda: self._rpc.get_logs(address, topics, from_block, to_block),
        )
        return list(result)

    # ---------------------------------------------------------------- write

    async def send_raw_transaction(self, raw: bytes) -> str:
        """Broadcast a signed transaction.

        Refuses when writes are disabled or the budget is spent, rather than
        attempting and failing halfway. A refusal here leaves the outbox row
        untouched and requeued; a half-attempt would burn a nonce.
        """
        self._assert_writes_enabled()
        result = await self._invoke(
            "eth_sendRawTransaction",
            lambda: self._rpc.send_raw_transaction(raw),
            sending=True,
        )
        return str(result)


def build_client(
    session_factory: Any,
    settings: Settings | None = None,
) -> ChainClient:
    """Assemble the production client: web3 over HTTP, metered against Alchemy's budget."""
    resolved = settings or get_settings()
    tracker = QuotaTracker(
        name="alchemy_cu",
        budget=resolved.alchemy_cu_monthly_budget,
        session_factory=session_factory,
        periodic=True,
    )
    meter = QuotaMeter(
        tracker,
        flush_units=resolved.chain_quota_flush_units,
        flush_seconds=resolved.chain_quota_flush_seconds,
    )
    rpc = Web3Rpc(resolved.chain_rpc_url, timeout_seconds=resolved.chain_rpc_timeout_seconds)
    return ChainClient(rpc, meter=meter, settings=resolved)
