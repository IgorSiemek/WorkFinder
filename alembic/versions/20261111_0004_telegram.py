"""Durable Telegram cursor and outgoing messages."""

import sqlalchemy as sa

from alembic import op

revision = "20261111_0004"
down_revision = "20261016_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bot_state",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
    )
    op.create_table(
        "bot_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dedup_key", sa.String(200), nullable=False, unique=True),
        sa.Column("method", sa.String(60), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime()),
        sa.Column("error", sa.Text()),
    )
    op.create_index("ix_bot_messages_status", "bot_messages", ["status"])


def downgrade() -> None:
    op.drop_table("bot_messages")
    op.drop_table("bot_state")
