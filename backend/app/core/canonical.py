"""RFC 8785 JSON Canonicalization Scheme.

Canonical bytes are the input to every hash in this system, so two structures
that are semantically equal must produce byte-identical output regardless of
key insertion order, unicode escape form, or how the value reached Python.

Deviations from plain :func:`json.dumps`, each deliberate:

* Object keys sort by UTF-16 code unit, not by Python code point. The two
  differ above the BMP and RFC 8785 specifies the former.
* Strings are NFC-normalised before escaping, so a precomposed character and
  its decomposed form hash identically.
* :class:`~decimal.Decimal` serialises as a JSON **string**, never a number,
  and never passes through :class:`float`. Quantities are ``numeric(18,4)`` in
  Postgres and binary floating point cannot represent them exactly.
* Floats use the ECMAScript ``Number::toString`` form required by RFC 8785,
  which is not what :func:`repr` produces for small magnitudes.
* NaN and Infinity raise instead of emitting the invalid JSON that Python's
  stdlib would happily write.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.core.clock import to_rfc3339

__all__ = ["CanonicalizationError", "canonicalize", "canonicalize_to_str"]


class CanonicalizationError(ValueError):
    """Raised when a value cannot be represented as canonical JSON."""


# Per RFC 8785, which follows ECMAScript JSON.stringify escaping.
_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _escape_string(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    out: list[str] = ['"']
    for char in normalized:
        escape = _ESCAPES.get(char)
        if escape is not None:
            out.append(escape)
        elif char < "\x20":
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _number_to_string(value: float) -> str:
    """Serialise *value* per ECMAScript ``Number::toString``.

    :func:`repr` gives the shortest round-tripping digits but the wrong layout
    for small magnitudes -- ``repr(1e-5)`` is ``'1e-05'`` where ECMAScript, and
    therefore RFC 8785, requires ``'0.00001'``.
    """
    if math.isnan(value) or math.isinf(value):
        raise CanonicalizationError(
            f"NaN and Infinity have no JSON representation (got {value!r})"
        )
    if value == 0:
        return "0"  # collapses -0.0, which RFC 8785 requires

    sign = "-" if value < 0 else ""
    _, raw_digits, raw_exponent = Decimal(repr(abs(value))).as_tuple()
    if not isinstance(raw_exponent, int):  # pragma: no cover - guarded by isfinite above
        raise CanonicalizationError(f"non-finite decimal for {value!r}")

    digits = list(raw_digits)
    exponent = raw_exponent
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1

    significand = "".join(str(digit) for digit in digits)
    length = len(significand)
    # `point` is the decimal point position: value == 0.significand * 10**point
    point = length + exponent

    if length <= point <= 21:
        return sign + significand + "0" * (point - length)
    if 0 < point <= 21:
        return sign + significand[:point] + "." + significand[point:]
    if -6 < point <= 0:
        return sign + "0." + "0" * -point + significand
    mantissa = significand[0] + ("." + significand[1:] if length > 1 else "")
    exponent_sign = "+" if point - 1 >= 0 else "-"
    return f"{sign}{mantissa}e{exponent_sign}{abs(point - 1)}"


def _sort_key(key: str) -> bytes:
    """Sort object keys by UTF-16 code unit, as RFC 8785 requires."""
    return key.encode("utf-16-be")


def _serialize(value: Any) -> str:
    if value is None:
        return "null"
    # bool before int: bool is a subclass of int and would render as 1/0.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _escape_string(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CanonicalizationError(f"non-finite Decimal: {value!r}")
        # A JSON string, never a number -- see the module docstring.
        return _escape_string(str(value))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _number_to_string(value)
    if isinstance(value, datetime):
        return _escape_string(to_rfc3339(value))
    if isinstance(value, UUID):
        return _escape_string(str(value))
    if isinstance(value, Mapping):
        keys = list(value.keys())
        if any(not isinstance(key, str) for key in keys):
            raise CanonicalizationError("object keys must be strings")
        items = (
            f"{_escape_string(key)}:{_serialize(value[key])}"
            for key in sorted(keys, key=_sort_key)
        )
        return "{" + ",".join(items) + "}"
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return "[" + ",".join(_serialize(item) for item in value) + "]"
    raise CanonicalizationError(
        f"type {type(value).__name__} has no canonical JSON representation"
    )


def canonicalize_to_str(obj: Any) -> str:
    """Canonical JSON as ``str``. Prefer :func:`canonicalize` for hashing."""
    return _serialize(obj)


def canonicalize(obj: Any) -> bytes:
    """Canonical RFC 8785 JSON encoded as UTF-8 bytes."""
    return _serialize(obj).encode("utf-8")
