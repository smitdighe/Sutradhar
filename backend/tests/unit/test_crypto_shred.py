"""Crypto-shredding: salted identity hashes and what deleting a salt buys.

The property under test is the one the DPDP erasure story rests on -- that an
identity hash is reproducible while the salt exists and useless once it is
gone.
"""

from __future__ import annotations

import pytest

from app.core.crypto_shred import SALT_BYTES, identity_hash, matches_identity, new_salt

pytestmark = pytest.mark.unit

SUBJECT = "01926b8f-0000-7000-8000-000000000001"


class TestSalt:
    def test_salt_is_32_bytes(self) -> None:
        assert len(new_salt()) == SALT_BYTES == 32

    def test_salts_are_unique(self) -> None:
        salts = {new_salt() for _ in range(1_000)}
        assert len(salts) == 1_000

    def test_wrong_salt_length_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly 32 bytes"):
            identity_hash(SUBJECT, b"too short")


class TestDeterminism:
    def test_same_subject_and_salt_give_the_same_hash(self) -> None:
        # Verification depends on this: the hash anchored on chain has to be
        # recomputable from the Postgres row years later.
        salt = new_salt()
        assert identity_hash(SUBJECT, salt) == identity_hash(SUBJECT, salt)

    def test_hash_is_prefixed_lowercase_hex(self) -> None:
        digest = identity_hash(SUBJECT, new_salt())
        assert digest.startswith("0x")
        assert len(digest) == 66
        assert digest[2:] == digest[2:].lower()

    def test_matches_identity_accepts_a_correct_recomputation(self) -> None:
        salt = new_salt()
        assert matches_identity(SUBJECT, salt, identity_hash(SUBJECT, salt))

    def test_matches_identity_rejects_a_wrong_subject(self) -> None:
        salt = new_salt()
        assert not matches_identity("someone-else", salt, identity_hash(SUBJECT, salt))


class TestUnlinkability:
    def test_different_salts_give_different_hashes(self) -> None:
        # The point of a per-subject salt: the same person under two salts is
        # two unrelated digests, so on-chain records cannot be correlated.
        first, second = identity_hash(SUBJECT, new_salt()), identity_hash(SUBJECT, new_salt())
        assert first != second

    def test_different_subjects_under_one_salt_give_different_hashes(self) -> None:
        salt = new_salt()
        assert identity_hash("subject-a", salt) != identity_hash("subject-b", salt)

    def test_hash_cannot_be_recomputed_without_the_right_salt(self) -> None:
        # This is what "deleting the salt row makes the on-chain hash
        # permanently unlinkable" means in practice. With the salt destroyed,
        # confirming the subject means searching a 2**256 space; guessing is
        # not a strategy. A thousand wrong guesses stand in for that here.
        real_salt = new_salt()
        target = identity_hash(SUBJECT, real_salt)
        guesses = {identity_hash(SUBJECT, new_salt()) for _ in range(1_000)}
        assert target not in guesses

    def test_no_salt_produces_a_colliding_hash_for_a_known_subject(self) -> None:
        salts = [new_salt() for _ in range(500)]
        digests = [identity_hash(SUBJECT, salt) for salt in salts]
        assert len(set(digests)) == len(digests)

    def test_subject_is_not_recoverable_from_the_digest(self) -> None:
        salt = new_salt()
        digest = identity_hash(SUBJECT, salt)
        assert SUBJECT not in digest
        assert salt.hex() not in digest
