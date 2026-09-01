"""Hash stability.

These digests are hardcoded on purpose. Every item hash ever anchored on chain
was produced by this code path, and verification recomputes it from the Postgres
row -- so if the output ever changes, every previously anchored record silently
fails to verify. These tests exist to make that change loud.

The three keccak256/sha256 vectors below are the published ones for the empty
string and "abc", so they also catch the library underneath being swapped for
something that is not actually keccak (SHA3-256, notably, is a different
padding and produces a different digest for the same input).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.canonical import canonicalize
from app.core.hashing import from_hex, hash_hex, hash_object, keccak256, sha256_hex

pytestmark = pytest.mark.unit

KECCAK_EMPTY = "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
KECCAK_ABC = "0x4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
SHA256_ABC = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


class TestKeccak256:
    def test_empty_input(self) -> None:
        assert hash_hex(keccak256(b"")) == KECCAK_EMPTY

    def test_abc(self) -> None:
        assert hash_hex(keccak256(b"abc")) == KECCAK_ABC

    def test_is_keccak_not_sha3(self) -> None:
        # SHA3-256("") is a7ffc6f8bf1ed766... -- a different padding rule. If a
        # dependency swap ever routed this to SHA3, the chain would disagree.
        import hashlib

        assert hash_hex(keccak256(b"")) != "0x" + hashlib.sha3_256(b"").hexdigest()

    def test_digest_is_32_bytes(self) -> None:
        assert len(keccak256(b"anything")) == 32

    def test_avalanche(self) -> None:
        assert keccak256(b"a") != keccak256(b"b")


class TestHexHelpers:
    def test_hash_hex_is_prefixed_and_lowercase(self) -> None:
        digest = hash_hex(keccak256(b"abc"))
        assert digest.startswith("0x")
        assert digest[2:] == digest[2:].lower()
        assert len(digest) == 66

    def test_from_hex_round_trips(self) -> None:
        digest = keccak256(b"round trip")
        assert from_hex(hash_hex(digest)) == digest

    def test_from_hex_accepts_unprefixed(self) -> None:
        digest = keccak256(b"x")
        assert from_hex(digest.hex()) == digest


class TestSha256:
    def test_abc(self) -> None:
        assert sha256_hex(b"abc") == SHA256_ABC

    def test_is_unprefixed(self) -> None:
        assert not sha256_hex(b"abc").startswith("0x")


class TestHashObject:
    def test_empty_object(self) -> None:
        assert hash_object({}) == (
            "0xb48d38f93eaa084033fc5970bf96e559c33c4cdc07d889ab00b4d63f9590739d"
        )

    def test_simple_object(self) -> None:
        assert hash_object({"a": 1, "b": "two"}) == (
            "0xc826ec2fb7997621526efcf7c936384d79cd9c82ce811bfeef27c2385b1b830d"
        )

    def test_decimal_quantity(self) -> None:
        assert hash_object({"quantity": Decimal("5.5000")}) == (
            "0x1676a149fb75fc105a359208311a01a89fb2fd259a0400c6190119fa267a7ef7"
        )

    def test_equals_keccak_of_canonical_bytes(self) -> None:
        # hash_object is exactly this composition and nothing else; anything
        # that hashes an object another way is a bug.
        obj = {"b": [1, 2], "a": "x"}
        assert hash_object(obj) == hash_hex(keccak256(canonicalize(obj)))

    def test_key_order_does_not_change_the_hash(self) -> None:
        assert hash_object({"a": 1, "b": 2}) == hash_object({"b": 2, "a": 1})

    def test_decimal_scale_changes_the_hash(self) -> None:
        # numeric(18,4) round-trips 5.5000, and that is a different stored
        # value from 5.5 even though the numbers are equal.
        assert hash_object({"q": Decimal("5.5000")}) != hash_object({"q": Decimal("5.5")})

    def test_type_changes_the_hash(self) -> None:
        assert hash_object({"q": Decimal("1")}) != hash_object({"q": 1})
        assert hash_object({"q": "1"}) != hash_object({"q": 1})
