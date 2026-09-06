"""Add bounded official Registry cache and imported provenance."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260906_0004"
down_revision: str | None = "20260905_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_audit_action", "audit_events", type_="check")
    op.drop_constraint("ck_audit_resource_type", "audit_events", type_="check")
    op.create_check_constraint(
        "ck_audit_action",
        "audit_events",
        "action IN ('workspace.created', 'membership.changed', 'secret_binding.created', "
        "'connection.created', 'connection.version_appended', 'connection.verified', "
        "'connection.disabled', 'connection.enabled', 'capability.version_recorded', "
        "'capability.enabled', 'capability.disabled', 'registry_entry.imported')",
    )
    op.create_check_constraint(
        "ck_audit_resource_type",
        "audit_events",
        "resource_type IN ('workspace', 'membership', 'secret_binding', 'server_connection', "
        "'capability', 'registry_entry')",
    )
    op.add_column(
        "registry_entry_versions", sa.Column("source_version", sa.String(128), nullable=True)
    )
    op.add_column(
        "registry_entry_versions", sa.Column("source_uri", sa.String(2048), nullable=True)
    )
    op.add_column(
        "registry_entry_versions", sa.Column("normalized_metadata", sa.JSON(), nullable=True)
    )
    op.add_column(
        "registry_entry_versions", sa.Column("imported_by_user_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_registry_entry_version_imported_by",
        "registry_entry_versions",
        "users",
        ["imported_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_table(
        "registry_search_cache",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("query_digest", sa.String(64), nullable=False),
        sa.Column("response_digest", sa.String(64), nullable=False),
        sa.Column("normalized_results", sa.JSON(), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("provider = 'official'", name="ck_registry_search_cache_provider"),
        sa.CheckConstraint("result_count >= 0", name="ck_registry_search_cache_result_count"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_registry_search_cache_lookup",
        "registry_search_cache",
        ["workspace_id", "provider", "query_digest", "expires_at"],
    )
    op.create_index("ix_registry_search_cache_expiry", "registry_search_cache", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_registry_search_cache_expiry", table_name="registry_search_cache")
    op.drop_index("ix_registry_search_cache_lookup", table_name="registry_search_cache")
    op.drop_table("registry_search_cache")
    op.drop_constraint(
        "fk_registry_entry_version_imported_by", "registry_entry_versions", type_="foreignkey"
    )
    op.drop_column("registry_entry_versions", "imported_by_user_id")
    op.drop_column("registry_entry_versions", "normalized_metadata")
    op.drop_column("registry_entry_versions", "source_uri")
    op.drop_column("registry_entry_versions", "source_version")
    op.drop_constraint("ck_audit_resource_type", "audit_events", type_="check")
    op.drop_constraint("ck_audit_action", "audit_events", type_="check")
    op.execute(
        "DELETE FROM audit_events WHERE action = 'registry_entry.imported' "
        "OR resource_type = 'registry_entry'"
    )
    op.create_check_constraint(
        "ck_audit_resource_type",
        "audit_events",
        "resource_type IN ('workspace', 'membership', 'secret_binding', 'server_connection', "
        "'capability')",
    )
    op.create_check_constraint(
        "ck_audit_action",
        "audit_events",
        "action IN ('workspace.created', 'membership.changed', 'secret_binding.created', "
        "'connection.created', 'connection.version_appended', 'connection.verified', "
        "'connection.disabled', 'connection.enabled', 'capability.version_recorded', "
        "'capability.enabled', 'capability.disabled')",
    )
