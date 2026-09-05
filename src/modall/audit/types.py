"""Allowlisted audit dimensions; payloads do not belong in audit rows."""

from enum import StrEnum


class AuditAction(StrEnum):
    WORKSPACE_CREATED = "workspace.created"
    MEMBERSHIP_CHANGED = "membership.changed"
    SECRET_BINDING_CREATED = "secret_binding.created"


class AuditOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    DENIED = "denied"
    FAILED = "failed"


class ResourceType(StrEnum):
    WORKSPACE = "workspace"
    MEMBERSHIP = "membership"
    SECRET_BINDING = "secret_binding"
