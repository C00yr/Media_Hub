"""add per-user media favorites

Revision ID: 20260714_0002
Revises: 20260629_0001
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite


revision = "20260714_0002"
down_revision = "20260629_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("media_favorites"):
        return
    op.create_table(
        "media_favorites",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("media_type", sa.String(length=16), nullable=False),
        sa.Column("tmdb_id", sa.Integer(), nullable=False),
        sa.Column("media_payload", sqlite.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "media_type", "tmdb_id", name="uq_media_favorite_user_title"),
    )
    op.create_index("ix_media_favorites_user_id", "media_favorites", ["user_id"])
    op.create_index("ix_media_favorites_media_type", "media_favorites", ["media_type"])
    op.create_index("ix_media_favorites_tmdb_id", "media_favorites", ["tmdb_id"])
    op.create_index("ix_media_favorites_created_at", "media_favorites", ["created_at"])


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("media_favorites"):
        return
    op.drop_index("ix_media_favorites_created_at", table_name="media_favorites")
    op.drop_index("ix_media_favorites_tmdb_id", table_name="media_favorites")
    op.drop_index("ix_media_favorites_media_type", table_name="media_favorites")
    op.drop_index("ix_media_favorites_user_id", table_name="media_favorites")
    op.drop_table("media_favorites")
