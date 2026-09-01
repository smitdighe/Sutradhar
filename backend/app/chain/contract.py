"""Binding to the Sutradhar registry: calldata in, decoded events out.

Encoding is done with :mod:`eth_abi` and a hand-computed selector rather than a
``web3.contract.Contract``, for two reasons. It works with no provider attached,
which is what lets the fake chain and the offline tests exercise the real
encoding path; and it does not move when web3 renames its contract API between
majors, which it has done twice.

**The surface is asserted at load time.** :func:`load_contract` checks that the
deployed ABI really exposes ``anchorItem(bytes32,bytes32)``,
``anchorBatch(bytes32,uint32)`` and the two events, with those exact argument
types and indexing. A registry whose ABI drifted from what this package encodes
would not fail loudly -- it would encode calldata against a selector nothing
answers to, and every anchoring transaction would revert or, worse, land in a
fallback function. Failing at import is the cheap version of that discovery.

**Re-anchoring is success, not failure.** The contract reverts with
``AlreadyAnchored(bytes32)`` when a hash is already recorded. That is exactly
what happens when a reorg makes this system replay a job whose original
transaction was re-included, so :func:`decode_revert` identifies it and the
writer treats it as a completed anchor. Retrying it forever, or dead-lettering
it, would both be wrong.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from eth_abi.abi import decode as abi_decode, encode as abi_encode
from eth_utils.address import to_checksum_address

from app.chain.client import LogEntry, normalise_hex
from app.config import get_settings
from app.core.hashing import keccak256

__all__ = [
    "REQUIRED_EVENTS",
    "REQUIRED_FUNCTIONS",
    "BatchAnchoredEvent",
    "ContractBinding",
    "ContractSurfaceError",
    "DecodedRevert",
    "ItemAnchoredEvent",
    "decode_revert",
    "event_topic",
    "load_contract",
    "selector",
]

# (name, argument types) -- the exact surface this package encodes against.
REQUIRED_FUNCTIONS: dict[str, tuple[str, ...]] = {
    "anchorItem": ("bytes32", "bytes32"),
    "anchorBatch": ("bytes32", "uint32"),
}

# (name, [(type, indexed), ...]) -- indexing matters as much as type, because it
# decides whether a value arrives in a topic or in the data blob.
REQUIRED_EVENTS: dict[str, tuple[tuple[str, bool], ...]] = {
    "ItemAnchored": (
        ("bytes32", True),
        ("bytes32", True),
        ("address", True),
        ("uint256", False),
    ),
    "BatchAnchored": (
        ("bytes32", True),
        ("uint32", False),
        ("address", False),
        ("uint256", False),
    ),
}

# Solidity's built-in revert carriers, present in every contract.
ERROR_STRING_SELECTOR = "0x08c379a0"  # Error(string)
PANIC_SELECTOR = "0x4e487b71"  # Panic(uint256)


class ContractSurfaceError(RuntimeError):
    """The loaded ABI does not expose what this package encodes against."""


@dataclass(frozen=True, slots=True)
class ItemAnchoredEvent:
    """One decoded ``ItemAnchored`` log, plus where it was found."""

    item_hash: str
    issuer_hash: str
    issuer: str
    timestamp: int
    block_number: int
    block_hash: str
    tx_hash: str
    log_index: int


@dataclass(frozen=True, slots=True)
class BatchAnchoredEvent:
    """One decoded ``BatchAnchored`` log, plus where it was found."""

    root: str
    leaf_count: int
    issuer: str
    timestamp: int
    block_number: int
    block_hash: str
    tx_hash: str
    log_index: int


@dataclass(frozen=True, slots=True)
class DecodedRevert:
    """A revert reason recovered from returned error data."""

    name: str
    args: tuple[Any, ...]
    raw: str

    @property
    def is_already_anchored(self) -> bool:
        """True when the chain is telling us this anchor already exists.

        The writer reads this as a completed job. See the module docstring.
        """
        return self.name == "AlreadyAnchored"

    def __str__(self) -> str:
        if self.args:
            rendered = ", ".join(str(arg) for arg in self.args)
            return f"{self.name}({rendered})"
        return f"{self.name}()"


def signature(name: str, types: tuple[str, ...]) -> str:
    """Canonical Solidity signature, e.g. ``anchorItem(bytes32,bytes32)``."""
    return f"{name}({','.join(types)})"


def selector(name: str, types: tuple[str, ...]) -> bytes:
    """First four bytes of the keccak256 of the signature."""
    return keccak256(signature(name, types).encode("utf-8"))[:4]


def event_topic(name: str, types: tuple[str, ...]) -> str:
    """``topic0`` for an event: the full keccak256 of its signature."""
    return normalise_hex(keccak256(signature(name, types).encode("utf-8")))


def _abi_entries(abi: list[dict[str, Any]], kind: str) -> dict[str, dict[str, Any]]:
    return {entry["name"]: entry for entry in abi if entry.get("type") == kind}


def _assert_surface(abi: list[dict[str, Any]]) -> None:
    """Fail loudly when the ABI is not the one this package was written against."""
    functions = _abi_entries(abi, "function")
    events = _abi_entries(abi, "event")

    for name, expected in REQUIRED_FUNCTIONS.items():
        entry = functions.get(name)
        if entry is None:
            raise ContractSurfaceError(
                f"ABI exposes no function '{name}'; expected {signature(name, expected)}"
            )
        actual = tuple(argument["type"] for argument in entry.get("inputs", []))
        if actual != expected:
            raise ContractSurfaceError(
                f"function '{name}' is {signature(name, actual)}, "
                f"expected {signature(name, expected)}"
            )

    for name, expected_args in REQUIRED_EVENTS.items():
        entry = events.get(name)
        if entry is None:
            raise ContractSurfaceError(f"ABI exposes no event '{name}'")
        actual_args = tuple(
            (argument["type"], bool(argument.get("indexed", False)))
            for argument in entry.get("inputs", [])
        )
        if actual_args != expected_args:
            raise ContractSurfaceError(
                f"event '{name}' has arguments {actual_args}, expected {expected_args}"
            )


class ContractBinding:
    """Encodes calls to, and decodes events from, one deployed registry."""

    def __init__(self, address: str, abi: list[dict[str, Any]], bytecode: str = "") -> None:
        _assert_surface(abi)
        self.address = to_checksum_address(address)
        self.abi = abi
        self.bytecode = bytecode
        self._errors = {
            normalise_hex(
                selector(entry["name"], tuple(a["type"] for a in entry.get("inputs", [])))
            ): entry
            for entry in abi
            if entry.get("type") == "error"
        }

    # ------------------------------------------------------------- encoding

    def encode_anchor_item(self, item_hash: str, issuer_hash: str) -> bytes:
        """Calldata for ``anchorItem(bytes32,bytes32)``."""
        types = REQUIRED_FUNCTIONS["anchorItem"]
        return selector("anchorItem", types) + abi_encode(
            list(types), [_to_bytes32(item_hash), _to_bytes32(issuer_hash)]
        )

    def encode_anchor_batch(self, root: str, leaf_count: int) -> bytes:
        """Calldata for ``anchorBatch(bytes32,uint32)``."""
        if not 0 < leaf_count < 2**32:
            raise ValueError(f"leaf_count {leaf_count} does not fit uint32")
        types = REQUIRED_FUNCTIONS["anchorBatch"]
        return selector("anchorBatch", types) + abi_encode(
            list(types), [_to_bytes32(root), int(leaf_count)]
        )

    def encode_is_item_anchored(self, item_hash: str) -> bytes:
        """Calldata for the read used by reconciliation and verification."""
        return selector("isItemAnchored", ("bytes32",)) + abi_encode(
            ["bytes32"], [_to_bytes32(item_hash)]
        )

    def encode_is_batch_anchored(self, root: str) -> bytes:
        return selector("isBatchAnchored", ("bytes32",)) + abi_encode(
            ["bytes32"], [_to_bytes32(root)]
        )

    @staticmethod
    def decode_bool(returned: bytes) -> bool:
        """Decode a single ``bool`` return value; empty data reads as False."""
        if not returned:
            return False
        decoded = abi_decode(["bool"], returned)
        return bool(decoded[0])

    # ------------------------------------------------------------- topics

    @property
    def item_anchored_topic(self) -> str:
        return event_topic("ItemAnchored", ("bytes32", "bytes32", "address", "uint256"))

    @property
    def batch_anchored_topic(self) -> str:
        return event_topic("BatchAnchored", ("bytes32", "uint32", "address", "uint256"))

    # ------------------------------------------------------------- decoding

    def decode_log(self, entry: LogEntry) -> ItemAnchoredEvent | BatchAnchoredEvent | None:
        """Decode one log, or ``None`` if it is not an event this system reads.

        Unknown logs are skipped rather than raised on: the registry may grow
        events this version predates, and an indexer that dies on an unfamiliar
        topic stops indexing the events it does understand.
        """
        if not entry.topics:
            return None
        topic0 = normalise_hex(entry.topics[0])
        if topic0 == self.item_anchored_topic:
            return self._decode_item_anchored(entry)
        if topic0 == self.batch_anchored_topic:
            return self._decode_batch_anchored(entry)
        return None

    def _decode_item_anchored(self, entry: LogEntry) -> ItemAnchoredEvent:
        if len(entry.topics) != 4:
            raise ValueError(f"ItemAnchored expects 4 topics, got {len(entry.topics)}")
        (timestamp,) = abi_decode(["uint256"], _hex_to_bytes(entry.data))
        return ItemAnchoredEvent(
            item_hash=normalise_hex(entry.topics[1]),
            issuer_hash=normalise_hex(entry.topics[2]),
            # An address topic is the 20-byte value right-aligned in 32 bytes.
            issuer=to_checksum_address("0x" + normalise_hex(entry.topics[3])[-40:]),
            timestamp=int(timestamp),
            block_number=entry.block_number,
            block_hash=entry.block_hash,
            tx_hash=entry.tx_hash,
            log_index=entry.log_index,
        )

    def _decode_batch_anchored(self, entry: LogEntry) -> BatchAnchoredEvent:
        if len(entry.topics) != 2:
            raise ValueError(f"BatchAnchored expects 2 topics, got {len(entry.topics)}")
        leaf_count, issuer, timestamp = abi_decode(
            ["uint32", "address", "uint256"], _hex_to_bytes(entry.data)
        )
        return BatchAnchoredEvent(
            root=normalise_hex(entry.topics[1]),
            leaf_count=int(leaf_count),
            issuer=to_checksum_address(issuer),
            timestamp=int(timestamp),
            block_number=entry.block_number,
            block_hash=entry.block_hash,
            tx_hash=entry.tx_hash,
            log_index=entry.log_index,
        )

    # --------------------------------------------------------------- errors

    def decode_revert(self, data: bytes | str | None) -> DecodedRevert | None:
        """Turn revert return-data into a named error, when it is recognisable."""
        return decode_revert(data, self._errors)


def decode_revert(
    data: bytes | str | None,
    custom_errors: dict[str, dict[str, Any]] | None = None,
) -> DecodedRevert | None:
    """Decode ``Error(string)``, ``Panic(uint256)``, or a known custom error.

    Returns ``None`` for empty or unrecognised data. An unrecognised revert is
    still a revert -- the caller records the raw bytes -- but guessing at its
    meaning would be worse than admitting the reason is unknown.
    """
    if not data:
        return None
    raw = data if isinstance(data, bytes) else _hex_to_bytes(data)
    if len(raw) < 4:
        return None

    head = normalise_hex(raw[:4])
    body = raw[4:]

    if head == ERROR_STRING_SELECTOR:
        (message,) = abi_decode(["string"], body)
        return DecodedRevert(name="Error", args=(message,), raw=normalise_hex(raw))

    if head == PANIC_SELECTOR:
        (code,) = abi_decode(["uint256"], body)
        return DecodedRevert(name="Panic", args=(int(code),), raw=normalise_hex(raw))

    entry = (custom_errors or {}).get(head)
    if entry is None:
        return None

    types = [argument["type"] for argument in entry.get("inputs", [])]
    decoded = abi_decode(types, body) if types else ()
    args = tuple(normalise_hex(v) if isinstance(v, bytes) else v for v in decoded)
    return DecodedRevert(name=entry["name"], args=args, raw=normalise_hex(raw))


# ------------------------------------------------------------------- helpers


def _to_bytes32(value: str | bytes) -> bytes:
    """Parse a 32-byte hash from hex or bytes, rejecting the wrong length."""
    raw = value if isinstance(value, bytes) else _hex_to_bytes(value)
    if len(raw) != 32:
        raise ValueError(f"expected a 32-byte value, got {len(raw)} bytes")
    return raw


def _hex_to_bytes(value: str) -> bytes:
    cleaned = value[2:] if value.startswith(("0x", "0X")) else value
    if len(cleaned) % 2:
        cleaned = "0" + cleaned
    return bytes.fromhex(cleaned)


@lru_cache(maxsize=4)
def load_contract(abi_path: Path | None = None, address: str | None = None) -> ContractBinding:
    """Load the exported artifact and bind it to the configured address.

    Cached: the surface assertion and the selector table are the same for the
    life of the process, and re-reading the artifact on every send would put a
    file read inside the outbox drain loop.
    """
    settings = get_settings()
    path = abi_path or settings.contract_abi_path
    if not path.is_file():
        raise ContractSurfaceError(
            f"no contract artifact at {path} -- "
            "run `npm run compile` in backend/contracts to build and export it"
        )
    artifact = json.loads(path.read_text(encoding="utf-8"))
    abi = artifact["abi"] if isinstance(artifact, dict) and "abi" in artifact else artifact
    bytecode = artifact.get("bytecode", "") if isinstance(artifact, dict) else ""
    return ContractBinding(address or settings.contract_address, abi, bytecode)
