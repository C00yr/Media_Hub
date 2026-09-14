"""Add persistent idempotent Agent download operations.

Revision ID: 20260729_0005
Revises: 20260722_0004
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite


revision = "20260729_0005"
down_revision = "20260722_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("agent_download_operations"):
        return
    op.create_table(
        "agent_download_operations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("session_key", sa.String(length=96), nullable=False),
        sa.Column("torrent_id", sa.String(length=96), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("downloader_id", sa.String(length=16), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("candidate", sqlite.JSON(), nullable=False),
        sa.Column("constraints", sqlite.JSON(), nullable=False),
        sa.Column("result", sqlite.JSON(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_agent_download_operations_operation_id", "agent_download_operations", ["operation_id"], unique=True)
    op.create_index("ix_agent_download_operations_session_key", "agent_download_operations", ["session_key"])
    op.create_index("ix_agent_download_operations_torrent_id", "agent_download_operations", ["torrent_id"])
    op.create_index("ix_agent_download_operations_state", "agent_download_operations", ["state"])
    op.create_index("ix_agent_download_operations_expires_at", "agent_download_operations", ["expires_at"])


def downgrade() -> None:
    raise RuntimeError("Production schema downgrades are disabled; restore the matching database backup instead.")
