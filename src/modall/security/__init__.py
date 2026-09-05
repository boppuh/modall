"""Shared fail-closed content validation."""

from modall.security.metadata import (
    MetadataValidationError,
    contains_obvious_secret,
    contains_sensitive_json,
    validate_capability_scalars,
    validate_schema_payload,
)

__all__ = [
    "MetadataValidationError",
    "contains_obvious_secret",
    "contains_sensitive_json",
    "validate_capability_scalars",
    "validate_schema_payload",
]
