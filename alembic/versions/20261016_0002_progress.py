"""Persist search observations and retryable Excel synchronization."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261016_0002"
down_revision = "20260930_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Also supports users who ran the earlier zero-configuration create_all startup.
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("excel_sync_tasks"):
        op.create_table(
            "excel_sync_tasks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "application_id",
                sa.Integer(),
                sa.ForeignKey("applications.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
                index=True,
            ),
            sa.Column("workbook_path", sa.Text(), nullable=False),
            sa.Column("applied_on", sa.Date(), nullable=False),
            sa.Column("status", sa.String(30), nullable=False, index=True),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("error_message", sa.Text()),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
    if not inspector.has_table("search_observations"):
        op.create_table(
            "search_observations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "search_run_id",
                sa.Integer(),
                sa.ForeignKey("search_runs.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="SET NULL"), index=True
            ),
            sa.Column("provider", sa.String(100), nullable=False),
            sa.Column("title", sa.String(300), nullable=False),
            sa.Column("company", sa.String(200), nullable=False),
            sa.Column("outcome", sa.String(40), nullable=False, index=True),
            sa.Column("detail", sa.Text(), nullable=False),
            sa.Column("observed_at", sa.DateTime(), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("search_observations")
    op.drop_table("excel_sync_tasks")
