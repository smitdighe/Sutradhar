"""RFC 8785 canonicalization.

The property that matters: two structures a human would call equal must produce
identical bytes, because those bytes are what gets hashed and anchored. Every
test here is a way that equality could be broken.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.canonical import CanonicalizationError, canonicalize, canonicalize_to_str

pytestmark = pytest.mark.unit


class TestKeyOrdering:
    def test_key_insertion_order_is_irrelevant(self) -> None:
        first = {"zebra": 1, "apple": 2, "mango": 3}
        second = {"mango": 3, "zebra": 1, "apple": 2}
        assert canonicalize(first) == canonicalize(second)

    def test_keys_are_sorted(self) -> None:
        assert canonicalize_to_str({"b": 1, "a": 2, "c": 3}) == '{"a":2,"b":1,"c":3}'

    def test_nested_objects_are_sorted_at_every_level(self) -> None:
        first = {"outer": {"z": {"y": 1, "x": 2}, "a": 3}}
        second = {"outer": {"a": 3, "z": {"x": 2, "y": 1}}}
        assert canonicalize(first) == canonicalize(second)
        assert canonicalize_to_str(first) == '{"outer":{"a":3,"z":{"x":2,"y":1}}}'

    def test_keys_sort_by_utf16_code_unit_not_code_point(self) -> None:
        # The two orderings genuinely disagree here, which is the point:
        #   U+1F600 is the surrogate pair D83D DE00 in UTF-16
        #   U+FB00 is the single code unit FB00
        # By UTF-16 code unit D83D < FB00, so the astral character sorts first.
        # By code point 0xFB00 < 0x1F600, so it would sort second. RFC 8785
        # mandates UTF-16, and a naive Python sort would get this backwards.
        astral, bmp = "\U0001f600", "ﬀ"
        assert sorted([astral, bmp]) == [bmp, astral]  # Python's code-point order

        output = canonicalize_to_str({astral: 1, bmp: 2})
        assert output.index(f'"{astral}"') < output.index(f'"{bmp}"')

    def test_non_string_keys_are_rejected(self) -> None:
        with pytest.raises(CanonicalizationError, match="keys must be strings"):
            canonicalize({1: "a"})


class TestUnicode:
    def test_decomposed_and_precomposed_forms_are_identical(self) -> None:
        precomposed = "é"  # e-acute
        decomposed = "é"  # e + combining acute
        assert precomposed != decomposed
        assert canonicalize(precomposed) == canonicalize(decomposed)

    def test_nfc_applies_to_keys_as_well_as_values(self) -> None:
        assert canonicalize({"é": 1}) == canonicalize({"é": 1})

    def test_control_characters_use_short_escapes_where_defined(self) -> None:
        assert canonicalize_to_str("a\tb\nc") == '"a\\tb\\nc"'

    def test_other_control_characters_use_u_escapes(self) -> None:
        assert canonicalize_to_str("\x01") == '"\\u0001"'

    def test_quotes_and_backslashes_are_escaped(self) -> None:
        assert canonicalize_to_str('a"b\\c') == '"a\\"b\\\\c"'

    def test_non_ascii_is_emitted_literally_not_escaped(self) -> None:
        assert canonicalize("नमस्ते") == '"नमस्ते"'.encode()


class TestDecimals:
    def test_decimal_serialises_as_a_string(self) -> None:
        assert canonicalize_to_str(Decimal("1.5")) == '"1.5"'

    def test_decimal_trailing_zeros_are_preserved(self) -> None:
        # 1.5000 and 1.5 are different scales and must not collapse: a quantity
        # of 1.5000 metres came out of numeric(18,4) and is not the same row.
        assert canonicalize_to_str(Decimal("1.5000")) == '"1.5000"'
        assert canonicalize(Decimal("1.5000")) != canonicalize(Decimal("1.5"))

    def test_decimal_never_passes_through_float(self) -> None:
        # 0.1 + 0.2 is exactly representable in Decimal and is not in binary
        # floating point. If this ever went through float the output would be
        # 0.30000000000000004.
        value = Decimal("0.1") + Decimal("0.2")
        assert canonicalize_to_str(value) == '"0.3"'

    def test_high_precision_decimal_survives_intact(self) -> None:
        value = Decimal("123456789012345.6789")
        assert canonicalize_to_str(value) == '"123456789012345.6789"'

    def test_non_finite_decimal_is_rejected(self) -> None:
        with pytest.raises(CanonicalizationError, match="non-finite"):
            canonicalize(Decimal("NaN"))


class TestNumbers:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, "0"),
            (-0.0, "0"),
            (1, "1"),
            (-17, "-17"),
            (0.1, "0.1"),
            (1.5, "1.5"),
            (123.0, "123"),
            (1e-5, "0.00001"),
            (1e-6, "0.000001"),
            (1e-7, "1e-7"),
            (1e20, "100000000000000000000"),
            (1e21, "1e+21"),
            (5e-324, "5e-324"),
        ],
    )
    def test_ecmascript_number_forms(self, value: float | int, expected: str) -> None:
        assert canonicalize_to_str(value) == expected

    def test_booleans_are_not_treated_as_integers(self) -> None:
        assert canonicalize_to_str(True) == "true"
        assert canonicalize_to_str(False) == "false"
        assert canonicalize_to_str({"a": True, "b": 1}) == '{"a":true,"b":1}'

    def test_nan_is_rejected(self) -> None:
        with pytest.raises(CanonicalizationError, match="NaN and Infinity"):
            canonicalize(math.nan)

    @pytest.mark.parametrize("value", [math.inf, -math.inf])
    def test_infinity_is_rejected(self, value: float) -> None:
        with pytest.raises(CanonicalizationError, match="NaN and Infinity"):
            canonicalize(value)

    def test_nan_nested_inside_a_structure_is_still_rejected(self) -> None:
        with pytest.raises(CanonicalizationError):
            canonicalize({"a": [1, {"b": math.nan}]})


class TestStructures:
    def test_null_and_empty_containers(self) -> None:
        assert canonicalize_to_str(None) == "null"
        assert canonicalize_to_str({}) == "{}"
        assert canonicalize_to_str([]) == "[]"

    def test_array_order_is_significant(self) -> None:
        assert canonicalize([1, 2]) != canonicalize([2, 1])

    def test_no_insignificant_whitespace(self) -> None:
        output = canonicalize_to_str({"a": [1, 2], "b": {"c": 3}})
        assert output == '{"a":[1,2],"b":{"c":3}}'
        assert " " not in output

    def test_output_is_utf8_bytes(self) -> None:
        assert isinstance(canonicalize({"a": 1}), bytes)
        assert canonicalize("é") == b'"\xc3\xa9"'

    def test_datetime_becomes_rfc3339(self) -> None:
        moment = datetime(2026, 8, 26, 11, 4, 22, 481920, tzinfo=UTC)
        assert canonicalize_to_str(moment) == '"2026-08-26T11:04:22.481920Z"'

    def test_unsupported_type_is_rejected(self) -> None:
        with pytest.raises(CanonicalizationError, match="no canonical JSON representation"):
            canonicalize({"a": {1, 2}})

    def test_realistic_item_payload_is_reorder_stable(self) -> None:
        first = {
            "quantity": Decimal("5.5000"),
            "attributes": {"warp": "silk", "weft": "cotton", "loom": "pit"},
            "category": "banarasi-brocade",
            "registered_by": "01926b8f-0000-7000-8000-000000000001",
        }
        second = {
            "registered_by": "01926b8f-0000-7000-8000-000000000001",
            "category": "banarasi-brocade",
            "attributes": {"loom": "pit", "weft": "cotton", "warp": "silk"},
            "quantity": Decimal("5.5000"),
        }
        assert canonicalize(first) == canonicalize(second)
