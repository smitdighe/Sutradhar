"""Merkle tree construction, proofs, and the odd-node promotion rule."""

from __future__ import annotations

import pytest

from app.core.hashing import keccak256
from app.core.merkle import MerkleError, build_proof, build_root, hash_pair, verify_proof

pytestmark = pytest.mark.unit


def leaves(count: int) -> list[bytes]:
    """Deterministic leaves, so roots are stable across runs and machines."""
    return [keccak256(f"leaf-{index}".encode()) for index in range(count)]


class TestDegenerateCases:
    def test_empty_raises(self) -> None:
        with pytest.raises(MerkleError, match="zero leaves"):
            build_root([])

    def test_single_leaf_is_its_own_root(self) -> None:
        one = leaves(1)
        assert build_root(one) == one[0]

    def test_single_leaf_proof_is_empty_and_verifies(self) -> None:
        one = leaves(1)
        proof = build_proof(one, 0)
        assert proof == []
        assert verify_proof(one[0], proof, build_root(one))

    def test_index_out_of_range_raises(self) -> None:
        with pytest.raises(MerkleError, match="out of range"):
            build_proof(leaves(3), 3)

    def test_wrong_sized_leaf_raises(self) -> None:
        with pytest.raises(MerkleError, match="not 32 bytes"):
            build_root([b"short"])


class TestRootConstruction:
    def test_two_leaves_is_the_sorted_pair_hash(self) -> None:
        pair = leaves(2)
        assert build_root(pair) == hash_pair(pair[0], pair[1])

    def test_pair_hashing_is_order_independent(self) -> None:
        first, second = leaves(2)
        assert hash_pair(first, second) == hash_pair(second, first)

    def test_odd_node_is_promoted_not_duplicated(self) -> None:
        # Three leaves: level 1 is [h(0,1), 2] because leaf 2 is carried up
        # unchanged. Duplicating it -- h(2,2) -- would give a different root,
        # and any verifier using the other rule would reject every proof.
        three = leaves(3)
        promoted = hash_pair(hash_pair(three[0], three[1]), three[2])
        duplicated = hash_pair(hash_pair(three[0], three[1]), hash_pair(three[2], three[2]))
        assert build_root(three) == promoted
        assert build_root(three) != duplicated

    def test_root_is_deterministic(self) -> None:
        assert build_root(leaves(17)) == build_root(leaves(17))

    def test_swapping_leaves_within_a_pair_does_not_change_the_root(self) -> None:
        # A documented consequence of sorted-pair hashing, not a bug: hash_pair
        # is symmetric, so leaves 0 and 1 are interchangeable under their shared
        # parent. See the module docstring -- a root commits to the leaf set and
        # the tree shape, not to intra-pair ordering.
        original = leaves(4)
        swapped = [original[1], original[0], original[2], original[3]]
        assert build_root(original) == build_root(swapped)

    def test_moving_a_leaf_across_parents_does_change_the_root(self) -> None:
        original = leaves(4)
        moved = [original[2], original[1], original[0], original[3]]
        assert build_root(original) != build_root(moved)

    def test_changing_a_leaf_changes_the_root(self) -> None:
        original = leaves(4)
        altered = [*original[:3], keccak256(b"different")]
        assert build_root(original) != build_root(altered)


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 7, 8, 9, 16, 17, 100, 1000])
class TestProofsAcrossSizes:
    def test_every_proof_verifies(self, count: int) -> None:
        batch = leaves(count)
        root = build_root(batch)
        for index in range(count):
            assert verify_proof(batch[index], build_proof(batch, index), root), (
                f"proof failed for leaf {index} of {count}"
            )

    def test_tampered_leaf_fails(self, count: int) -> None:
        batch = leaves(count)
        root = build_root(batch)
        forged = keccak256(b"not in the tree")
        assert not verify_proof(forged, build_proof(batch, 0), root)

    def test_proof_depth_is_logarithmic(self, count: int) -> None:
        batch = leaves(count)
        assert len(build_proof(batch, 0)) <= count.bit_length()


class TestProofRejection:
    def test_wrong_root_fails(self) -> None:
        batch = leaves(8)
        other = leaves(9)
        assert not verify_proof(batch[0], build_proof(batch, 0), build_root(other))

    def test_truncated_proof_fails(self) -> None:
        batch = leaves(8)
        proof = build_proof(batch, 3)
        assert not verify_proof(batch[3], proof[:-1], build_root(batch))

    def test_proof_for_a_different_leaf_fails(self) -> None:
        batch = leaves(8)
        root = build_root(batch)
        assert not verify_proof(batch[0], build_proof(batch, 5), root)

    def test_malformed_sizes_are_rejected_not_raised(self) -> None:
        batch = leaves(4)
        root = build_root(batch)
        assert not verify_proof(b"short", build_proof(batch, 0), root)
        assert not verify_proof(batch[0], build_proof(batch, 0), b"short")
        assert not verify_proof(batch[0], [b"short"], root)
