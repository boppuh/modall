"""Add bounded accepted invocation results."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260906_0006"
down_revision: str | None = "20260906_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    expanded_failure_codes = (
        "safe_error_code IS NULL OR safe_error_code IN "
        "('worker_lost_before_dispatch', 'worker_lost_after_dispatch', "
        "'deadline_exceeded', 'cancelled_before_dispatch', 'restore_reconciliation', "
        "'content_retention_deadline', 'preparation_failed', "
        "'session_initialization_failed', 'tool_call_failed', 'invalid_tool_result', "
        "'unsupported_tool_result', 'sensitive_tool_result', "
        "'upstream_outcome_unknown')"
    )
    for table, constraint in (
        ("runs", "ck_run_safe_error_code"),
        ("run_attempts", "ck_run_attempt_safe_error_code"),
        ("run_events", "ck_run_event_safe_error_code"),
    ):
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(constraint, table, expanded_failure_codes)
    op.create_table(
        "run_results",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("canonical_digest", sa.String(64), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("byte_count > 0", name="ck_run_result_byte_count"),
        sa.CheckConstraint("length(canonical_digest) = 64", name="ck_run_result_digest"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["runs.workspace_id", "runs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("ix_run_results_expiry", "run_results", ["expires_at", "run_id"])
    op.execute(
        "CREATE TRIGGER run_results_immutable BEFORE UPDATE ON run_results "
        "FOR EACH ROW EXECUTE FUNCTION modall_reject_immutable_update()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS run_results_immutable ON run_results")
    op.drop_index("ix_run_results_expiry", table_name="run_results")
    op.drop_table("run_results")
    original_failure_codes = (
        "safe_error_code IS NULL OR safe_error_code IN "
        "('worker_lost_before_dispatch', 'worker_lost_after_dispatch', "
        "'deadline_exceeded', 'cancelled_before_dispatch', 'restore_reconciliation', "
        "'content_retention_deadline', 'preparation_failed', "
        "'session_initialization_failed', 'tool_call_failed', 'invalid_tool_result', "
        "'upstream_outcome_unknown')"
    )
    for table, constraint in (
        ("runs", "ck_run_safe_error_code"),
        ("run_attempts", "ck_run_attempt_safe_error_code"),
        ("run_events", "ck_run_event_safe_error_code"),
    ):
        op.drop_constraint(constraint, table, type_="check")
    op.execute("DROP TRIGGER IF EXISTS run_events_immutable ON run_events")
    for table in ("runs", "run_attempts", "run_events"):
        op.execute(
            f"UPDATE {table} SET safe_error_code = 'invalid_tool_result' "
            "WHERE safe_error_code IN ('unsupported_tool_result', 'sensitive_tool_result')"
        )
    op.execute(
        "CREATE TRIGGER run_events_immutable BEFORE UPDATE ON run_events "
        "FOR EACH ROW EXECUTE FUNCTION modall_reject_immutable_update()"
    )
    for table, constraint in (
        ("runs", "ck_run_safe_error_code"),
        ("run_attempts", "ck_run_attempt_safe_error_code"),
        ("run_events", "ck_run_event_safe_error_code"),
    ):
        op.create_check_constraint(constraint, table, original_failure_codes)
