"""Identity, workspace, secret-binding, and audit persistence models."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from modall.audit.types import AuditAction, AuditOutcome, ResourceType
from modall.identity.types import Role


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
            "action IN ('workspace.created', 'membership.changed', 'secret_binding.created')",
            name="ck_audit_action",
        ),
        CheckConstraint(
            "resource_type IN ('workspace', 'membership', 'secret_binding')",
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
