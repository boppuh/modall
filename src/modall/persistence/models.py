"""Identity, workspace, secret-binding, and audit persistence models."""

from datetime import UTC, datetime
from typing import Annotated, cast
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Mapper, mapped_column
from sqlalchemy.orm.attributes import get_history

from modall.audit.types import AuditAction, AuditOutcome, ResourceType
from modall.identity.types import Role
from modall.registry.types import CapabilityStatus, ConnectionLifecycle, RegistrySource, Transport


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


UuidPrimaryKey = Annotated[UUID, mapped_column(primary_key=True, default=uuid4)]
CreatedAt = Annotated[datetime, mapped_column(DateTime(timezone=True), default=utc_now)]
_TERMINAL_EXECUTION_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "timed_out", "indeterminate"}
)
_TERMINAL_EXECUTION_STATUS_SQL = "'succeeded', 'failed', 'cancelled', 'timed_out', 'indeterminate'"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("oidc_issuer", "oidc_subject"),)

    id: Mapped[UuidPrimaryKey]
    oidc_issuer: Mapped[str] = mapped_column(String(512))
    oidc_subject: Mapped[str] = mapped_column(String(512))
    display_name: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[CreatedAt]


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[UuidPrimaryKey]
    name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[CreatedAt]


class WorkspaceMembership(Base):
    __tablename__ = "workspace_memberships"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'operator', 'viewer')", name="ck_membership_role"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[CreatedAt]

    @property
    def typed_role(self) -> Role:
        return Role(self.role)


class SecretBinding(Base):
    __tablename__ = "secret_bindings"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_secret_binding_workspace_id"),
        UniqueConstraint("workspace_id", "provider", "external_reference", "version"),
        CheckConstraint("provider IN ('fixture', 'mounted_file')", name="ck_secret_provider"),
    )

    id: Mapped[UuidPrimaryKey]
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(32))
    external_reference: Mapped[str] = mapped_column(String(256))
    version: Mapped[str] = mapped_column(String(128))
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[CreatedAt]


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("outcome IN ('succeeded', 'denied', 'failed')", name="ck_audit_outcome"),
        CheckConstraint(
            "action IN ('workspace.created', 'membership.changed', 'secret_binding.created', "
            "'connection.created', 'connection.version_appended', 'connection.verified', "
            "'connection.disabled', 'connection.enabled', 'capability.version_recorded', "
            "'capability.enabled', 'capability.disabled', 'registry_entry.imported', "
            "'run.created', 'run.cancellation_requested', 'run.cancelled')",
            name="ck_audit_action",
        ),
        CheckConstraint(
            "resource_type IN ('workspace', 'membership', 'secret_binding', 'server_connection', "
            "'capability', 'registry_entry', 'run')",
            name="ck_audit_resource_type",
        ),
        Index("ix_audit_workspace_time_id", "workspace_id", "occurred_at", "id"),
    )

    id: Mapped[UuidPrimaryKey]
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    action: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str] = mapped_column(String(32))
    resource_id: Mapped[UUID]
    outcome: Mapped[str] = mapped_column(String(16))
    correlation_id: Mapped[UUID]
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    @classmethod
    def succeeded(
        cls,
        *,
        workspace_id: UUID,
        actor_user_id: UUID,
        action: AuditAction,
        resource_type: ResourceType,
        resource_id: UUID,
        correlation_id: UUID,
    ) -> "AuditEvent":
        return cls(
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            action=action.value,
            resource_type=resource_type.value,
            resource_id=resource_id,
            outcome=AuditOutcome.SUCCEEDED.value,
            correlation_id=correlation_id,
        )


class RegistryEntry(Base):
    __tablename__ = "registry_entries"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "source", "external_id"),
        CheckConstraint("source IN ('manual', 'official')", name="ck_registry_entry_source"),
        CheckConstraint(
            "source <> 'official' OR (external_id IS NOT NULL AND length(trim(external_id)) > 0)",
            name="ck_registry_entry_official_external_id",
        ),
        ForeignKeyConstraint(
            ["id", "current_version_id"],
            ["registry_entry_versions.registry_entry_id", "registry_entry_versions.id"],
            name="fk_registry_entry_current_version",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    id: Mapped[UuidPrimaryKey]
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str | None] = mapped_column(String(256))
    current_version_id: Mapped[UUID | None]
    created_at: Mapped[CreatedAt]

    @property
    def typed_source(self) -> RegistrySource:
        return RegistrySource(self.source)


class RegistryEntryVersion(Base):
    __tablename__ = "registry_entry_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "registry_entry_id"],
            ["registry_entries.workspace_id", "registry_entries.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("registry_entry_id", "id"),
        UniqueConstraint("registry_entry_id", "sequence"),
    )

    id: Mapped[UuidPrimaryKey]
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    registry_entry_id: Mapped[UUID]
    sequence: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(String(2048))
    provenance_digest: Mapped[str] = mapped_column(String(64))
    source_version: Mapped[str | None] = mapped_column(String(128))
    source_uri: Mapped[str | None] = mapped_column(String(2048))
    normalized_metadata: Mapped[dict[str, object] | None] = mapped_column(JSON)
    imported_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[CreatedAt]


class RegistrySearchCache(Base):
    __tablename__ = "registry_search_cache"
    __table_args__ = (
        CheckConstraint("provider = 'official'", name="ck_registry_search_cache_provider"),
        CheckConstraint("result_count >= 0", name="ck_registry_search_cache_result_count"),
        CheckConstraint("byte_count >= 0", name="ck_registry_search_cache_byte_count"),
        Index(
            "ix_registry_search_cache_lookup",
            "workspace_id",
            "provider",
            "query_digest",
            "expires_at",
        ),
        Index("ix_registry_search_cache_expiry", "expires_at"),
    )

    id: Mapped[UuidPrimaryKey]
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(32))
    query_digest: Mapped[str] = mapped_column(String(64))
    response_digest: Mapped[str] = mapped_column(String(64))
    normalized_results: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    result_count: Mapped[int] = mapped_column(Integer)
    byte_count: Mapped[int] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ServerConnection(Base):
    __tablename__ = "server_connections"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        CheckConstraint(
            "lifecycle IN ('verifying', 'active', 'degraded', 'disabled')",
            name="ck_connection_lifecycle",
        ),
        CheckConstraint("control_epoch >= 0", name="ck_connection_control_epoch"),
        CheckConstraint("refresh_generation >= 0", name="ck_connection_refresh_generation"),
        CheckConstraint(
            "(refresh_generation = 0 AND allocated_control_epoch IS NULL AND "
            "allocated_target_version_id IS NULL) OR "
            "(refresh_generation > 0 AND ((allocated_control_epoch IS NULL AND "
            "allocated_target_version_id IS NULL) OR "
            "(allocated_control_epoch IS NOT NULL AND "
            "allocated_target_version_id IS NOT NULL)))",
            name="ck_connection_refresh_allocation",
        ),
        CheckConstraint(
            "allocated_control_epoch IS NULL OR allocated_control_epoch >= 0",
            name="ck_connection_allocated_control_epoch",
        ),
        CheckConstraint(
            "pending_version_id IS NOT NULL OR verified_version_id IS NOT NULL",
            name="ck_connection_has_version",
        ),
        CheckConstraint(
            "pending_version_id IS NULL OR pending_version_id <> verified_version_id",
            name="ck_connection_distinct_version_pointers",
        ),
        ForeignKeyConstraint(
            ["id", "pending_version_id"],
            ["server_connection_versions.connection_id", "server_connection_versions.id"],
            name="fk_connection_pending_version",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["id", "verified_version_id"],
            ["server_connection_versions.connection_id", "server_connection_versions.id"],
            name="fk_connection_verified_version",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["id", "allocated_target_version_id"],
            ["server_connection_versions.connection_id", "server_connection_versions.id"],
            name="fk_connection_allocated_target_version",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["id", "current_snapshot_id"],
            ["discovery_snapshots.connection_id", "discovery_snapshots.id"],
            name="fk_connection_current_snapshot",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    id: Mapped[UuidPrimaryKey]
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(128))
    lifecycle: Mapped[str] = mapped_column(String(16))
    pending_version_id: Mapped[UUID | None]
    verified_version_id: Mapped[UUID | None]
    control_epoch: Mapped[int] = mapped_column(Integer, default=0)
    refresh_generation: Mapped[int] = mapped_column(Integer, default=0)
    allocated_control_epoch: Mapped[int | None] = mapped_column(Integer)
    allocated_target_version_id: Mapped[UUID | None]
    current_snapshot_id: Mapped[UUID | None]
    last_refresh_error_code: Mapped[str | None] = mapped_column(String(64))
    last_refresh_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[CreatedAt]

    @property
    def typed_lifecycle(self) -> ConnectionLifecycle:
        return ConnectionLifecycle(self.lifecycle)


class ServerConnectionVersion(Base):
    __tablename__ = "server_connection_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "connection_id"],
            ["server_connections.workspace_id", "server_connections.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "secret_binding_id"],
            ["secret_bindings.workspace_id", "secret_bindings.id"],
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("connection_id", "id"),
        UniqueConstraint("connection_id", "sequence"),
        CheckConstraint("sequence > 0", name="ck_connection_version_sequence"),
        CheckConstraint("transport = 'streamable_http'", name="ck_connection_transport"),
    )

    id: Mapped[UuidPrimaryKey]
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    connection_id: Mapped[UUID]
    sequence: Mapped[int] = mapped_column(Integer)
    endpoint_url: Mapped[str] = mapped_column(String(2048))
    secret_binding_id: Mapped[UUID | None]
    transport: Mapped[str] = mapped_column(String(32))
    policy_version: Mapped[str] = mapped_column(String(64))
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[CreatedAt]

    @property
    def typed_transport(self) -> Transport:
        return Transport(self.transport)


class Capability(Base):
    __tablename__ = "capabilities"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(
            ["workspace_id", "connection_id"],
            ["server_connections.workspace_id", "server_connections.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("connection_id", "tool_identity"),
        UniqueConstraint("connection_id", "id"),
        CheckConstraint("status_epoch >= 0", name="ck_capability_status_epoch"),
        CheckConstraint(
            "status IN ('pending_review', 'enabled', 'disabled', 'unavailable')",
            name="ck_capability_status",
        ),
        CheckConstraint(
            "pending_version_id IS NULL OR pending_version_id <> enabled_version_id",
            name="ck_capability_distinct_version_pointers",
        ),
        CheckConstraint(
            "status <> 'enabled' OR "
            "(enabled_version_id IS NOT NULL AND pending_version_id IS NULL)",
            name="ck_capability_enabled_projection",
        ),
        CheckConstraint(
            "status <> 'pending_review' OR pending_version_id IS NOT NULL",
            name="ck_capability_pending_projection",
        ),
        ForeignKeyConstraint(
            ["id", "pending_version_id"],
            ["capability_versions.capability_id", "capability_versions.id"],
            name="fk_capability_pending_version",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["id", "enabled_version_id"],
            ["capability_versions.capability_id", "capability_versions.id"],
            name="fk_capability_enabled_version",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    id: Mapped[UuidPrimaryKey]
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    connection_id: Mapped[UUID]
    tool_identity: Mapped[str] = mapped_column(String(256))
    pending_version_id: Mapped[UUID | None]
    enabled_version_id: Mapped[UUID | None]
    status: Mapped[str] = mapped_column(String(24), default=CapabilityStatus.PENDING_REVIEW.value)
    status_epoch: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[CreatedAt]

    @property
    def typed_status(self) -> CapabilityStatus:
        return CapabilityStatus(self.status)


class CapabilityVersion(Base):
    __tablename__ = "capability_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "capability_id"],
            ["capabilities.workspace_id", "capabilities.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("capability_id", "id"),
        UniqueConstraint("capability_id", "sequence"),
        CheckConstraint("sequence > 0", name="ck_capability_version_sequence"),
    )

    id: Mapped[UuidPrimaryKey]
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    capability_id: Mapped[UUID]
    sequence: Mapped[int] = mapped_column(Integer)
    display_name: Mapped[str] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(String(2048))
    input_schema: Mapped[dict[str, object]] = mapped_column(JSON)
    output_schema: Mapped[dict[str, object] | None] = mapped_column(JSON)
    metadata_digest: Mapped[str] = mapped_column(String(64))
    schema_supported: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[CreatedAt]


class McpToolBinding(Base):
    __tablename__ = "mcp_tool_bindings"
    __table_args__ = (
        UniqueConstraint("connection_id", "connection_version_id", "capability_version_id"),
        ForeignKeyConstraint(
            ["workspace_id", "capability_version_id"],
            ["capability_versions.workspace_id", "capability_versions.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "connection_version_id"],
            ["server_connection_versions.workspace_id", "server_connection_versions.id"],
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["capability_id", "capability_version_id"],
            ["capability_versions.capability_id", "capability_versions.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["connection_id", "capability_id"],
            ["capabilities.connection_id", "capabilities.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["connection_id", "connection_version_id"],
            ["server_connection_versions.connection_id", "server_connection_versions.id"],
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    capability_version_id: Mapped[UUID] = mapped_column(primary_key=True)
    capability_id: Mapped[UUID]
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    connection_id: Mapped[UUID]
    connection_version_id: Mapped[UUID]
    tool_name: Mapped[str] = mapped_column(String(256))
    protocol_revision: Mapped[str] = mapped_column(String(64))


class CapabilityStatusEvent(Base):
    __tablename__ = "capability_status_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "capability_id"],
            ["capabilities.workspace_id", "capabilities.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["capability_id", "capability_version_id"],
            ["capability_versions.capability_id", "capability_versions.id"],
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint("capability_id", "status_epoch"),
        CheckConstraint(
            "status IN ('pending_review', 'enabled', 'disabled', 'unavailable')",
            name="ck_capability_status_event_status",
        ),
        CheckConstraint("status_epoch > 0", name="ck_capability_status_event_epoch"),
    )

    id: Mapped[UuidPrimaryKey]
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    capability_id: Mapped[UUID]
    capability_version_id: Mapped[UUID | None]
    status: Mapped[str] = mapped_column(String(24))
    status_epoch: Mapped[int] = mapped_column(Integer)
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[CreatedAt]


class DiscoveryPayload(Base):
    __tablename__ = "discovery_payloads"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "canonical_digest"),
        CheckConstraint("byte_count > 0", name="ck_discovery_payload_byte_count"),
    )

    id: Mapped[UuidPrimaryKey]
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    canonical_digest: Mapped[str] = mapped_column(String(64))
    normalized_payload: Mapped[dict[str, object]] = mapped_column(JSON)
    byte_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[CreatedAt]


class DiscoverySnapshot(Base):
    __tablename__ = "discovery_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "connection_id"],
            ["server_connections.workspace_id", "server_connections.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["connection_id", "connection_version_id"],
            ["server_connection_versions.connection_id", "server_connection_versions.id"],
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "payload_id"],
            ["discovery_payloads.workspace_id", "discovery_payloads.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("connection_id", "id"),
        UniqueConstraint("connection_id", "connection_version_id", "id"),
        UniqueConstraint("connection_id", "generation"),
        CheckConstraint("generation > 0", name="ck_discovery_snapshot_generation"),
        CheckConstraint("control_epoch >= 0", name="ck_discovery_snapshot_control_epoch"),
    )

    id: Mapped[UuidPrimaryKey]
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    connection_id: Mapped[UUID]
    connection_version_id: Mapped[UUID]
    payload_id: Mapped[UUID]
    generation: Mapped[int] = mapped_column(Integer)
    control_epoch: Mapped[int] = mapped_column(Integer)
    protocol_revision: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[CreatedAt]


class DiscoverySnapshotCapability(Base):
    __tablename__ = "discovery_snapshot_capabilities"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "snapshot_id"],
            ["discovery_snapshots.workspace_id", "discovery_snapshots.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["connection_id", "connection_version_id", "snapshot_id"],
            [
                "discovery_snapshots.connection_id",
                "discovery_snapshots.connection_version_id",
                "discovery_snapshots.id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "capability_version_id"],
            ["capability_versions.workspace_id", "capability_versions.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["connection_id", "connection_version_id", "capability_version_id"],
            [
                "mcp_tool_bindings.connection_id",
                "mcp_tool_bindings.connection_version_id",
                "mcp_tool_bindings.capability_version_id",
            ],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("snapshot_id", "capability_version_id"),
    )

    id: Mapped[UuidPrimaryKey]
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    connection_id: Mapped[UUID]
    connection_version_id: Mapped[UUID]
    snapshot_id: Mapped[UUID]
    capability_version_id: Mapped[UUID]
    created_at: Mapped[CreatedAt]


class DiscoveryRefreshJob(Base):
    __tablename__ = "discovery_refresh_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "connection_id"],
            ["server_connections.workspace_id", "server_connections.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["connection_id", "connection_version_id"],
            ["server_connection_versions.connection_id", "server_connection_versions.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("connection_id", "generation"),
        CheckConstraint("generation > 0", name="ck_discovery_job_generation"),
        CheckConstraint("control_epoch >= 0", name="ck_discovery_job_control_epoch"),
        CheckConstraint("lease_epoch >= 0", name="ck_discovery_job_lease_epoch"),
        CheckConstraint(
            "status IN ('queued', 'leased', 'succeeded', 'failed', 'obsolete')",
            name="ck_discovery_job_status",
        ),
        CheckConstraint(
            "(status = 'leased' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status <> 'leased' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="ck_discovery_job_active_lease",
        ),
    )

    id: Mapped[UuidPrimaryKey]
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    connection_id: Mapped[UUID]
    connection_version_id: Mapped[UUID]
    generation: Mapped[int] = mapped_column(Integer)
    control_epoch: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_epoch: Mapped[int] = mapped_column(Integer, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[CreatedAt]


class SystemExecutionState(Base):
    __tablename__ = "system_execution_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_system_execution_state_singleton"),
        CheckConstraint("execution_epoch > 0", name="ck_system_execution_state_epoch"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    execution_epoch: Mapped[int] = mapped_column(Integer, default=1)
    dispatch_quarantined: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        CheckConstraint(
            "status IN ('queued', 'preparing', 'session_fenced', 'dispatch_fenced', "
            "'succeeded', 'failed', 'cancelled', 'timed_out', 'indeterminate')",
            name="ck_run_status",
        ),
        CheckConstraint(
            f"(status IN ({_TERMINAL_EXECUTION_STATUS_SQL})) = (terminal_at IS NOT NULL)",
            name="ck_run_terminal_time",
        ),
        CheckConstraint("connection_control_epoch >= 0", name="ck_run_connection_epoch"),
        CheckConstraint("capability_status_epoch >= 0", name="ck_run_capability_epoch"),
        CheckConstraint(
            "(arguments IS NULL AND argument_digest IS NULL) OR "
            "(arguments IS NOT NULL AND length(argument_digest) = 64)",
            name="ck_run_argument_digest",
        ),
        CheckConstraint(
            "safe_error_code IS NULL OR safe_error_code IN "
            "('worker_lost_before_dispatch', 'worker_lost_after_dispatch', "
            "'deadline_exceeded', 'cancelled_before_dispatch', 'restore_reconciliation', "
            "'content_retention_deadline', 'preparation_failed', "
            "'session_initialization_failed', 'tool_call_failed', 'invalid_tool_result', "
            "'unsupported_tool_result', 'sensitive_tool_result', "
            "'upstream_outcome_unknown')",
            name="ck_run_safe_error_code",
        ),
        CheckConstraint(
            "status <> 'succeeded' OR safe_error_code IS NULL",
            name="ck_run_success_has_no_error",
        ),
        CheckConstraint(
            "status NOT IN ('failed', 'cancelled', 'timed_out', 'indeterminate') "
            "OR safe_error_code IS NOT NULL",
            name="ck_run_terminal_has_error",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "capability_id"],
            ["capabilities.workspace_id", "capabilities.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "capability_version_id"],
            ["capability_versions.workspace_id", "capability_versions.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "connection_id"],
            ["server_connections.workspace_id", "server_connections.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "connection_version_id"],
            ["server_connection_versions.workspace_id", "server_connection_versions.id"],
            ondelete="RESTRICT",
        ),
        Index("ix_runs_workspace_created", "workspace_id", "created_at", "id"),
        Index(
            "ix_runs_arguments_expiry",
            "arguments_expires_at",
            "id",
            postgresql_where=text("arguments IS NOT NULL"),
            sqlite_where=text("arguments IS NOT NULL"),
        ),
        Index(
            "ix_runs_terminal_expiry",
            "terminal_at",
            "id",
            postgresql_where=text("terminal_at IS NOT NULL"),
            sqlite_where=text("terminal_at IS NOT NULL"),
        ),
        Index(
            "ix_runs_restore_reconciliation",
            "created_at",
            "id",
            postgresql_where=text(
                "status IN ('queued', 'preparing', 'session_fenced', 'dispatch_fenced')"
            ),
            sqlite_where=text(
                "status IN ('queued', 'preparing', 'session_fenced', 'dispatch_fenced')"
            ),
        ),
    )

    id: Mapped[UuidPrimaryKey]
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    capability_id: Mapped[UUID]
    capability_version_id: Mapped[UUID]
    connection_id: Mapped[UUID]
    connection_version_id: Mapped[UUID]
    connection_control_epoch: Mapped[int] = mapped_column(Integer)
    capability_status_epoch: Mapped[int] = mapped_column(Integer)
    protocol_revision: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), active_history=True)
    arguments: Mapped[dict[str, object] | None] = mapped_column(JSON(none_as_null=True))
    argument_digest: Mapped[str | None] = mapped_column(String(64))
    arguments_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    safe_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunResult(Base):
    __tablename__ = "run_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("byte_count > 0", name="ck_run_result_byte_count"),
        CheckConstraint("length(canonical_digest) = 64", name="ck_run_result_digest"),
        Index("ix_run_results_expiry", "expires_at", "run_id"),
    )

    run_id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    canonical_digest: Mapped[str] = mapped_column(String(64))
    byte_count: Mapped[int] = mapped_column(Integer)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("run_id"),
        CheckConstraint("execution_epoch > 0", name="ck_job_execution_epoch"),
        CheckConstraint("lease_epoch >= 0", name="ck_job_lease_epoch"),
        CheckConstraint(
            "status IN ('queued', 'leased', 'succeeded', 'failed', 'cancelled', "
            "'timed_out', 'indeterminate')",
            name="ck_job_status",
        ),
        CheckConstraint(
            "(status = 'leased' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status <> 'leased' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="ck_job_active_lease",
        ),
        CheckConstraint(
            f"(status IN ({_TERMINAL_EXECUTION_STATUS_SQL})) = (completed_at IS NOT NULL)",
            name="ck_job_completion_time",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            ondelete="CASCADE",
        ),
        Index("ix_jobs_claim", "status", "available_at", "created_at"),
        Index(
            "ix_jobs_claim_priority",
            "created_at",
            "id",
            postgresql_where=text("status IN ('queued', 'leased')"),
            sqlite_where=text("status IN ('queued', 'leased')"),
        ),
        Index(
            "ix_jobs_deadline_reconciliation",
            "deadline",
            "id",
            postgresql_where=text("status IN ('queued', 'leased')"),
            sqlite_where=text("status IN ('queued', 'leased')"),
        ),
        Index(
            "ix_jobs_lease_reconciliation",
            "lease_expires_at",
            "id",
            postgresql_where=text("status = 'leased'"),
            sqlite_where=text("status = 'leased'"),
        ),
    )

    id: Mapped[UuidPrimaryKey]
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    run_id: Mapped[UUID]
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    execution_epoch: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), active_history=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_epoch: Mapped[int] = mapped_column(Integer, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[CreatedAt]
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunAttempt(Base):
    __tablename__ = "run_attempts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("run_id", "sequence"),
        CheckConstraint("sequence > 0", name="ck_run_attempt_sequence"),
        CheckConstraint("lease_epoch > 0", name="ck_run_attempt_lease_epoch"),
        CheckConstraint(
            "status IN ('preparing', 'session_fenced', 'dispatch_fenced', 'succeeded', "
            "'failed', 'cancelled', 'timed_out', 'indeterminate')",
            name="ck_run_attempt_status",
        ),
        CheckConstraint(
            f"(status IN ({_TERMINAL_EXECUTION_STATUS_SQL})) = (terminal_at IS NOT NULL)",
            name="ck_run_attempt_terminal_time",
        ),
        CheckConstraint(
            "safe_error_code IS NULL OR safe_error_code IN "
            "('worker_lost_before_dispatch', 'worker_lost_after_dispatch', "
            "'deadline_exceeded', 'cancelled_before_dispatch', 'restore_reconciliation', "
            "'content_retention_deadline', 'preparation_failed', "
            "'session_initialization_failed', 'tool_call_failed', 'invalid_tool_result', "
            "'unsupported_tool_result', 'sensitive_tool_result', "
            "'upstream_outcome_unknown')",
            name="ck_run_attempt_safe_error_code",
        ),
        CheckConstraint(
            "status <> 'succeeded' OR safe_error_code IS NULL",
            name="ck_run_attempt_success_has_no_error",
        ),
        CheckConstraint(
            "status NOT IN ('failed', 'cancelled', 'timed_out', 'indeterminate') "
            "OR safe_error_code IS NOT NULL",
            name="ck_run_attempt_terminal_has_error",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "job_id"],
            ["jobs.workspace_id", "jobs.id"],
            ondelete="CASCADE",
        ),
        Index(
            "uq_run_attempts_active",
            "run_id",
            unique=True,
            postgresql_where=text("status IN ('preparing', 'session_fenced', 'dispatch_fenced')"),
            sqlite_where=text("status IN ('preparing', 'session_fenced', 'dispatch_fenced')"),
        ),
        Index("ix_run_attempts_job", "workspace_id", "job_id"),
    )

    id: Mapped[UuidPrimaryKey]
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    run_id: Mapped[UUID]
    job_id: Mapped[UUID]
    sequence: Mapped[int] = mapped_column(Integer)
    lease_epoch: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), active_history=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    safe_error_code: Mapped[str | None] = mapped_column(String(64))


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("run_id", "sequence"),
        CheckConstraint("sequence > 0", name="ck_run_event_sequence"),
        CheckConstraint(
            "event_type IN ('admitted', 'attempt_started', 'session_fenced', "
            "'dispatch_fenced', 'lease_lost', 'cancel_requested', 'terminal', "
            "'content_expired', 'restore_reconciled')",
            name="ck_run_event_type",
        ),
        CheckConstraint(
            "status IN ('queued', 'preparing', 'session_fenced', 'dispatch_fenced', "
            "'succeeded', 'failed', 'cancelled', 'timed_out', 'indeterminate')",
            name="ck_run_event_status",
        ),
        CheckConstraint(
            "safe_error_code IS NULL OR safe_error_code IN "
            "('worker_lost_before_dispatch', 'worker_lost_after_dispatch', "
            "'deadline_exceeded', 'cancelled_before_dispatch', 'restore_reconciliation', "
            "'content_retention_deadline', 'preparation_failed', "
            "'session_initialization_failed', 'tool_call_failed', 'invalid_tool_result', "
            "'unsupported_tool_result', 'sensitive_tool_result', "
            "'upstream_outcome_unknown')",
            name="ck_run_event_safe_error_code",
        ),
        CheckConstraint(
            "status <> 'succeeded' OR safe_error_code IS NULL",
            name="ck_run_event_success_has_no_error",
        ),
        CheckConstraint(
            "status NOT IN ('failed', 'cancelled', 'timed_out', 'indeterminate') "
            "OR safe_error_code IS NOT NULL",
            name="ck_run_event_terminal_has_error",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            ondelete="CASCADE",
        ),
    )

    id: Mapped[UuidPrimaryKey]
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    run_id: Mapped[UUID]
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24))
    safe_error_code: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ConfirmationNonce(Base):
    __tablename__ = "confirmation_nonces"
    __table_args__ = (
        UniqueConstraint("nonce_digest"),
        ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("length(nonce_digest) = 64", name="ck_confirmation_nonce_digest"),
        CheckConstraint(
            "length(key_version) BETWEEN 1 AND 32", name="ck_confirmation_nonce_key_version"
        ),
        Index("ix_confirmation_nonces_run", "workspace_id", "run_id"),
        Index("ix_confirmation_nonces_key_version", "key_version"),
    )

    id: Mapped[UuidPrimaryKey]
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    nonce_digest: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[UUID]
    key_version: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "actor_user_id", "method", "route", "key_version", "key_hmac"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "resource_id"],
            ["runs.workspace_id", "runs.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("length(key_hmac) = 64", name="ck_idempotency_key_hmac"),
        CheckConstraint("length(request_hmac) = 64", name="ck_idempotency_request_hmac"),
        CheckConstraint("resource_type = 'run'", name="ck_idempotency_resource_type"),
        CheckConstraint(
            "length(confirmation_key_version) BETWEEN 1 AND 32",
            name="ck_idempotency_confirmation_key_version",
        ),
        Index("ix_idempotency_confirmation_key_version", "confirmation_key_version"),
        Index("ix_idempotency_key_version", "key_version"),
        Index("ix_idempotency_resource", "workspace_id", "resource_id"),
        Index("ix_idempotency_expiry", "expires_at"),
    )

    id: Mapped[UuidPrimaryKey]
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    method: Mapped[str] = mapped_column(String(8))
    route: Mapped[str] = mapped_column(String(128))
    confirmation_key_version: Mapped[str] = mapped_column(String(32))
    key_version: Mapped[str] = mapped_column(String(32))
    key_hmac: Mapped[str] = mapped_column(String(64))
    request_hmac: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str] = mapped_column(String(32))
    resource_id: Mapped[UUID]
    created_at: Mapped[CreatedAt]
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def _reject_immutable_update(mapper: Mapper[object], connection: object, target: object) -> None:
    del connection
    if any(get_history(target, attribute.key).has_changes() for attribute in mapper.column_attrs):
        raise ValueError("immutable version rows cannot be updated")


def _reject_immutable_delete(mapper: Mapper[object], connection: object, target: object) -> None:
    del mapper, connection, target
    raise ValueError("immutable version rows cannot be deleted independently")


def _reject_capability_identity_update(
    mapper: Mapper[object], connection: object, target: object
) -> None:
    del mapper, connection
    capability = cast(Capability, target)
    if any(
        get_history(capability, attribute).has_changes()
        for attribute in ("workspace_id", "connection_id", "tool_identity")
    ):
        raise ValueError("capability identity cannot be updated")


def _reject_registry_entry_identity_update(
    mapper: Mapper[object], connection: object, target: object
) -> None:
    del mapper, connection
    entry = cast(RegistryEntry, target)
    if any(
        get_history(entry, attribute).has_changes()
        for attribute in ("workspace_id", "source", "external_id")
    ):
        raise ValueError("registry entry identity cannot be updated")


def _reject_terminal_status_update(
    mapper: Mapper[object], connection: object, target: object
) -> None:
    del mapper, connection
    history = get_history(target, "status")
    if history.has_changes() and any(
        status in _TERMINAL_EXECUTION_STATUSES for status in history.deleted
    ):
        raise ValueError("terminal execution status cannot be updated")


def _validate_registry_entry_insert(
    mapper: Mapper[object], connection: object, target: object
) -> None:
    del mapper, connection
    entry = cast(RegistryEntry, target)
    if entry.source == RegistrySource.OFFICIAL.value and (
        entry.external_id is None or not entry.external_id.strip()
    ):
        raise ValueError("official registry entry requires an external identity")


for immutable_model in (
    SecretBinding,
    RegistryEntryVersion,
    ServerConnectionVersion,
    CapabilityVersion,
    McpToolBinding,
    CapabilityStatusEvent,
    DiscoveryPayload,
    DiscoverySnapshot,
    DiscoverySnapshotCapability,
    RegistrySearchCache,
    RunEvent,
    RunResult,
    ConfirmationNonce,
    IdempotencyRecord,
):
    event.listen(immutable_model, "before_update", _reject_immutable_update)

event.listen(McpToolBinding, "before_delete", _reject_immutable_delete)
event.listen(CapabilityStatusEvent, "before_delete", _reject_immutable_delete)
event.listen(ConfirmationNonce, "before_delete", _reject_immutable_delete)
event.listen(IdempotencyRecord, "before_delete", _reject_immutable_delete)
event.listen(RunEvent, "before_delete", _reject_immutable_delete)
event.listen(Capability, "before_update", _reject_capability_identity_update)
event.listen(RegistryEntry, "before_update", _reject_registry_entry_identity_update)
event.listen(RegistryEntry, "before_insert", _validate_registry_entry_insert)
for execution_model in (Run, Job, RunAttempt):
    event.listen(execution_model, "before_update", _reject_terminal_status_update)
