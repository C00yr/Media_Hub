"""Create the production baseline schema.

Revision ID: 20260629_0001
Revises:
Create Date: 2026-06-29
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

revision = "20260629_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(80), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_id", sa.String(64), nullable=False),
        sa.Column("qb2_grant_expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_user_sessions_token_id", "user_sessions", ["token_id"], unique=True)
    op.create_table(
        "wechat_claw_bindings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("role_name", sa.String(120), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("notification_preferences", sqlite.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "integration_configs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("encrypted_payload", sa.Text(), nullable=False),
        sa.Column("redacted_summary", sqlite.JSON(), nullable=False),
        sa.Column("last_tested_at", sa.DateTime(), nullable=True),
        sa.Column("last_test_result", sqlite.JSON(), nullable=True),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_integration_configs_provider", "integration_configs", ["provider"], unique=True)
    op.create_table(
        "config_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("test_success", sa.Boolean(), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_config_audit_logs_provider", "config_audit_logs", ["provider"])
    op.create_index("ix_config_audit_logs_trace_id", "config_audit_logs", ["trace_id"])
    op.create_table(
        "debug_traces",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("timeline", sqlite.JSON(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_debug_traces_trace_id", "debug_traces", ["trace_id"])
    op.create_index("ix_debug_traces_event_type", "debug_traces", ["event_type"])
    op.create_table(
        "download_actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("downloader_id", sa.String(16), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_hash", sa.String(128), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_download_actions_trace_id", "download_actions", ["trace_id"])
    op.create_table(
        "mteam_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("upload_total", sa.Float(), nullable=False),
        sa.Column("download_total", sa.Float(), nullable=False),
        sa.Column("bonus", sa.Float(), nullable=False),
        sa.Column("ratio", sa.Float(), nullable=False),
        sa.Column("active_uploads", sa.Integer(), nullable=False),
        sa.Column("active_downloads", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("completeness", sa.String(64), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "qb_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("downloader_id", sa.String(16), nullable=False),
        sa.Column("download_speed", sa.Float(), nullable=False),
        sa.Column("upload_speed", sa.Float(), nullable=False),
        sa.Column("downloaded_total", sa.Float(), nullable=False),
        sa.Column("uploaded_total", sa.Float(), nullable=False),
        sa.Column("active_downloads", sa.Integer(), nullable=False),
        sa.Column("active_uploads", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("completeness", sa.String(64), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_qb_snapshots_downloader_id", "qb_snapshots", ["downloader_id"])
    op.create_table(
        "qb_torrent_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("qb_snapshot_id", sa.Integer(), sa.ForeignKey("qb_snapshots.id"), nullable=True),
        sa.Column("downloader_id", sa.String(16), nullable=False),
        sa.Column("torrent_hash", sa.String(128), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("progress", sa.Float(), nullable=False),
        sa.Column("state", sa.String(64), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_qb_torrent_snapshots_downloader_id", "qb_torrent_snapshots", ["downloader_id"])
    op.create_index("ix_qb_torrent_snapshots_torrent_hash", "qb_torrent_snapshots", ["torrent_hash"])
    op.create_table(
        "nas_disk_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("path_label", sa.String(120), nullable=False),
        sa.Column("free_bytes", sa.Float(), nullable=False),
        sa.Column("total_bytes", sa.Float(), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("completeness", sa.String(64), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "stat_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("scope", sqlite.JSON(), nullable=False),
        sa.Column("formula", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("name", name="uq_stat_rules_name"),
    )
    op.create_table(
        "stat_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("rule_name", sa.String(120), nullable=False),
        sa.Column("range_label", sa.String(64), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("formula", sa.Text(), nullable=False),
        sa.Column("start_snapshot", sqlite.JSON(), nullable=False),
        sa.Column("current_snapshot", sqlite.JSON(), nullable=False),
        sa.Column("result", sqlite.JSON(), nullable=False),
        sa.Column("completeness", sa.String(64), nullable=False),
        sa.Column("calculated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_stat_results_rule_name", "stat_results", ["rule_name"])
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("level", sa.String(32), nullable=False),
        sa.Column("read", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(120), nullable=False),
        sa.Column("value", sqlite.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("key", name="uq_settings_key"),
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_table("notifications")
    op.drop_index("ix_stat_results_rule_name", table_name="stat_results")
    op.drop_table("stat_results")
    op.drop_table("stat_rules")
    op.drop_table("nas_disk_snapshots")
    op.drop_index("ix_qb_torrent_snapshots_torrent_hash", table_name="qb_torrent_snapshots")
    op.drop_index("ix_qb_torrent_snapshots_downloader_id", table_name="qb_torrent_snapshots")
    op.drop_table("qb_torrent_snapshots")
    op.drop_index("ix_qb_snapshots_downloader_id", table_name="qb_snapshots")
    op.drop_table("qb_snapshots")
    op.drop_table("mteam_snapshots")
    op.drop_index("ix_download_actions_trace_id", table_name="download_actions")
    op.drop_table("download_actions")
    op.drop_index("ix_debug_traces_event_type", table_name="debug_traces")
    op.drop_index("ix_debug_traces_trace_id", table_name="debug_traces")
    op.drop_table("debug_traces")
    op.drop_index("ix_config_audit_logs_trace_id", table_name="config_audit_logs")
    op.drop_index("ix_config_audit_logs_provider", table_name="config_audit_logs")
    op.drop_table("config_audit_logs")
    op.drop_index("ix_integration_configs_provider", table_name="integration_configs")
    op.drop_table("integration_configs")
    op.drop_table("wechat_claw_bindings")
    op.drop_index("ix_user_sessions_token_id", table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
