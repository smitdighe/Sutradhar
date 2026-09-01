"""Request and response models for the catalog endpoints."""

from __future__ import annotations

import re
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.clock import UtcDatetime

__all__ = [
    "CategoryDetail",
    "CategoryListResponse",
    "CategorySummary",
    "CategoryVersionsResponse",
    "CreateCategoryRequest",
    "CreateVersionRequest",
    "SchemaDiff",
    "UpdateCategoryRequest",
    "ValidateAttributesRequest",
    "ValidateAttributesResponse",
]

# Lowercase, hyphen-separated. Slugs appear in URLs and on printed tags, so
# they are kept to the character set that survives both.
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class CreateCategoryRequest(BaseModel):
    """Create a category at v1.

    ``schema_version`` is absent on purpose: v1 is implied, and letting a caller
    pick a starting version would allow gaps that make "the previous version"
    ambiguous.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    slug: str = Field(min_length=2, max_length=64)
    display_name: str = Field(min_length=1, max_length=120)
    is_textile: bool = True
    quantity_unit: str = Field(min_length=1, max_length=32)
    attribute_schema: dict[str, Any]

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, value: str) -> str:
        if not SLUG_PATTERN.match(value):
            raise ValueError("slug must be lowercase alphanumeric words separated by hyphens")
        return value


class CreateVersionRequest(BaseModel):
    """Publish v(n+1) of an existing category. Only the schema changes."""

    model_config = ConfigDict(extra="ignore")

    attribute_schema: dict[str, Any]


class UpdateCategoryRequest(BaseModel):
    """Mutable category metadata.

    ``slug`` and ``attribute_schema`` are absent, not optional. A slug is
    referenced by URLs and printed tags; a schema change is a new version by
    definition. Neither is reachable through this model, so neither can be
    changed by any shape of request body.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    is_active: bool | None = None


class ValidateAttributesRequest(BaseModel):
    """Dry-run attribute check. Writes nothing."""

    model_config = ConfigDict(extra="ignore")

    attributes: dict[str, Any]
    schema_version: int | None = None


class ValidateAttributesResponse(BaseModel):
    valid: bool
    slug: str
    schema_version: int


class CategorySummary(BaseModel):
    """A category in a listing."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    display_name: str
    is_textile: bool
    quantity_unit: str
    schema_version: int
    is_active: bool
    created_at: UtcDatetime


class CategoryDetail(CategorySummary):
    """A category with its schema."""

    attribute_schema: dict[str, Any]


class CategoryListResponse(BaseModel):
    """Collection envelope, per app.core.envelope."""

    data: list[CategorySummary]
    pagination: dict[str, Any]


class CategoryVersionsResponse(BaseModel):
    slug: str
    data: list[CategorySummary]


class SchemaDiff(BaseModel):
    """What publishing this version changes, relative to the previous one.

    Returned from the create-version endpoint so an operator sees the
    consequence at the moment they cause it, rather than discovering it when a
    weaver's next registration starts failing.
    """

    added: list[str]
    removed: list[str]
    type_changed: list[dict[str, str]]
    newly_required: list[str]
    no_longer_required: list[str]

    @property
    def is_breaking(self) -> bool:
        """True when an item valid under the previous version might now fail."""
        return bool(self.removed or self.type_changed or self.newly_required)


class CreateVersionResponse(BaseModel):
    category: CategoryDetail
    diff: SchemaDiff
    breaking: bool
