"""initial schema

Revision ID: 20260629_0001
Revises:
Create Date: 2026-06-29
"""

from alembic import op
import sqlalchemy as sa


revision = "20260629_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("users", sa.Column("id", sa.Integer(), primary_key=True))
    # The app creates the full MVP schema with SQLAlchemy on startup.
    # This migration placeholder establishes Alembic wiring for future explicit revisions.


def downgrade() -> None:
    op.drop_table("users")

