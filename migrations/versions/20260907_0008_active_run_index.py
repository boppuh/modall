"""Add a workspace-leading index for active run reconciliation."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260907_0008"
down_revision: str | None = "20260906_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE_PREDICATE = "status IN ('queued', 'preparing', 'session_fenced', 'dispatch_fenced')"


def upgrade() -> None:
    op.create_index(
        "ix_runs_workspace_active",
        "runs",
        ["workspace_id", "created_at", "id"],
        postgresql_where=sa.text(_ACTIVE_PREDICATE),
        sqlite_where=sa.text(_ACTIVE_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index("ix_runs_workspace_active", table_name="runs")
