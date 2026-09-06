"""Add durable execution ledger, confirmation nonces, and idempotency."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260906_0005"
down_revision: str | None = "20260906_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


RUN_STATES = (
    "'queued', 'preparing', 'session_fenced', 'dispatch_fenced', 'succeeded', "
    "'failed', 'cancelled', 'timed_out', 'indeterminate'"
)
TERMINAL_JOB_STATES = "'succeeded', 'failed', 'cancelled', 'timed_out', 'indeterminate'"


def upgrade() -> None:
    op.drop_constraint("ck_audit_action", "audit_events", type_="check")
    op.drop_constraint("ck_audit_resource_type", "audit_events", type_="check")
    op.create_check_constraint(
        "ck_audit_action",
        "audit_events",
        "action IN ('workspace.created', 'membership.changed', 'secret_binding.created', "
        "'connection.created', 'connection.version_appended', 'connection.verified', "
        "'connection.disabled', 'connection.enabled', 'capability.version_recorded', "
        "'capability.enabled', 'capability.disabled', 'registry_entry.imported', "
        "'run.created', 'run.cancelled')",
    )
    op.create_check_constraint(
        "ck_audit_resource_type",
        "audit_events",
        "resource_type IN ('workspace', 'membership', 'secret_binding', 'server_connection', "
        "'capability', 'registry_entry', 'run')",
    )

    op.create_table(
        "system_execution_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("execution_epoch", sa.Integer(), nullable=False),
        sa.Column("dispatch_quarantined", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_system_execution_state_singleton"),
        sa.CheckConstraint("execution_epoch > 0", name="ck_system_execution_state_epoch"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "INSERT INTO system_execution_state "
        "(id, execution_epoch, dispatch_quarantined, updated_at) "
        "VALUES (1, 1, false, CURRENT_TIMESTAMP)"
    )

    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("capability_id", sa.Uuid(), nullable=False),
        sa.Column("capability_version_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("connection_version_id", sa.Uuid(), nullable=False),
        sa.Column("connection_control_epoch", sa.Integer(), nullable=False),
        sa.Column("capability_status_epoch", sa.Integer(), nullable=False),
        sa.Column("protocol_revision", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("arguments", sa.JSON()),
        sa.Column("argument_digest", sa.String(64), nullable=False),
        sa.Column("arguments_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancellation_requested", sa.Boolean(), nullable=False),
        sa.Column("safe_error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(f"status IN ({RUN_STATES})", name="ck_run_status"),
        sa.CheckConstraint(
            "(status IN ('succeeded', 'failed', 'cancelled', 'timed_out', 'indeterminate')) "
            "= (terminal_at IS NOT NULL)",
            name="ck_run_terminal_time",
        ),
        sa.CheckConstraint("connection_control_epoch >= 0", name="ck_run_connection_epoch"),
        sa.CheckConstraint("capability_status_epoch >= 0", name="ck_run_capability_epoch"),
        sa.CheckConstraint("length(argument_digest) = 64", name="ck_run_argument_digest"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "capability_id"],
            ["capabilities.workspace_id", "capabilities.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "capability_version_id"],
            ["capability_versions.workspace_id", "capability_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "connection_id"],
            ["server_connections.workspace_id", "server_connections.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "connection_version_id"],
            ["server_connection_versions.workspace_id", "server_connection_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id"),
    )
    op.create_index("ix_runs_workspace_created", "runs", ["workspace_id", "created_at", "id"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("execution_epoch", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("lease_owner", sa.String(128)),
        sa.Column("lease_epoch", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("execution_epoch > 0", name="ck_job_execution_epoch"),
        sa.CheckConstraint("lease_epoch >= 0", name="ck_job_lease_epoch"),
        sa.CheckConstraint(
            f"status IN ('queued', 'leased', {TERMINAL_JOB_STATES})", name="ck_job_status"
        ),
        sa.CheckConstraint(
            "(status = 'leased' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status <> 'leased' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="ck_job_active_lease",
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded', 'failed', 'cancelled', 'timed_out', 'indeterminate')) "
            "= (completed_at IS NOT NULL)",
            name="ck_job_completion_time",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"], ["runs.workspace_id", "runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id"),
        sa.UniqueConstraint("run_id"),
    )
    op.create_index("ix_jobs_claim", "jobs", ["status", "available_at", "created_at"])

    op.create_table(
        "run_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("lease_epoch", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_at", sa.DateTime(timezone=True)),
        sa.Column("safe_error_code", sa.String(64)),
        sa.CheckConstraint("sequence > 0", name="ck_run_attempt_sequence"),
        sa.CheckConstraint("lease_epoch > 0", name="ck_run_attempt_lease_epoch"),
        sa.CheckConstraint(
            "status IN ('preparing', 'session_fenced', 'dispatch_fenced', 'succeeded', "
            "'failed', 'cancelled', 'timed_out', 'indeterminate')",
            name="ck_run_attempt_status",
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded', 'failed', 'cancelled', 'timed_out', 'indeterminate')) "
            "= (terminal_at IS NOT NULL)",
            name="ck_run_attempt_terminal_time",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"], ["runs.workspace_id", "runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "job_id"], ["jobs.workspace_id", "jobs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id"),
        sa.UniqueConstraint("run_id", "sequence"),
    )
    op.create_index(
        "uq_run_attempts_active",
        "run_attempts",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('preparing', 'session_fenced', 'dispatch_fenced')"),
        sqlite_where=sa.text("status IN ('preparing', 'session_fenced', 'dispatch_fenced')"),
    )

    op.create_table(
        "run_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("safe_error_code", sa.String(64)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence > 0", name="ck_run_event_sequence"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"], ["runs.workspace_id", "runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id"),
        sa.UniqueConstraint("run_id", "sequence"),
    )
    op.create_index("ix_run_events_run_sequence", "run_events", ["run_id", "sequence"])

    op.create_table(
        "confirmation_nonces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("nonce_digest", sa.String(64), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(nonce_digest) = 64", name="ck_confirmation_nonce_digest"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"], ["runs.workspace_id", "runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("nonce_digest"),
    )

    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("route", sa.String(128), nullable=False),
        sa.Column("key_version", sa.String(32), nullable=False),
        sa.Column("key_hmac", sa.String(64), nullable=False),
        sa.Column("request_hmac", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(key_hmac) = 64", name="ck_idempotency_key_hmac"),
        sa.CheckConstraint("length(request_hmac) = 64", name="ck_idempotency_request_hmac"),
        sa.CheckConstraint("resource_type = 'run'", name="ck_idempotency_resource_type"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "resource_id"], ["runs.workspace_id", "runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "actor_user_id",
            "method",
            "route",
            "key_version",
            "key_hmac",
        ),
    )
    op.create_index("ix_idempotency_expiry", "idempotency_records", ["expires_at"])

    for table in ("run_events", "confirmation_nonces", "idempotency_records"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION modall_reject_immutable_update()"
        )
    op.execute(
        "CREATE FUNCTION modall_reject_terminal_execution_update() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        f"IF OLD.status IN ({TERMINAL_JOB_STATES}) AND NEW.status IS DISTINCT FROM OLD.status "
        "THEN RAISE EXCEPTION 'terminal execution status cannot be updated'; END IF; "
        "RETURN NEW; END; $$"
    )
    for table in ("runs", "jobs", "run_attempts"):
        op.execute(
            f"CREATE TRIGGER {table}_terminal_status_immutable BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION modall_reject_terminal_execution_update()"
        )


def downgrade() -> None:
    for table in ("run_attempts", "jobs", "runs"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_terminal_status_immutable ON {table}")
    op.execute("DROP FUNCTION IF EXISTS modall_reject_terminal_execution_update()")
    for table in ("idempotency_records", "confirmation_nonces", "run_events"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.drop_index("ix_idempotency_expiry", table_name="idempotency_records")
    op.drop_table("idempotency_records")
    op.drop_table("confirmation_nonces")
    op.drop_index("ix_run_events_run_sequence", table_name="run_events")
    op.drop_table("run_events")
    op.drop_index("uq_run_attempts_active", table_name="run_attempts")
    op.drop_table("run_attempts")
    op.drop_index("ix_jobs_claim", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_runs_workspace_created", table_name="runs")
    op.drop_table("runs")
    op.drop_table("system_execution_state")

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
