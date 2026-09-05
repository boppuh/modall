"""Add immutable discovery payloads and per-refresh observations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260905_0003"
down_revision: str | None = "20260905_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("server_connections", sa.Column("current_snapshot_id", sa.Uuid()))
    op.add_column("server_connections", sa.Column("last_refresh_error_code", sa.String(64)))
    op.add_column("server_connections", sa.Column("last_refresh_at", sa.DateTime(timezone=True)))
    op.add_column(
        "capability_versions",
        sa.Column("schema_supported", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.alter_column("capability_versions", "schema_supported", server_default=None)

    op.create_table(
        "discovery_payloads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_digest", sa.String(64), nullable=False),
        sa.Column("normalized_payload", sa.JSON(), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("byte_count > 0", name="ck_discovery_payload_byte_count"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id"),
        sa.UniqueConstraint("workspace_id", "canonical_digest"),
    )
    op.create_table(
        "discovery_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("connection_version_id", sa.Uuid(), nullable=False),
        sa.Column("payload_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("control_epoch", sa.Integer(), nullable=False),
        sa.Column("protocol_revision", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("generation > 0", name="ck_discovery_snapshot_generation"),
        sa.CheckConstraint("control_epoch >= 0", name="ck_discovery_snapshot_control_epoch"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "connection_id"],
            ["server_connections.workspace_id", "server_connections.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "connection_version_id"],
            ["server_connection_versions.workspace_id", "server_connection_versions.id"],
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "payload_id"],
            ["discovery_payloads.workspace_id", "discovery_payloads.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id"),
        sa.UniqueConstraint("connection_id", "id"),
        sa.UniqueConstraint("connection_id", "generation"),
    )
    op.create_foreign_key(
        "fk_connection_current_snapshot",
        "server_connections",
        "discovery_snapshots",
        ["id", "current_snapshot_id"],
        ["connection_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_table(
        "discovery_refresh_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("connection_version_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("control_epoch", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("lease_owner", sa.String(128)),
        sa.Column("lease_epoch", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("generation > 0", name="ck_discovery_job_generation"),
        sa.CheckConstraint("control_epoch >= 0", name="ck_discovery_job_control_epoch"),
        sa.CheckConstraint("lease_epoch >= 0", name="ck_discovery_job_lease_epoch"),
        sa.CheckConstraint(
            "status IN ('queued', 'leased', 'succeeded', 'failed', 'obsolete')",
            name="ck_discovery_job_status",
        ),
        sa.CheckConstraint(
            "(status = 'leased' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status <> 'leased' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="ck_discovery_job_active_lease",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "connection_id"],
            ["server_connections.workspace_id", "server_connections.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id", "connection_version_id"],
            ["server_connection_versions.connection_id", "server_connection_versions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", "generation"),
    )


def downgrade() -> None:
    op.drop_table("discovery_refresh_jobs")
    op.drop_constraint("fk_connection_current_snapshot", "server_connections", type_="foreignkey")
    op.drop_table("discovery_snapshots")
    op.drop_table("discovery_payloads")
    op.drop_column("capability_versions", "schema_supported")
    op.drop_column("server_connections", "last_refresh_at")
    op.drop_column("server_connections", "last_refresh_error_code")
    op.drop_column("server_connections", "current_snapshot_id")
