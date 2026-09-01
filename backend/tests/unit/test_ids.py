"""UUIDv7 identifiers and human-typeable tag codes.

The tag code volume test is large on purpose. A tag code is printed on a
physical label; a collision means two textiles claim the same provenance and
there is no way to tell which is which after the fact.
"""

from __future__ import annotations

import itertools
import uuid

import pytest

from app.core.ids import (
    TAG_ALPHABET,
    TAG_CHECKSUM_MODULUS,
    TAG_CODE_LENGTH,
    new_id,
    new_tag_code,
    new_uuid,
    normalize_tag_code,
    validate_tag_code,
)

pytestmark = pytest.mark.unit

TAG_SAMPLE = 100_000
# Excluded outright: A and E are vowels (codes should not spell words); I, L, O
# and U are the characters Crockford drops because they are misread; Z is
# dropped so the alphabet size equals the checksum modulus.
FORBIDDEN = set("AEILOUZ")


class TestUUIDv7:
    def test_new_id_is_a_canonical_hyphenated_string(self) -> None:
        value = new_id()
        assert isinstance(value, str)
        assert len(value) == 36
        assert value.count("-") == 4
        assert str(uuid.UUID(value)) == value

    def test_version_is_7(self) -> None:
        assert new_uuid().version == 7

    def test_new_uuid_returns_a_uuid_object(self) -> None:
        assert isinstance(new_uuid(), uuid.UUID)

    def test_ids_sort_in_generation_order(self) -> None:
        # This is the whole reason for v7 over v4: keyset pagination breaks
        # ties on id, and a random id would order pages arbitrarily.
        generated = [new_id() for _ in range(20_000)]
        assert generated == sorted(generated)

    def test_no_duplicates(self) -> None:
        generated = [new_id() for _ in range(20_000)]
        assert len(set(generated)) == len(generated)

    def test_strictly_increasing_within_the_same_millisecond(self) -> None:
        generated = [new_id() for _ in range(5_000)]
        assert all(first < second for first, second in itertools.pairwise(generated))


class TestTagCodeShape:
    def test_length(self) -> None:
        assert len(new_tag_code()) == TAG_CODE_LENGTH == 12

    def test_alphabet_size_equals_the_checksum_modulus(self) -> None:
        # Not a coincidence: equality is what makes the index-to-residue map a
        # bijection, and that is what closes the transposition blind spot.
        assert len(TAG_ALPHABET) == TAG_CHECKSUM_MODULUS == 29
        assert len(set(TAG_ALPHABET)) == 29

    def test_alphabet_excludes_ambiguous_and_vowel_characters(self) -> None:
        assert FORBIDDEN.isdisjoint(TAG_ALPHABET)

    def test_generated_codes_are_uppercase_alphanumeric(self) -> None:
        for _ in range(1_000):
            code = new_tag_code()
            assert code.isupper() or code.isdigit()
            assert code.isalnum()


@pytest.fixture(scope="module")
def sample() -> list[str]:
    """One large batch, generated once -- generation is the expensive part."""
    return [new_tag_code() for _ in range(TAG_SAMPLE)]


class TestTagCodeVolume:
    def test_no_collisions(self, sample: list[str]) -> None:
        assert len(set(sample)) == TAG_SAMPLE

    def test_all_checksums_valid(self, sample: list[str]) -> None:
        invalid = [code for code in sample if not validate_tag_code(code)]
        assert invalid == []

    def test_none_contain_forbidden_characters(self, sample: list[str]) -> None:
        offenders = [code for code in sample if FORBIDDEN & set(code)]
        assert offenders == []

    def test_all_symbols_of_the_alphabet_get_used(self, sample: list[str]) -> None:
        seen = set("".join(sample))
        assert seen == set(TAG_ALPHABET)


class TestChecksum:
    def test_any_single_character_substitution_is_caught(self) -> None:
        # A prime modulus with positional weights detects every single-symbol
        # error. Exhaustive over one code: every position, every wrong symbol.
        code = new_tag_code()
        for position in range(TAG_CODE_LENGTH):
            for replacement in TAG_ALPHABET:
                if replacement == code[position]:
                    continue
                mutated = code[:position] + replacement + code[position + 1 :]
                assert not validate_tag_code(mutated), f"missed substitution at {position}"

    def test_adjacent_transpositions_are_caught(self) -> None:
        for _ in range(500):
            code = new_tag_code()
            for position in range(TAG_CODE_LENGTH - 1):
                if code[position] == code[position + 1]:
                    continue  # swapping equal characters is not an error
                swapped = (
                    code[:position]
                    + code[position + 1]
                    + code[position]
                    + code[position + 2 :]
                )
                assert not validate_tag_code(swapped)

    def test_wrong_length_is_rejected(self) -> None:
        code = new_tag_code()
        assert not validate_tag_code(code[:-1])
        assert not validate_tag_code(code + "7")
        assert not validate_tag_code("")

    def test_out_of_alphabet_characters_are_rejected(self) -> None:
        assert not validate_tag_code("!" * TAG_CODE_LENGTH)


class TestNormalisation:
    def test_lowercase_is_accepted(self) -> None:
        code = new_tag_code()
        assert validate_tag_code(code.lower())

    def test_hyphens_spaces_and_underscores_are_stripped(self) -> None:
        code = new_tag_code()
        assert validate_tag_code(f"{code[:4]}-{code[4:8]} {code[8:]}")
        assert validate_tag_code(f"{code[:6]}_{code[6:]}")

    @pytest.mark.parametrize(
        ("typed", "meant"),
        [("I", "1"), ("L", "1"), ("O", "0"), ("U", "V"), ("Z", "2")],
    )
    def test_ambiguous_characters_fold_to_what_was_meant(self, typed: str, meant: str) -> None:
        assert normalize_tag_code(typed) == meant

    def test_normalisation_is_idempotent(self) -> None:
        code = new_tag_code()
        assert normalize_tag_code(normalize_tag_code(code)) == normalize_tag_code(code)

    def test_a_misread_l_still_validates(self) -> None:
        # Somebody reads '1' off a label as 'l'. The fold recovers it rather
        # than telling an honest buyer their genuine textile is fake.
        code = next(
            candidate for candidate in (new_tag_code() for _ in range(1_000)) if "1" in candidate
        )
        assert validate_tag_code(code.replace("1", "l", 1))
        assert validate_tag_code(code.replace("1", "I", 1))
