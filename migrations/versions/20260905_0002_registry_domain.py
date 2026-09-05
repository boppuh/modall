"""Create registry identities, immutable versions, and lifecycle projections."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260905_0002"
down_revision: str | None = "20260905_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_secret_binding_workspace_id", "secret_bindings", ["workspace_id", "id"]
    )
    op.drop_constraint("ck_audit_action", "audit_events", type_="check")
    op.drop_constraint("ck_audit_resource_type", "audit_events", type_="check")
    op.create_check_constraint(
        "ck_audit_action",
        "audit_events",
        "action IN ('workspace.created', 'membership.changed', 'secret_binding.created', "
        "'connection.created', 'connection.version_appended', 'connection.verified', "
        "'connection.disabled', 'connection.enabled', 'capability.version_recorded', "
        "'capability.enabled', 'capability.disabled')",
    )
    op.create_check_constraint(
        "ck_audit_resource_type",
        "audit_events",
        "resource_type IN ('workspace', 'membership', 'secret_binding', 'server_connection', "
        "'capability')",
    )

    op.create_table(
        "registry_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("external_id", sa.String(256), nullable=True),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source <> 'official' OR (external_id IS NOT NULL AND length(trim(external_id)) > 0)",
            name="ck_registry_entry_official_external_id",
        ),
        sa.CheckConstraint("source IN ('manual', 'official')", name="ck_registry_entry_source"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id"),
        sa.UniqueConstraint("workspace_id", "source", "external_id"),
    )
    op.create_table(
        "registry_entry_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("registry_entry_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("description", sa.String(2048), nullable=True),
        sa.Column("provenance_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "registry_entry_id"],
            ["registry_entries.workspace_id", "registry_entries.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id"),
        sa.UniqueConstraint("registry_entry_id", "id"),
        sa.UniqueConstraint("registry_entry_id", "sequence"),
    )
    op.create_foreign_key(
        "fk_registry_entry_current_version",
        "registry_entries",
        "registry_entry_versions",
        ["id", "current_version_id"],
        ["registry_entry_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "server_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("lifecycle", sa.String(16), nullable=False),
        sa.Column("pending_version_id", sa.Uuid(), nullable=True),
        sa.Column("verified_version_id", sa.Uuid(), nullable=True),
        sa.Column("control_epoch", sa.Integer(), nullable=False),
        sa.Column("refresh_generation", sa.Integer(), nullable=False),
        sa.Column("allocated_control_epoch", sa.Integer(), nullable=True),
        sa.Column("allocated_target_version_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("control_epoch >= 0", name="ck_connection_control_epoch"),
        sa.CheckConstraint("refresh_generation >= 0", name="ck_connection_refresh_generation"),
        sa.CheckConstraint(
            "(refresh_generation = 0 AND allocated_control_epoch IS NULL AND "
            "allocated_target_version_id IS NULL) OR "
            "(refresh_generation > 0 AND ((allocated_control_epoch IS NULL AND "
            "allocated_target_version_id IS NULL) OR "
            "(allocated_control_epoch IS NOT NULL AND "
            "allocated_target_version_id IS NOT NULL)))",
            name="ck_connection_refresh_allocation",
        ),
        sa.CheckConstraint(
            "allocated_control_epoch IS NULL OR allocated_control_epoch >= 0",
            name="ck_connection_allocated_control_epoch",
        ),
        sa.CheckConstraint(
            "lifecycle IN ('verifying', 'active', 'degraded', 'disabled')",
            name="ck_connection_lifecycle",
        ),
        sa.CheckConstraint(
            "pending_version_id IS NOT NULL OR verified_version_id IS NOT NULL",
            name="ck_connection_has_version",
        ),
        sa.CheckConstraint(
            "pending_version_id IS NULL OR pending_version_id <> verified_version_id",
            name="ck_connection_distinct_version_pointers",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id"),
    )
    op.create_table(
        "server_connection_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("endpoint_url", sa.String(2048), nullable=False),
        sa.Column("secret_binding_id", sa.Uuid(), nullable=True),
        sa.Column("transport", sa.String(32), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence > 0", name="ck_connection_version_sequence"),
        sa.CheckConstraint("transport = 'streamable_http'", name="ck_connection_transport"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "connection_id"],
            ["server_connections.workspace_id", "server_connections.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "secret_binding_id"],
            ["secret_bindings.workspace_id", "secret_bindings.id"],
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id"),
        sa.UniqueConstraint("connection_id", "id"),
        sa.UniqueConstraint("connection_id", "sequence"),
    )
    op.create_foreign_key(
        "fk_connection_pending_version",
        "server_connections",
        "server_connection_versions",
        ["id", "pending_version_id"],
        ["connection_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_connection_verified_version",
        "server_connections",
        "server_connection_versions",
        ["id", "verified_version_id"],
        ["connection_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_connection_allocated_target_version",
        "server_connections",
        "server_connection_versions",
        ["id", "allocated_target_version_id"],
        ["connection_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "capabilities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("tool_identity", sa.String(256), nullable=False),
        sa.Column("pending_version_id", sa.Uuid(), nullable=True),
        sa.Column("enabled_version_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("status_epoch", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status_epoch >= 0", name="ck_capability_status_epoch"),
        sa.CheckConstraint(
            "status IN ('pending_review', 'enabled', 'disabled', 'unavailable')",
            name="ck_capability_status",
        ),
        sa.CheckConstraint(
            "pending_version_id IS NULL OR pending_version_id <> enabled_version_id",
            name="ck_capability_distinct_version_pointers",
        ),
        sa.CheckConstraint(
            "status <> 'enabled' OR "
            "(enabled_version_id IS NOT NULL AND pending_version_id IS NULL)",
            name="ck_capability_enabled_projection",
        ),
        sa.CheckConstraint(
            "status <> 'pending_review' OR pending_version_id IS NOT NULL",
            name="ck_capability_pending_projection",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "connection_id"],
            ["server_connections.workspace_id", "server_connections.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", "tool_identity"),
        sa.UniqueConstraint("connection_id", "id"),
        sa.UniqueConstraint("workspace_id", "id"),
    )
    op.create_table(
        "capability_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("capability_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(256), nullable=False),
        sa.Column("description", sa.String(2048), nullable=True),
        sa.Column("input_schema", sa.JSON(), nullable=False),
        sa.Column("output_schema", sa.JSON(), nullable=True),
        sa.Column("metadata_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence > 0", name="ck_capability_version_sequence"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "capability_id"],
            ["capabilities.workspace_id", "capabilities.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id"),
        sa.UniqueConstraint("capability_id", "id"),
        sa.UniqueConstraint("capability_id", "sequence"),
    )
    op.create_foreign_key(
        "fk_capability_pending_version",
        "capabilities",
        "capability_versions",
        ["id", "pending_version_id"],
        ["capability_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_capability_enabled_version",
        "capabilities",
        "capability_versions",
        ["id", "enabled_version_id"],
        ["capability_id", "id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_table(
        "mcp_tool_bindings",
        sa.Column("capability_version_id", sa.Uuid(), nullable=False),
        sa.Column("capability_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("connection_version_id", sa.Uuid(), nullable=False),
        sa.Column("tool_name", sa.String(256), nullable=False),
        sa.Column("protocol_revision", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "capability_version_id"],
            ["capability_versions.workspace_id", "capability_versions.id"],
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
            ["capability_id", "capability_version_id"],
            ["capability_versions.capability_id", "capability_versions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id", "capability_id"],
            ["capabilities.connection_id", "capabilities.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id", "connection_version_id"],
            ["server_connection_versions.connection_id", "server_connection_versions.id"],
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("capability_version_id"),
    )
    op.create_table(
        "capability_status_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("capability_id", sa.Uuid(), nullable=False),
        sa.Column("capability_version_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("status_epoch", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status_epoch > 0", name="ck_capability_status_event_epoch"),
        sa.CheckConstraint(
            "status IN ('pending_review', 'enabled', 'disabled', 'unavailable')",
            name="ck_capability_status_event_status",
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "capability_id"],
            ["capabilities.workspace_id", "capabilities.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["capability_id", "capability_version_id"],
            ["capability_versions.capability_id", "capability_versions.id"],
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("capability_id", "status_epoch"),
    )

    op.execute(
        "CREATE FUNCTION modall_reject_immutable_update() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'immutable version row'; END; $$"
    )
    for table in (
        "secret_bindings",
        "registry_entry_versions",
        "server_connection_versions",
        "capability_versions",
        "mcp_tool_bindings",
        "capability_status_events",
    ):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION modall_reject_immutable_update()"
        )
    op.execute(
        "CREATE FUNCTION modall_reject_binding_delete() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM capability_versions "
        "WHERE id = OLD.capability_version_id) THEN "
        "RAISE EXCEPTION 'immutable binding row'; END IF; RETURN OLD; END; $$"
    )
    op.execute(
        "CREATE TRIGGER mcp_tool_bindings_immutable_delete "
        "BEFORE DELETE ON mcp_tool_bindings FOR EACH ROW "
        "EXECUTE FUNCTION modall_reject_binding_delete()"
    )
    op.execute(
        "CREATE FUNCTION modall_reject_capability_identity_update() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        "IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id "
        "OR NEW.connection_id IS DISTINCT FROM OLD.connection_id "
        "OR NEW.tool_identity IS DISTINCT FROM OLD.tool_identity THEN "
        "RAISE EXCEPTION 'immutable capability identity'; END IF; RETURN NEW; END; $$"
    )
    op.execute(
        "CREATE TRIGGER capabilities_immutable_identity "
        "BEFORE UPDATE ON capabilities FOR EACH ROW "
        "EXECUTE FUNCTION modall_reject_capability_identity_update()"
    )
    op.execute(
        "CREATE FUNCTION modall_reject_status_event_delete() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM capabilities WHERE id = OLD.capability_id) THEN "
        "RAISE EXCEPTION 'immutable status event row'; END IF; RETURN OLD; END; $$"
    )
    op.execute(
        "CREATE TRIGGER capability_status_events_immutable_delete "
        "BEFORE DELETE ON capability_status_events FOR EACH ROW "
        "EXECUTE FUNCTION modall_reject_status_event_delete()"
    )
    op.execute(
        "CREATE FUNCTION modall_reject_registry_entry_identity_update() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        "IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id "
        "OR NEW.source IS DISTINCT FROM OLD.source "
        "OR NEW.external_id IS DISTINCT FROM OLD.external_id THEN "
        "RAISE EXCEPTION 'immutable registry entry identity'; END IF; RETURN NEW; END; $$"
    )
    op.execute(
        "CREATE TRIGGER registry_entries_immutable_identity "
        "BEFORE UPDATE ON registry_entries FOR EACH ROW "
        "EXECUTE FUNCTION modall_reject_registry_entry_identity_update()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS registry_entries_immutable_identity ON registry_entries")
    op.execute("DROP FUNCTION IF EXISTS modall_reject_registry_entry_identity_update()")
    op.execute(
        "DROP TRIGGER IF EXISTS capability_status_events_immutable_delete "
        "ON capability_status_events"
    )
    op.execute("DROP FUNCTION IF EXISTS modall_reject_status_event_delete()")
    op.execute("DROP TRIGGER IF EXISTS capabilities_immutable_identity ON capabilities")
    op.execute("DROP FUNCTION IF EXISTS modall_reject_capability_identity_update()")
    op.execute("DROP TRIGGER IF EXISTS mcp_tool_bindings_immutable_delete ON mcp_tool_bindings")
    op.execute("DROP FUNCTION IF EXISTS modall_reject_binding_delete()")
    for table in (
        "secret_bindings",
        "registry_entry_versions",
        "server_connection_versions",
        "capability_versions",
        "mcp_tool_bindings",
        "capability_status_events",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.execute("DROP FUNCTION IF EXISTS modall_reject_immutable_update()")
    op.drop_table("capability_status_events")
    op.drop_table("mcp_tool_bindings")
    op.drop_constraint("fk_capability_enabled_version", "capabilities", type_="foreignkey")
    op.drop_constraint("fk_capability_pending_version", "capabilities", type_="foreignkey")
    op.drop_table("capability_versions")
    op.drop_table("capabilities")
    op.drop_constraint("fk_connection_verified_version", "server_connections", type_="foreignkey")
    op.drop_constraint("fk_connection_pending_version", "server_connections", type_="foreignkey")
    op.drop_constraint(
        "fk_connection_allocated_target_version", "server_connections", type_="foreignkey"
    )
    op.drop_table("server_connection_versions")
    op.drop_table("server_connections")
    op.drop_constraint("fk_registry_entry_current_version", "registry_entries", type_="foreignkey")
    op.drop_table("registry_entry_versions")
    op.drop_table("registry_entries")
    op.drop_constraint("ck_audit_resource_type", "audit_events", type_="check")
    op.drop_constraint("ck_audit_action", "audit_events", type_="check")
    op.execute(
        "DELETE FROM audit_events WHERE action IN ("
        "'connection.created', 'connection.version_appended', 'connection.verified', "
        "'connection.disabled', 'connection.enabled', 'capability.version_recorded', "
        "'capability.enabled', 'capability.disabled')"
    )
    op.create_check_constraint(
        "ck_audit_action",
        "audit_events",
        "action IN ('workspace.created', 'membership.changed', 'secret_binding.created')",
    )
    op.create_check_constraint(
        "ck_audit_resource_type",
        "audit_events",
        "resource_type IN ('workspace', 'membership', 'secret_binding')",
    )
    op.drop_constraint("uq_secret_binding_workspace_id", "secret_bindings", type_="unique")
