"""Initial WorkFinder schema."""

from __future__ import annotations

from alembic import op
from app import models  # noqa: F401
from app.database import Base

revision = "20260930_0001"
down_revision = None
branch_labels = None
depends_on = None

INITIAL_TABLES = (
    "jobs",
    "job_occurrences",
    "search_runs",
    "provider_runs",
    "applications",
    "application_activities",
    "candidate_profiles",
    "cv_versions",
    "company_preferences",
    "saved_searches",
    "ignore_rules",
)


def upgrade() -> None:
    Base.metadata.create_all(
        bind=op.get_bind(), tables=[Base.metadata.tables[name] for name in INITIAL_TABLES]
    )


def downgrade() -> None:
    Base.metadata.drop_all(
        bind=op.get_bind(), tables=[Base.metadata.tables[name] for name in INITIAL_TABLES]
    )
