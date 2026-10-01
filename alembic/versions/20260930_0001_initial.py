"""Initial WorkFinder schema."""

from __future__ import annotations

from alembic import op
from app import models  # noqa: F401
from app.database import Base

revision = "20260930_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
