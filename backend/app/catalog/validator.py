"""JSON Schema validation for category schemas and item attributes.

Two distinct jobs, deliberately kept apart:

**Meta-validation** -- is a *submitted schema* a legal Draft 2020-12 document,
and is it safe to store? Run once, at category creation, against operator input.

**Attribute validation** -- do an *item's attributes* satisfy a stored schema?
Run on every item write, against weaver input.

They fail with different codes because they are different people's mistakes.

Three hardening decisions worth stating:

*Closed by default.* ``additionalProperties: false`` is injected on the root if
a submitted schema omits it. An open schema accepts ``warp_cout: 120`` silently,
the typo lands in JSONB, and nobody ever sees that attribute again. On a GI
provenance record that is worse than a rejection.

*No remote ``$ref``.* A schema that fetches over the network at validation time
turns every item registration into an outbound request -- a demo that dies on
conference Wi-Fi, and an SSRF vector pointed at whatever the operator typed.

*Bounded.* Size, depth, and property-count limits, because a stored schema is
executed against user input on a hot path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing.exceptions import Unresolvable

from app.core.errors import ErrorCode, ValidationError

__all__ = [
    "MAX_NESTING_DEPTH",
    "MAX_PROPERTIES",
    "MAX_SCHEMA_BYTES",
    "FieldError",
    "compile_schema",
    "normalize_schema",
    "validate_attributes",
    "validate_schema_document",
]

MAX_SCHEMA_BYTES = 64 * 1024
MAX_NESTING_DEPTH = 10
MAX_PROPERTIES = 100

# Anything not resolvable offline. A relative "#/$defs/..." pointer is fine;
# a scheme-bearing URI is not.
_REMOTE_REF_SCHEMES = ("http://", "https://", "file://", "ftp://")


@dataclass(frozen=True, slots=True)
class FieldError:
    """One validation failure, addressed by RFC 6901 JSON Pointer."""

    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


def _pointer(parts: Any) -> str:
    """Render a jsonschema error path as an RFC 6901 pointer.

    ``/`` for the document root, so a root-level error is addressable rather
    than reported against an empty string.
    """
    segments = [str(part).replace("~", "~0").replace("/", "~1") for part in parts]
    return "/" + "/".join(segments) if segments else "/"


def _walk_depth(node: Any, depth: int = 0) -> int:
    if depth > MAX_NESTING_DEPTH:
        return depth
    if isinstance(node, dict):
        return max((_walk_depth(value, depth + 1) for value in node.values()), default=depth)
    if isinstance(node, list):
        return max((_walk_depth(value, depth + 1) for value in node), default=depth)
    return depth


def _count_properties(node: Any) -> int:
    total = 0
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            total += len(properties)
        total += sum(_count_properties(value) for value in node.values())
    elif isinstance(node, list):
        total += sum(_count_properties(value) for value in node)
    return total


def _find_remote_refs(node: Any, trail: tuple[str, ...] = ()) -> list[FieldError]:
    found: list[FieldError] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                if value.startswith(_REMOTE_REF_SCHEMES):
                    found.append(
                        FieldError(
                            path=_pointer([*trail, "$ref"]),
                            message=(
                                "remote $ref is not allowed; schemas must resolve offline"
                            ),
                        )
                    )
            else:
                found.extend(_find_remote_refs(value, (*trail, key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_find_remote_refs(value, (*trail, str(index))))
    return found


def _reject(errors: list[FieldError], code: ErrorCode, message: str) -> None:
    raise ValidationError(
        code=code,
        status=422,
        message=message,
        details={"errors": [error.as_dict() for error in errors]},
    )


def normalize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return *schema* with the project's invariants applied.

    Currently one: the root is closed. Nested objects are left as the author
    wrote them -- forcing closure all the way down would break legitimate
    open-ended sub-objects, and the root is where typo'd attribute keys land.
    """
    normalized = dict(schema)
    if normalized.get("type") == "object" or "properties" in normalized:
        normalized.setdefault("additionalProperties", False)
    return normalized


def validate_schema_document(schema: Any) -> dict[str, Any]:
    """Meta-validate a submitted schema and return the normalized form.

    Raises ``INVALID_CATEGORY_SCHEMA`` (422) with the location of the problem.
    """
    if not isinstance(schema, dict):
        _reject(
            [FieldError("/", "attribute_schema must be a JSON object")],
            ErrorCode.INVALID_CATEGORY_SCHEMA,
            "attribute schema is not a JSON object",
        )

    encoded = json.dumps(schema).encode("utf-8")
    if len(encoded) > MAX_SCHEMA_BYTES:
        _reject(
            [
                FieldError(
                    "/", f"schema is {len(encoded)} bytes; the limit is {MAX_SCHEMA_BYTES}"
                )
            ],
            ErrorCode.INVALID_CATEGORY_SCHEMA,
            "attribute schema is too large",
        )

    depth = _walk_depth(schema)
    if depth > MAX_NESTING_DEPTH:
        _reject(
            [FieldError("/", f"schema nests {depth} levels; the limit is {MAX_NESTING_DEPTH}")],
            ErrorCode.INVALID_CATEGORY_SCHEMA,
            "attribute schema is nested too deeply",
        )

    property_count = _count_properties(schema)
    if property_count > MAX_PROPERTIES:
        _reject(
            [
                FieldError(
                    "/", f"schema declares {property_count} properties; the limit is "
                    f"{MAX_PROPERTIES}"
                )
            ],
            ErrorCode.INVALID_CATEGORY_SCHEMA,
            "attribute schema declares too many properties",
        )

    remote = _find_remote_refs(schema)
    if remote:
        _reject(remote, ErrorCode.INVALID_CATEGORY_SCHEMA, "attribute schema uses a remote $ref")

    try:
        # The validator class is pinned, never inferred from a submitted
        # `$schema` keyword -- that value is operator input, and letting it
        # choose the dialect would let it choose weaker validation.
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        _reject(
            [FieldError(_pointer(exc.absolute_path), exc.message)],
            ErrorCode.INVALID_CATEGORY_SCHEMA,
            "attribute schema is not a valid Draft 2020-12 document",
        )

    normalized = normalize_schema(schema)

    # Compile once here so a schema that meta-validates but cannot actually be
    # used fails at creation rather than at the first item registration.
    try:
        Draft202012Validator(normalized)
    except Exception as exc:  # noqa: BLE001 - reported, never propagated raw
        _reject(
            [FieldError("/", str(exc))],
            ErrorCode.INVALID_CATEGORY_SCHEMA,
            "attribute schema could not be compiled",
        )

    return normalized


def compile_schema(schema: dict[str, Any]) -> Draft202012Validator:
    """Build a reusable validator. Compilation is the expensive part."""
    return Draft202012Validator(schema)


def validate_attributes(validator: Draft202012Validator, attributes: Any) -> None:
    """Check item attributes, or raise ``ATTRIBUTE_VALIDATION_FAILED`` (422).

    Every failure is reported, not just the first -- a weaver filling a form
    should see everything wrong with it in one round trip.
    """
    if not isinstance(attributes, dict):
        _reject(
            [FieldError("/", "attributes must be a JSON object")],
            ErrorCode.ATTRIBUTE_VALIDATION_FAILED,
            "attributes are not a JSON object",
        )

    try:
        raw_errors = sorted(validator.iter_errors(attributes), key=lambda e: list(e.absolute_path))
    except Unresolvable as exc:
        # A stored schema with an unresolvable reference. 422 rather than 500:
        # nothing is going to fix itself on retry, and the traceback belongs in
        # the log, not the response.
        _reject(
            [FieldError("/", "schema reference could not be resolved")],
            ErrorCode.ATTRIBUTE_VALIDATION_FAILED,
            "category schema is not usable",
        )
        raise AssertionError from exc  # pragma: no cover - _reject always raises

    if not raw_errors:
        return

    errors: list[FieldError] = []
    for error in raw_errors:
        path = list(error.absolute_path)
        # additionalProperties reports against the parent, so the offending key
        # only appears in the message. Lift it into the path -- "/warp_cout is
        # not a recognised attribute" is actionable; "/ has an unexpected
        # property" sends somebody hunting.
        if error.validator == "additionalProperties":
            for key in sorted(_unexpected_keys(error)):
                errors.append(
                    FieldError(
                        path=_pointer([*path, key]),
                        message=f"'{key}' is not an attribute of this category",
                    )
                )
            continue
        if error.validator == "required":
            missing = _missing_key(error)
            if missing is not None:
                errors.append(
                    FieldError(
                        path=_pointer([*path, missing]),
                        message=f"'{missing}' is required",
                    )
                )
                continue
        errors.append(FieldError(path=_pointer(path), message=error.message))

    _reject(
        errors,
        ErrorCode.ATTRIBUTE_VALIDATION_FAILED,
        "attributes do not satisfy this category's schema",
    )


def _unexpected_keys(error: Any) -> set[str]:
    """Recover the offending keys from an additionalProperties error."""
    instance = error.instance
    if not isinstance(instance, dict):
        return set()
    allowed = set(error.schema.get("properties", {}))
    return set(instance) - allowed


def _missing_key(error: Any) -> str | None:
    """Recover the missing property name from a `required` error message."""
    message = str(error.message)
    if "'" not in message:
        return None
    return message.split("'")[1]
