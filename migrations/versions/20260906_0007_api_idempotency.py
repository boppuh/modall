"""Add replay records for non-run API mutations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260906_0007"
down_revision: str | None = "20260906_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_idempotency_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("route", sa.String(128), nullable=False),
        sa.Column("key_version", sa.String(32), nullable=False),
        sa.Column("key_hmac", sa.String(64), nullable=False),
        sa.Column("request_hmac", sa.String(64), nullable=False),
        sa.Column("response_body", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(key_hmac) = 64", name="ck_api_idempotency_key_hmac"),
        sa.CheckConstraint("length(request_hmac) = 64", name="ck_api_idempotency_request_hmac"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
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
    op.create_index("ix_api_idempotency_expiry", "api_idempotency_records", ["expires_at", "id"])
    op.create_index("ix_api_idempotency_key_version", "api_idempotency_records", ["key_version"])


def downgrade() -> None:
    op.drop_index("ix_api_idempotency_key_version", table_name="api_idempotency_records")
    op.drop_index("ix_api_idempotency_expiry", table_name="api_idempotency_records")
    op.drop_table("api_idempotency_records")
