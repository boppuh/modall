"""Allowlisted execution states and payload-free failure codes."""

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

MAX_ACTIVE_RUNS_PER_WORKSPACE = 100


class RunStatus(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    SESSION_FENCED = "session_fenced"
    DISPATCH_FENCED = "dispatch_fenced"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INDETERMINATE = "indeterminate"


class JobStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INDETERMINATE = "indeterminate"


class RunEventType(StrEnum):
    ADMITTED = "admitted"
    ATTEMPT_STARTED = "attempt_started"
    SESSION_FENCED = "session_fenced"
    DISPATCH_FENCED = "dispatch_fenced"
    LEASE_LOST = "lease_lost"
    CANCEL_REQUESTED = "cancel_requested"
    TERMINAL = "terminal"
    CONTENT_EXPIRED = "content_expired"
    RESTORE_RECONCILED = "restore_reconciled"


class ExecutionFailureCode(StrEnum):
    INVALID_ARGUMENTS = "invalid_arguments"
    ARGUMENT_LIMIT = "argument_limit"
    SENSITIVE_ARGUMENTS = "sensitive_arguments"
    SCANNER_FAILED = "scanner_failed"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    INVALID_CONFIRMATION = "invalid_confirmation"
    CONFIRMATION_EXPIRED = "confirmation_expired"
    CONFIRMATION_REPLAYED = "confirmation_replayed"
    INVALID_IDEMPOTENCY_KEY = "invalid_idempotency_key"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    IDEMPOTENCY_KEY_HISTORY_INCOMPLETE = "idempotency_key_history_incomplete"
    CONFIRMATION_KEY_HISTORY_INCOMPLETE = "confirmation_key_history_incomplete"
    DISPATCH_QUARANTINED = "dispatch_quarantined"
    ACTIVE_RUN_LIMIT = "active_run_limit"
    NO_JOB_AVAILABLE = "no_job_available"
    LEASE_LOST = "lease_lost"
    INVALID_TRANSITION = "invalid_transition"
    PERSISTENCE_FAILURE = "persistence_failure"


class RunFailureCode(StrEnum):
    """Payload-free terminal codes that workers may persist in the execution ledger."""

    WORKER_LOST_BEFORE_DISPATCH = "worker_lost_before_dispatch"
    WORKER_LOST_AFTER_DISPATCH = "worker_lost_after_dispatch"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CANCELLED_BEFORE_DISPATCH = "cancelled_before_dispatch"
    RESTORE_RECONCILIATION = "restore_reconciliation"
    CONTENT_RETENTION_DEADLINE = "content_retention_deadline"
    PREPARATION_FAILED = "preparation_failed"
    SESSION_INITIALIZATION_FAILED = "session_initialization_failed"
    TOOL_CALL_FAILED = "tool_call_failed"
    INVALID_TOOL_RESULT = "invalid_tool_result"
    UNSUPPORTED_TOOL_RESULT = "unsupported_tool_result"
    SENSITIVE_TOOL_RESULT = "sensitive_tool_result"
    UPSTREAM_OUTCOME_UNKNOWN = "upstream_outcome_unknown"


@dataclass(frozen=True, slots=True)
class AcceptedToolResult:
    """Bounded, screened result content safe for durable storage."""

    payload: dict[str, object]
    canonical_digest: str
    byte_count: int


class ExecutionError(Exception):
    """A stable execution failure that carries no submitted content."""

    def __init__(self, code: ExecutionFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class HmacKeyVersion:
    version: str
    secret: bytes


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    max_argument_bytes: int = 65_536
    max_result_bytes: int = 262_144
    confirmation_ttl_seconds: int = 120
    max_run_seconds: int = 300
    argument_retention_days: int = 14
    result_retention_days: int = 14
    run_retention_days: int = 90
    max_idempotency_key_characters: int = 256
    max_historical_hmac_keys: int = 8
    schema_validation_timeout_seconds: float = 2.0
    schema_validation_memory_bytes: int = 256 * 1024 * 1024
    reconciliation_batch_size: int = 100
    max_active_runs_per_workspace: int = MAX_ACTIVE_RUNS_PER_WORKSPACE

    def __post_init__(self) -> None:
        if (
            self.max_argument_bytes <= 0
            or self.max_result_bytes <= 0
            or not 1 <= self.confirmation_ttl_seconds <= 300
            or self.max_run_seconds <= 0
            or not 1 <= self.argument_retention_days <= 14
            or not 1 <= self.result_retention_days <= 14
            or self.run_retention_days < self.argument_retention_days
            or self.run_retention_days < self.result_retention_days
            or self.max_idempotency_key_characters <= 0
            or not 1 <= self.max_historical_hmac_keys <= 16
            or not 0 < self.schema_validation_timeout_seconds <= 5
            or not math.isfinite(self.schema_validation_timeout_seconds)
            or not 64 * 1024 * 1024 <= self.schema_validation_memory_bytes <= 256 * 1024 * 1024
            or not 1 <= self.reconciliation_batch_size <= 1000
            or not 1 <= self.max_active_runs_per_workspace <= MAX_ACTIVE_RUNS_PER_WORKSPACE
        ):
            raise ValueError("invalid execution limits")


@dataclass(frozen=True, slots=True)
class RunPreflight:
    confirmation_token: str
    capability_version_id: UUID
    connection_version_id: UUID
    argument_digest: str
    expires_at: datetime
    server_observed_at: datetime


@dataclass(frozen=True, slots=True)
class JobLease:
    job_id: UUID
    run_id: UUID
    workspace_id: UUID
    worker_id: str
    lease_epoch: int
    execution_epoch: int
    expires_at: datetime


class SystemExecutionAuthority:
    """Opaque installation-scoped capability for global execution-state changes."""

    __slots__ = ()
