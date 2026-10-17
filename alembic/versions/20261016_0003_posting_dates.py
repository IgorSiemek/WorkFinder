"""Keep spreadsheet-added dates separate from employer posting dates."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261016_0003"
down_revision = "20261016_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    jobs = sa.table(
        "jobs",
        sa.column("source", sa.String),
        sa.column("notes", sa.Text),
        sa.column("date_posted", sa.Date),
    )
    op.execute(
        jobs.update()
        .where(
            jobs.c.source == "Excel",
            jobs.c.date_posted.is_not(None),
        )
        .values(
            notes=sa.func.coalesce(jobs.c.notes, "")
            + "\nSpreadsheet-added date (not employer posting date): "
            + sa.cast(jobs.c.date_posted, sa.String),
            date_posted=None,
        )
    )


def downgrade() -> None:
    # The original spreadsheet-added date remains in notes and the raw import payload.
    pass
