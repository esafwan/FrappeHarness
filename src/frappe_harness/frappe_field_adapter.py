"""Canonical semantic-field to Frappe-16 metadata projection."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import FieldType


@dataclass(frozen=True)
class FrappeFieldProjection:
    fieldtype: str
    options: str | None = None


def project_field_type(field_type: FieldType) -> FrappeFieldProjection:
    if field_type is FieldType.URL:
        return FrappeFieldProjection("Data", "URL")
    return FrappeFieldProjection(field_type.value)
