"""Sorted-pair keccak256 Merkle tree.

Two rules define this tree, and both change the root, so both are load-bearing:

**Sorted pairs.** A parent is ``keccak256(min(a, b) + max(a, b))`` comparing the
two children as byte strings. Ordering the pair means a proof does not need to
record whether each sibling sat on the left or the right, so a proof is a flat
list of hashes. This is the same construction OpenZeppelin's ``MerkleProof``
verifies on chain, which is what lets the Solidity side check a proof without
extra calldata.

The price is that the root does **not** commit to the order of two leaves that
share a parent: swapping leaves 0 and 1 leaves the root unchanged, because
``hash_pair`` is symmetric by construction. Swapping leaves under *different*
parents does change it. So a root commits to the set of leaves and to the tree's
shape, but not to intra-pair ordering. Nothing here relies on leaf order --
``merkle_leaves.leaf_index`` records the position in Postgres so proofs can be
rebuilt, and inclusion is the only property being proved -- but any future code
that tries to read ordering out of a root is building on sand.

**Odd node promotes, it does not duplicate.** When a level has an odd number of
nodes the last one is carried up to the next level unchanged. The common
alternative -- hashing the last node with itself -- produces a *different root*
for the same leaves, and it admits a second-preimage quirk where a tree of n
leaves can be made to collide with one of n+1. Promotion is chosen here; any
verifier, on chain or off, must use the same rule or every proof fails.

Leaves are used exactly as supplied. Callers are responsible for domain
separation, because this module cannot tell an item hash from an attestation
hash.
"""

from __future__ import annotations

from app.core.hashing import keccak256

__all__ = ["build_proof", "build_root", "hash_pair", "verify_proof"]

DIGEST_BYTES = 32


class MerkleError(ValueError):
    """Raised for a malformed tree or an out-of-range leaf index."""


def _validate(leaves: list[bytes]) -> None:
    if not leaves:
        raise MerkleError("cannot build a Merkle tree over zero leaves")
    for position, leaf in enumerate(leaves):
        if not isinstance(leaf, bytes | bytearray) or len(leaf) != DIGEST_BYTES:
            raise MerkleError(f"leaf {position} is not {DIGEST_BYTES} bytes")


def hash_pair(left: bytes, right: bytes) -> bytes:
    """Combine two nodes as ``keccak256(min + max)``."""
    return keccak256(left + right if left <= right else right + left)


def _next_level(level: list[bytes]) -> list[bytes]:
    """Fold one level into its parents, promoting a trailing odd node."""
    parents = [hash_pair(level[i], level[i + 1]) for i in range(0, len(level) - 1, 2)]
    if len(level) % 2:
        parents.append(level[-1])
    return parents


def build_root(leaves: list[bytes]) -> bytes:
    """Return the Merkle root. A single leaf is its own root."""
    _validate(leaves)
    level = list(leaves)
    while len(level) > 1:
        level = _next_level(level)
    return level[0]


def build_proof(leaves: list[bytes], index: int) -> list[bytes]:
    """Return the sibling hashes proving ``leaves[index]`` is under the root.

    A promoted odd node has no sibling at that level, so it contributes nothing
    to the proof -- which is exactly why the promotion rule has to match on the
    verifying side.
    """
    _validate(leaves)
    if not 0 <= index < len(leaves):
        raise MerkleError(f"leaf index {index} out of range for {len(leaves)} leaves")

    proof: list[bytes] = []
    level = list(leaves)
    position = index
    while len(level) > 1:
        is_promoted = position == len(level) - 1 and len(level) % 2
        if not is_promoted:
            sibling = position ^ 1
            proof.append(level[sibling])
        level = _next_level(level)
        position //= 2
    return proof


def verify_proof(leaf: bytes, proof: list[bytes], root: bytes) -> bool:
    """True when *proof* carries *leaf* up to *root*."""
    if len(leaf) != DIGEST_BYTES or len(root) != DIGEST_BYTES:
        return False
    computed = leaf
    for sibling in proof:
        if len(sibling) != DIGEST_BYTES:
            return False
        computed = hash_pair(computed, sibling)
    return computed == root
