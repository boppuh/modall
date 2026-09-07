"""Persist API-to-worker correlation on the durable run ledger."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260907_0009"
down_revision: str | None = "20260907_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("correlation_id", sa.Uuid(), nullable=True))
    op.execute(
        """
        WITH created_run_events AS (
            SELECT DISTINCT ON (event.workspace_id, event.resource_id)
                event.workspace_id,
                event.resource_id,
                event.correlation_id
            FROM audit_events AS event
            WHERE event.resource_type = 'run'
              AND event.action = 'run.created'
              AND event.outcome = 'succeeded'
            ORDER BY event.workspace_id, event.resource_id, event.occurred_at, event.id
        )
        UPDATE runs AS r
        SET correlation_id = event.correlation_id
        FROM created_run_events AS event
        WHERE event.workspace_id = r.workspace_id
          AND event.resource_id = r.id
        """
    )
    op.execute("UPDATE runs SET correlation_id = id WHERE correlation_id IS NULL")
    op.alter_column("runs", "correlation_id", nullable=False)
    op.create_index("ix_runs_workspace_correlation", "runs", ["workspace_id", "correlation_id"])


def downgrade() -> None:
    op.drop_index("ix_runs_workspace_correlation", table_name="runs")
    op.drop_column("runs", "correlation_id")
