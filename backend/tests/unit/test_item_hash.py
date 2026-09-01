"""The frozen item preimage.

The digests below are literal hex strings, checked in. They are not "whatever
the code produces today" -- they are the contract. Every item hash anchored on
chain is one of these, and the chain cannot be rewritten, so a change to the
preimage does not break a test, it permanently invalidates every record that
came before.

If a test in this file fails, the correct response is almost never to update the
expected value.

**What is frozen, precisely.** The *field set* and *each value's encoding*.
Field *order* is not, and cannot be: :func:`~app.core.canonical.canonicalize`
implements RFC 8785, which sorts object keys, so Python's insertion order is
erased before the digest is taken. That is deliberate -- it is what lets a
Solidity verifier, a Python reader and a JS client agree without first agreeing
on an ordering. :class:`TestOrderingIsNotPartOfTheContract` pins that property
so nobody "fixes" it later.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.core.canonical import canonicalize
from app.provenance.item_hash import (
    PREIMAGE_FIELDS,
    PREIMAGE_VERSION,
    assert_no_pii,
    build_preimage,
    compute_item_hash,
    quantise,
    registrant_hash,
)

pytestmark = pytest.mark.unit

# Fixed so the digests are reproducible. Never call now() in this file.
FIXED_AT = datetime(2026, 8, 26, 11, 4, 22, 481920, tzinfo=UTC)
BOLT_ID = uuid.UUID("01926b8f-0000-7000-8000-000000000001")
SAREE_ID = uuid.UUID("01926b8f-0000-7000-8000-000000000002")
CHAPPAL_ID = uuid.UUID("01926b8f-0000-7000-8000-000000000003")
WEAVER_HASH = "0x" + "ab" * 32
COBBLER_HASH = "0x" + "cd" * 32

PATOLA_ATTRS: dict[str, Any] = {
    "warp_count": 120,
    "weft_count": 116,
    "dye_type": "natural",
    "double_ikat": True,
    "loom_type": "pit",
    "weave_days": 210,
    "gi_registration_no": "GI-00232",
}
KOLHAPURI_ATTRS: dict[str, Any] = {
    "leather_type": "buffalo",
    "tanning_method": "vegetable",
    "sole_thickness_mm": 8.5,
    "braid_pattern": "kapshi",
    "artisan_cluster": "Kolhapur North",
}

# --- THE CONTRACT. Do not edit these to make a test pass. -------------------
EXPECTED_ROOT_BOLT = "0x4bdc6b388d9d111f67a4f9e423bcc58f2e5fa1c4f98a1511d8dcd643ed8d5b4e"
EXPECTED_CHILD_SAREE = "0x69c7fa39ec3050f1e617091fc74e6d465da45c5fecae8cae048df3f60c938baa"
EXPECTED_NON_TEXTILE = "0xd86ec192ab52f66b6c96298659897ef8f55e51df02948d3d5bb7d7767ca6076d"
# ---------------------------------------------------------------------------


def root_bolt(**overrides: Any) -> dict[str, Any]:
    """Fixture A: a 12-metre Patola bolt with no parent."""
    return build_preimage(
        **{
            "item_id": BOLT_ID,
            "category_slug": "patola-silk",
            "category_schema_version": 1,
            "parent_id": None,
            "quantity": Decimal("12.0000"),
            "quantity_unit": "metre",
            "attributes": PATOLA_ATTRS,
            "registered_by_hash": WEAVER_HASH,
            "registered_at": FIXED_AT,
            **overrides,
        }
    )


def child_saree(**overrides: Any) -> dict[str, Any]:
    """Fixture B: a 5.5-metre saree cut from fixture A."""
    return build_preimage(
        **{
            "item_id": SAREE_ID,
            "category_slug": "patola-silk",
            "category_schema_version": 1,
            "parent_id": BOLT_ID,
            "quantity": Decimal("5.5000"),
            "quantity_unit": "metre",
            "attributes": PATOLA_ATTRS,
            "registered_by_hash": WEAVER_HASH,
            "registered_at": FIXED_AT,
            **overrides,
        }
    )


def non_textile(**overrides: Any) -> dict[str, Any]:
    """Fixture C: a pair of Kolhapuri chappals. Different unit, different shape."""
    return build_preimage(
        **{
            "item_id": CHAPPAL_ID,
            "category_slug": "kolhapuri-chappal",
            "category_schema_version": 1,
            "parent_id": None,
            "quantity": Decimal("1.0000"),
            "quantity_unit": "pair",
            "attributes": KOLHAPURI_ATTRS,
            "registered_by_hash": COBBLER_HASH,
            "registered_at": FIXED_AT,
            **overrides,
        }
    )


class TestFrozenDigests:
    """The three checked-in digests. These are the wire format."""

    def test_root_bolt(self) -> None:
        assert compute_item_hash(root_bolt()) == EXPECTED_ROOT_BOLT

    def test_child_saree(self) -> None:
        assert compute_item_hash(child_saree()) == EXPECTED_CHILD_SAREE

    def test_non_textile(self) -> None:
        assert compute_item_hash(non_textile()) == EXPECTED_NON_TEXTILE

    def test_digests_are_distinct(self) -> None:
        assert len({EXPECTED_ROOT_BOLT, EXPECTED_CHILD_SAREE, EXPECTED_NON_TEXTILE}) == 3

    def test_shape(self) -> None:
        digest = compute_item_hash(root_bolt())
        assert digest.startswith("0x")
        assert len(digest) == 66
        assert digest[2:] == digest[2:].lower()

    def test_stable_across_repeated_computation(self) -> None:
        # Nothing in the path may depend on process state -- no PYTHONHASHSEED
        # sensitivity, no memoisation keyed on identity.
        assert {compute_item_hash(root_bolt()) for _ in range(50)} == {EXPECTED_ROOT_BOLT}


class TestFieldSetIsFrozen:
    def test_exactly_these_fields(self) -> None:
        # Adding or removing a preimage field changes every future digest, so
        # it fails here first.
        assert set(root_bolt()) == set(PREIMAGE_FIELDS)

    def test_version_is_one(self) -> None:
        assert root_bolt()["v"] == PREIMAGE_VERSION == 1

    @pytest.mark.parametrize("field", sorted(PREIMAGE_FIELDS))
    def test_dropping_any_field_changes_the_hash(self, field: str) -> None:
        mutilated = {k: v for k, v in root_bolt().items() if k != field}
        assert compute_item_hash(mutilated) != EXPECTED_ROOT_BOLT

    @pytest.mark.parametrize("field", sorted(PREIMAGE_FIELDS))
    def test_renaming_any_field_changes_the_hash(self, field: str) -> None:
        # This is the check that would catch somebody "tidying" a field name.
        renamed = {(f"{k}_renamed" if k == field else k): v for k, v in root_bolt().items()}
        assert compute_item_hash(renamed) != EXPECTED_ROOT_BOLT

    def test_adding_a_field_changes_the_hash(self) -> None:
        assert compute_item_hash({**root_bolt(), "extra": "harmless"}) != EXPECTED_ROOT_BOLT


class TestEveryValueMatters:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("item_id", str(SAREE_ID)),
            ("category_slug", "sambalpuri-bandha"),
            ("category_schema_version", 2),
            ("parent_id", str(BOLT_ID)),
            ("quantity", "12.0001"),
            ("quantity_unit", "yard"),
            ("registered_by_hash", "0x" + "ef" * 32),
            ("registered_at", "2026-08-26T11:04:22.481921Z"),
            ("v", 2),
        ],
    )
    def test_changing_one_field_changes_the_hash(self, field: str, value: Any) -> None:
        assert compute_item_hash({**root_bolt(), field: value}) != EXPECTED_ROOT_BOLT

    def test_changing_one_attribute_changes_the_hash(self) -> None:
        altered = root_bolt(attributes={**PATOLA_ATTRS, "warp_count": 121})
        assert compute_item_hash(altered) != EXPECTED_ROOT_BOLT

    def test_a_parent_is_not_the_same_as_no_parent(self) -> None:
        # A child that dropped its parent link would otherwise hash like a root,
        # which is exactly the "orphan the lineage" attack.
        orphaned = child_saree(parent_id=None)
        assert compute_item_hash(orphaned) != EXPECTED_CHILD_SAREE


class TestEncodingIsFrozen:
    def test_quantity_is_a_string_at_four_decimal_places(self) -> None:
        preimage = root_bolt(quantity=Decimal("12"))
        assert preimage["quantity"] == "12.0000"
        assert isinstance(preimage["quantity"], str)

    def test_quantity_scale_is_normalised_not_preserved(self) -> None:
        # 12, 12.0 and 12.0000 are the same physical quantity and must hash the
        # same; numeric(18,4) stores them identically.
        for value in (Decimal("12"), Decimal("12.0"), Decimal("12.0000")):
            assert compute_item_hash(root_bolt(quantity=value)) == EXPECTED_ROOT_BOLT

    def test_quantity_never_becomes_a_float(self) -> None:
        # The classic failure: 0.1 + 0.2 as float is 0.30000000000000004.
        preimage = root_bolt(quantity=Decimal("0.1") + Decimal("0.2"))
        assert preimage["quantity"] == "0.3000"

    def test_a_float_quantity_would_be_visible(self) -> None:
        # Documents the failure mode: a JSON number renders differently from
        # the string form, so the digest moves.
        assert compute_item_hash({**root_bolt(), "quantity": 12.0}) != EXPECTED_ROOT_BOLT

    def test_timestamp_has_exactly_six_fractional_digits(self) -> None:
        rendered = root_bolt()["registered_at"]
        assert rendered == "2026-08-26T11:04:22.481920Z"
        assert rendered.endswith("Z")
        assert len(rendered.split(".")[1]) == 7  # six digits plus the Z

    def test_trailing_zeros_in_the_timestamp_are_significant(self) -> None:
        # ...481920Z and ...48192Z are the same instant, rendered differently.
        # A renderer that trimmed would silently change every digest.
        assert (
            compute_item_hash({**root_bolt(), "registered_at": "2026-08-26T11:04:22.48192Z"})
            != EXPECTED_ROOT_BOLT
        )

    def test_naive_and_aware_timestamps_agree(self) -> None:
        naive = FIXED_AT.replace(tzinfo=None)
        assert compute_item_hash(root_bolt(registered_at=naive)) == EXPECTED_ROOT_BOLT

    def test_non_utc_timestamps_are_converted(self) -> None:
        from datetime import timedelta, timezone

        ist = FIXED_AT.astimezone(timezone(timedelta(hours=5, minutes=30)))
        assert compute_item_hash(root_bolt(registered_at=ist)) == EXPECTED_ROOT_BOLT


class TestOrderingIsNotPartOfTheContract:
    """RFC 8785 sorts keys, so insertion order cannot affect the digest.

    This is a property worth pinning rather than an accident: it is what lets
    an independent implementation reproduce the hash without being told the
    field order, and it means a refactor that reshuffles a dict literal is safe.
    """

    def test_reversed_preimage_order_hashes_identically(self) -> None:
        reversed_order = dict(reversed(list(root_bolt().items())))
        assert list(reversed_order) != list(root_bolt())
        assert compute_item_hash(reversed_order) == EXPECTED_ROOT_BOLT

    def test_attribute_insertion_order_does_not_matter(self) -> None:
        shuffled = dict(reversed(list(PATOLA_ATTRS.items())))
        assert compute_item_hash(root_bolt(attributes=shuffled)) == EXPECTED_ROOT_BOLT

    def test_sorted_and_unsorted_agree(self) -> None:
        alphabetical = dict(sorted(root_bolt().items()))
        assert compute_item_hash(alphabetical) == EXPECTED_ROOT_BOLT


class TestNoPersonalDataEverEnters:
    """The DPDP Act 2023 answer, asserted rather than asserted-in-a-docstring.

    The chain is append-only. Anything in the preimage is anchored forever, so
    a name or an email in here would make erasure impossible. The registrant
    appears only as a salted digest; delete the salt and the anchored hash
    becomes unlinkable to the person.
    """

    def test_the_registrant_appears_only_as_a_hash(self) -> None:
        preimage = root_bolt()
        assert preimage["registered_by_hash"] == WEAVER_HASH
        assert "registered_by" not in preimage
        assert "user_id" not in preimage

    def test_no_identifying_substring_survives_serialisation(self) -> None:
        blob = canonicalize(root_bolt()).decode("utf-8")
        for needle in [
            "ramesh.patel@patanweavers.example.com",
            "Ramesh Patel",
            str(uuid.UUID("01926b8f-1111-7000-8000-000000000009")),
            "@",
        ]:
            assert needle not in blob, f"preimage leaked {needle!r}"

    def test_assert_no_pii_catches_a_leak(self) -> None:
        leaky = {**root_bolt(), "registered_by_email": "weaver@example.com"}
        with pytest.raises(ValueError, match="identifying data"):
            assert_no_pii(leaky, ["weaver@example.com"])

    def test_assert_no_pii_passes_a_clean_preimage(self) -> None:
        assert_no_pii(root_bolt(), ["weaver@example.com", "Ramesh Patel"])

    def test_the_identity_hash_is_salt_dependent(self) -> None:
        # Two salts, same user, different digests. Losing the salt is what makes
        # the on-chain value unlinkable.
        user_id = uuid.uuid4()
        first = registrant_hash(user_id, b"\x01" * 32)
        second = registrant_hash(user_id, b"\x02" * 32)
        assert first != second
        assert str(user_id) not in first


class TestQuantise:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("12", "12.0000"),
            ("12.5", "12.5000"),
            ("5.55555", "5.5556"),  # half-up
            ("5.55554", "5.5555"),
            ("0.00005", "0.0001"),
            (12, "12.0000"),
        ],
    )
    def test_rounding(self, value: Any, expected: str) -> None:
        assert str(quantise(value)) == expected

    def test_half_up_not_bankers(self) -> None:
        # Banker's rounding would give 0.0002 here, which is a surprise nobody
        # reading a mass-balance calculation expects.
        assert str(quantise("0.00025")) == "0.0003"
