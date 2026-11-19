"""The lego tables: lego_ids (who owns each id, its kind) and legos (one row per version, never updated).

Revision ID: 0001
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lego_ids",
        sa.Column("id", sa.String(63), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("data_kind", sa.String(16)),
        sa.Column("owner", sa.String(255), nullable=False),
        sa.Column("owner_group", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "legos",
        sa.Column("id", sa.String(63), sa.ForeignKey("lego_ids.id"), primary_key=True),
        sa.Column("version", sa.String(32), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("owner", sa.String(255), nullable=False),
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("body", sa.JSON, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("legos")
    op.drop_table("lego_ids")
