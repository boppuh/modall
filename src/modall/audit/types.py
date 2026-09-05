"""Allowlisted audit dimensions; payloads do not belong in audit rows."""

from enum import StrEnum


class AuditAction(StrEnum):
    WORKSPACE_CREATED = "workspace.created"
    MEMBERSHIP_CHANGED = "membership.changed"
    SECRET_BINDING_CREATED = "secret_binding.created"
    CONNECTION_CREATED = "connection.created"
    CONNECTION_VERSION_APPENDED = "connection.version_appended"
    CONNECTION_VERIFIED = "connection.verified"
    CONNECTION_DISABLED = "connection.disabled"
    CONNECTION_ENABLED = "connection.enabled"
    CAPABILITY_VERSION_RECORDED = "capability.version_recorded"
    CAPABILITY_ENABLED = "capability.enabled"
    CAPABILITY_DISABLED = "capability.disabled"


class AuditOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    DENIED = "denied"
    FAILED = "failed"


class ResourceType(StrEnum):
    WORKSPACE = "workspace"
    MEMBERSHIP = "membership"
    SECRET_BINDING = "secret_binding"
    SERVER_CONNECTION = "server_connection"
    CAPABILITY = "capability"
