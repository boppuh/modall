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


class DiscoveryFailureCode(StrEnum):
    AUTHENTICATION_FAILED = "authentication_failed"
    ENDPOINT_REJECTED = "endpoint_rejected"
    PROTOCOL_MISMATCH = "protocol_mismatch"
    RESPONSE_LIMIT = "response_limit"
    TIMEOUT = "timeout"
    TRANSPORT_FAILED = "transport_failed"
    INVALID_METADATA = "invalid_metadata"


class RefreshJobStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OBSOLETE = "obsolete"
