"""Add session expiry and runtime schema introduced after the baseline.

Revision ID: 20260722_0004
Revises: 20260717_0003
Create Date: 2026-07-22
"""

from alembic import op
import sqlalchemy as sa


revision = "20260722_0004"
down_revision = "20260717_0003"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    mteam_columns = _column_names("mteam_snapshots")
    if "user_level" not in mteam_columns:
        op.add_column(
            "mteam_snapshots",
            sa.Column("user_level", sa.String(length=64), nullable=False, server_default=""),
        )
    if "seed_size" not in mteam_columns:
        op.add_column(
            "mteam_snapshots",
            sa.Column("seed_size", sa.Float(), nullable=False, server_default="0"),
        )

    binding_columns = _column_names("wechat_claw_bindings")
    if "avatar_key" not in binding_columns:
        op.add_column(
            "wechat_claw_bindings",
            sa.Column("avatar_key", sa.String(length=32), nullable=False, server_default="mint"),
        )

    session_columns = _column_names("user_sessions")
    if "expires_at" not in session_columns:
        op.add_column("user_sessions", sa.Column("expires_at", sa.DateTime(), nullable=True))

    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("qb_delete_confirmations"):
        op.create_table(
            "qb_delete_confirmations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("downloader_id", sa.String(length=16), nullable=False),
            sa.Column("torrent_hash", sa.String(length=128), nullable=False),
            sa.Column("delete_files", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("used_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_qb_delete_confirmations_token_hash", "qb_delete_confirmations", ["token_hash"], unique=True)
        op.create_index("ix_qb_delete_confirmations_user_id", "qb_delete_confirmations", ["user_id"])
        op.create_index("ix_qb_delete_confirmations_downloader_id", "qb_delete_confirmations", ["downloader_id"])
        op.create_index("ix_qb_delete_confirmations_expires_at", "qb_delete_confirmations", ["expires_at"])


def downgrade() -> None:
    raise RuntimeError("Production schema downgrades are disabled; restore the matching database backup instead.")
