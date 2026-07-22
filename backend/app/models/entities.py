from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base
from app.utils.time import utc_now_naive


def utcnow() -> datetime:
    return utc_now_naive()


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    token_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    qb2_grant_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    user: Mapped[User] = relationship()


class QbDeleteConfirmation(Base):
    __tablename__ = "qb_delete_confirmations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    downloader_id: Mapped[str] = mapped_column(String(16), index=True)
    torrent_hash: Mapped[str] = mapped_column(String(128))
    delete_files: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WechatClawBinding(Base):
    __tablename__ = "wechat_claw_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120), default="微信成员")
    role_name: Mapped[str] = mapped_column(String(120), default="微信助手")
    avatar_key: Mapped[str] = mapped_column(String(32), default="mint")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notification_preferences: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class IntegrationConfig(Base):
    __tablename__ = "integration_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    config_version: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    encrypted_payload: Mapped[str] = mapped_column(Text, default="")
    redacted_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_test_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ConfigAuditLog(Base):
    __tablename__ = "config_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    config_version: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(64))
    test_success: Mapped[bool | None] = mapped_column(Boolean)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DebugTrace(Base):
    __tablename__ = "debug_traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(32), default="success")
    timeline: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    config_version: Mapped[int | None] = mapped_column(Integer)
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DownloadAction(Base):
    __tablename__ = "download_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    downloader_id: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(64))
    target_hash: Mapped[str | None] = mapped_column(String(128))
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(32), default="accepted")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MTeamSnapshot(Base):
    __tablename__ = "mteam_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_level: Mapped[str] = mapped_column(String(64), default="")
    upload_total: Mapped[float] = mapped_column(Float, default=0)
    download_total: Mapped[float] = mapped_column(Float, default=0)
    bonus: Mapped[float] = mapped_column(Float, default=0)
    ratio: Mapped[float] = mapped_column(Float, default=0)
    seed_size: Mapped[float] = mapped_column(Float, default=0)
    active_uploads: Mapped[int] = mapped_column(Integer, default=0)
    active_downloads: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(64), default="collected")
    completeness: Mapped[str] = mapped_column(String(64), default="complete")
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MTeamTrafficRollup(Base):
    __tablename__ = "mteam_traffic_rollups"
    __table_args__ = (
        UniqueConstraint("period_type", "period_start", "timezone", name="uq_mteam_traffic_rollup_period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period_type: Mapped[str] = mapped_column(String(16), index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    upload_total: Mapped[float] = mapped_column(Float, default=0)
    download_total: Mapped[float] = mapped_column(Float, default=0)
    source: Mapped[str] = mapped_column(String(64), default="app_rollup")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class QbSnapshot(Base):
    __tablename__ = "qb_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    downloader_id: Mapped[str] = mapped_column(String(16), index=True)
    download_speed: Mapped[float] = mapped_column(Float, default=0)
    upload_speed: Mapped[float] = mapped_column(Float, default=0)
    downloaded_total: Mapped[float] = mapped_column(Float, default=0)
    uploaded_total: Mapped[float] = mapped_column(Float, default=0)
    active_downloads: Mapped[int] = mapped_column(Integer, default=0)
    active_uploads: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(64), default="collected")
    completeness: Mapped[str] = mapped_column(String(64), default="complete")
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class QbTorrentSnapshot(Base):
    __tablename__ = "qb_torrent_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    qb_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("qb_snapshots.id"))
    downloader_id: Mapped[str] = mapped_column(String(16), index=True)
    torrent_hash: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(255))
    progress: Mapped[float] = mapped_column(Float, default=0)
    state: Mapped[str] = mapped_column(String(64), default="downloading")
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class NasDiskSnapshot(Base):
    __tablename__ = "nas_disk_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path_label: Mapped[str] = mapped_column(String(120), default="media")
    free_bytes: Mapped[float] = mapped_column(Float, default=0)
    total_bytes: Mapped[float] = mapped_column(Float, default=0)
    source: Mapped[str] = mapped_column(String(64), default="collected")
    completeness: Mapped[str] = mapped_column(String(64), default="complete")
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class StatRule(Base):
    __tablename__ = "stat_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    formula: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class StatResult(Base):
    __tablename__ = "stat_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_name: Mapped[str] = mapped_column(String(120), index=True)
    range_label: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(80))
    formula: Mapped[str] = mapped_column(Text)
    start_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    current_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    completeness: Mapped[str] = mapped_column(String(64), default="complete")
    calculated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    level: Mapped[str] = mapped_column(String(32), default="info")
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(64), default="app")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MediaFavorite(Base):
    __tablename__ = "media_favorites"
    __table_args__ = (UniqueConstraint("user_id", "media_type", "tmdb_id", name="uq_media_favorite_user_title"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    media_type: Mapped[str] = mapped_column(String(16), index=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, index=True)
    media_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Setting(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(120), unique=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
