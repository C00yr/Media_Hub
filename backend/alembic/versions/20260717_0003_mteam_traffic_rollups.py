"""add compacted M-Team traffic rollups

Revision ID: 20260717_0003
Revises: 20260714_0002
Create Date: 2026-07-17
"""

from alembic import op
import sqlalchemy as sa


revision = "20260717_0003"
down_revision = "20260714_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("mteam_traffic_rollups"):
        return
    op.create_table(
        "mteam_traffic_rollups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("period_type", sa.String(length=16), nullable=False),
        sa.Column("period_start", sa.DateTime(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("upload_total", sa.Float(), nullable=False),
        sa.Column("download_total", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("period_type", "period_start", "timezone", name="uq_mteam_traffic_rollup_period"),
    )
    op.create_index("ix_mteam_traffic_rollups_period_type", "mteam_traffic_rollups", ["period_type"])
    op.create_index("ix_mteam_traffic_rollups_period_start", "mteam_traffic_rollups", ["period_start"])


    if not sa.inspect(op.get_bind()).has_table("mteam_traffic_rollups"):
        return
def downgrade() -> None:
    op.drop_index("ix_mteam_traffic_rollups_period_start", table_name="mteam_traffic_rollups")
    op.drop_index("ix_mteam_traffic_rollups_period_type", table_name="mteam_traffic_rollups")
    op.drop_table("mteam_traffic_rollups")
