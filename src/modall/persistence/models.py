"""Identity, workspace, secret-binding, and audit persistence models."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
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
            "'capability.enabled', 'capability.disabled')",
            name="ck_audit_action",
        ),
        CheckConstraint(
            "resource_type IN ('workspace', 'membership', 'secret_binding', 'server_connection', "
            "'capability')",
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
    created_at: Mapped[CreatedAt]


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
            "(refresh_generation > 0 AND allocated_control_epoch IS NOT NULL AND "
            "allocated_target_version_id IS NOT NULL)",
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
    created_at: Mapped[CreatedAt]


class McpToolBinding(Base):
    __tablename__ = "mcp_tool_bindings"
    __table_args__ = (
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


def _reject_immutable_update(mapper: Mapper[object], connection: object, target: object) -> None:
    del connection
    if any(get_history(target, attribute.key).has_changes() for attribute in mapper.column_attrs):
        raise ValueError("immutable version rows cannot be updated")


for immutable_model in (
    SecretBinding,
    RegistryEntryVersion,
    ServerConnectionVersion,
    CapabilityVersion,
    McpToolBinding,
    CapabilityStatusEvent,
):
    event.listen(immutable_model, "before_update", _reject_immutable_update)
