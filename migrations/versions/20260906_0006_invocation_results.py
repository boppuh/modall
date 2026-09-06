"""Add bounded accepted invocation results."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260906_0006"
down_revision: str | None = "20260906_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
