"""Allowlisted registry and connection state dimensions."""

from enum import StrEnum


class RegistrySource(StrEnum):
    MANUAL = "manual"
    OFFICIAL = "official"


class ConnectionLifecycle(StrEnum):
    VERIFYING = "verifying"
    ACTIVE = "active"
    DEGRADED = "degraded"
    DISABLED = "disabled"


class Transport(StrEnum):
    STREAMABLE_HTTP = "streamable_http"


class CapabilityStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    ENABLED = "enabled"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"
