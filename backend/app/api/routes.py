import os
import hashlib
import json
import logging
import mimetypes
import re
import secrets
import shutil
import time
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path
from socket import timeout as SocketTimeout
from threading import Lock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.adapters.ai import AIConfigError, AIServiceError, DeepSeekChatAdapter
from app.adapters.mock import MockMetadataAdapter
from app.adapters.mteam import MTeamAdapter, MTeamApiError, MTeamConfigError
from app.adapters.qbittorrent import QbittorrentApiError, QbittorrentConfigError, QbittorrentWebAdapter
from app.adapters.tmdb import TmdbAdapter
from app.adapters.tmdb.client import DEFAULT_TMDB_PROXY_URL, TmdbConfigError, TmdbDohError, TmdbImageError, open_tmdb_network_request
from app.adapters.wechat_claw import WechatClawAdapter, WechatClawApiError, WechatClawConfigError
from app.api.deps import get_current_user, has_qb2_grant, require_admin
from app.auth.security import create_access_token, decode_token, hash_password, verify_password
from app.config.settings import get_settings
from app.db.session import SessionLocal, get_db
from app.diagnostics.tracing import TraceRecorder
from app.models.entities import (
    ConfigAuditLog,
    DebugTrace,
    DiagnosticExport,
    DownloadAction,
    IntegrationConfig,
    MTeamSnapshot,
    Notification,
    QbSnapshot,
    Setting,
    User,
    UserSession,
    WechatClawBinding,
)
from app.services.integrations import (
    PROVIDERS,
    get_config,
    get_decrypted_config,
    record_test_result,
    serialize_config,
    set_enabled,
    upsert_config,
)
from app.utils.ids import trace_id
from app.utils.redaction import redact_payload


router = APIRouter()
settings = get_settings()
logger = logging.getLogger(__name__)
_PRELOAD_TASK_LOCK = Lock()
_PRELOAD_TASKS_RUNNING: set[str] = set()
_WECHAT_CLAW_POLL_LOCKS: dict[int, Lock] = {}
_WECHAT_CLAW_POLL_LOCK_GUARD = Lock()
_WECHAT_CLAW_ACTIVE_USER_ID: ContextVar[int | None] = ContextVar("wechat_claw_active_user_id", default=None)
PRELOAD_PREFIX = "preload."
TMDB_IMAGE_SIZES = {"w92", "w154", "w185", "w342", "w500", "w780", "w1280", "original"}
TMDB_IMAGE_PATH_RE = re.compile(r"^[A-Za-z0-9_./-]+\.(jpg|jpeg|png|webp)$", re.IGNORECASE)
TMDB_DIRECT_IMAGE_RE = re.compile(
    r"https://image\.tmdb\.org/t/p/(?P<size>w\d+|original)/(?P<path>[A-Za-z0-9_./-]+\.(?:jpg|jpeg|png|webp))",
    re.IGNORECASE,
)
TMDB_IMAGE_MAX_BYTES = 20 * 1024 * 1024


class SetupAdminRequest(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=8, max_length=200)


class LoginRequest(BaseModel):
    username: str
    password: str


class AdminVerifyRequest(BaseModel):
    username: str
    password: str


class IntegrationPayload(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


class QbActionPayload(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)
    confirm_token: str | None = None


class AssistantChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class AssistantExecuteRequest(BaseModel):
    intent: dict[str, Any] = Field(default_factory=dict)
    message: str = ""


class NotificationPreferencesPayload(BaseModel):
    preferences: dict[str, bool] = Field(default_factory=dict)


class WechatClawMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    user_id: str = ""
    conversation_id: str = ""
    context_token: str = ""
    admin_password: str = Field(default="", max_length=200)


class WechatClawBindingPayload(BaseModel):
    display_name: str = Field(default="", max_length=120)
    role_name: str = Field(default="微信助手", min_length=1, max_length=120)
    enabled: bool = True
    notification_preferences: dict[str, Any] = Field(default_factory=dict)


DEFAULT_NOTIFICATION_PREFERENCES: dict[str, bool] = {
    "download_started": True,
    "download_completed": True,
    "resource_search": False,
    "status_query": False,
    "wechat_claw_push": True,
}
DEFAULT_WECHAT_CLAW_BINDING_PREFERENCES: dict[str, bool] = {
    "download_started": True,
    "download_completed": True,
    "mteam_exception": True,
    "qb_exception": True,
    "ai_exception": True,
    "tmdb_exception": True,
    "wechat_claw_exception": True,
}
WECHAT_CLAW_AVATAR_KEYS = ("mint", "violet", "coral", "sky", "amber", "rose", "indigo", "lime")

NOTIFICATION_PREFERENCES_KEY = "notification.preferences"
WECHAT_CLAW_INTERACTIONS_KEY = "wechat_claw.interactions"
WECHAT_CLAW_ILINK_STATE_KEY = "wechat_claw.ilink_state"
WECHAT_CLAW_LAST_POLL_KEY = "wechat_claw.last_poll"
WECHAT_CLAW_INTERACTION_LIMIT = 5
WECHAT_CLAW_PENDING_LIMIT = 100
MODULE_HEALTH_PREFIX = "module.health."
WECHAT_CLAW_MOBILE_SELECTION_LIMIT = 10
WECHAT_CLAW_MOBILE_SELECTION_TTL_SECONDS = 30 * 60
MTEAM_INITIAL_DISPLAY_LIMIT = 5
MTEAM_SHOW_ALL_RESULTS_RE = re.compile(r"(?:查看|显示|展示|给我).*(?:全部|完整)(?:资源|结果|列表|信息)?|(?:全部|完整).*(?:资源|结果|列表|信息)", re.IGNORECASE)
WECHAT_CLAW_DOWNLOAD_SELECTION_RE = re.compile(r"^\s*(?:下载|选择|选)\s*(?:第)?\s*(\d+)\s*$", re.IGNORECASE)
WECHAT_CLAW_ADMIN_VERIFY_RE = re.compile(r"^\s*(?:验证管理员密码|管理员验证|验证密码)\s*[:： ]\s*(.+?)\s*$", re.IGNORECASE)
WECHAT_CLAW_DOWNLOAD_CONFIRM_RE = re.compile(r"(?:确认|就它|就这个|就下载|可以下载|开始下载|下吧|下载吧)", re.IGNORECASE)
WECHAT_CLAW_SELECTION_WORDS = {"第一个": 1, "第一部": 1, "第1个": 1, "推荐的": -1, "推荐那个": -1, "推荐的那个": -1}
WECHAT_CLAW_PRIVACY_GRANT_SECONDS = 15 * 60


def wechat_claw_setting_key(key: str, user_id: int | None = None) -> str:
    effective_user_id = user_id if user_id is not None else _WECHAT_CLAW_ACTIVE_USER_ID.get()
    return key if effective_user_id is None else f"{key}.binding.{effective_user_id}"


def wechat_claw_poll_lock(user_id: int | None) -> Lock:
    key = int(user_id or 0)
    with _WECHAT_CLAW_POLL_LOCK_GUARD:
        return _WECHAT_CLAW_POLL_LOCKS.setdefault(key, Lock())


def normalized_wechat_claw_binding_preferences(preferences: dict[str, Any] | None = None) -> dict[str, Any]:
    source = preferences if isinstance(preferences, dict) else {}
    normalized: dict[str, Any] = {
        key: bool(source.get(key, default))
        for key, default in DEFAULT_WECHAT_CLAW_BINDING_PREFERENCES.items()
    }
    try:
        hours = int(source.get("exception_after_hours", 5))
    except (TypeError, ValueError):
        hours = 5
    normalized["exception_after_hours"] = hours if hours in {3, 5, 24} else 5
    return normalized


def next_wechat_claw_member_name(db: Session) -> str:
    names = [item[0] for item in db.query(WechatClawBinding.display_name).all()]
    numbers = [int(match.group(1)) for name in names if (match := re.fullmatch(r"成员(\d+)", str(name).strip()))]
    return f"成员{(max(numbers) if numbers else 0) + 1}"


def next_wechat_claw_avatar_key(db: Session) -> str:
    used = {str(item[0] or "") for item in db.query(WechatClawBinding.avatar_key).all()}
    candidates = [key for key in WECHAT_CLAW_AVATAR_KEYS if key not in used] or list(WECHAT_CLAW_AVATAR_KEYS)
    return secrets.choice(candidates)


def serialize_wechat_claw_binding(binding: WechatClawBinding) -> dict[str, Any]:
    return {
        "id": binding.id,
        "display_name": binding.display_name,
        "role_name": binding.role_name,
        "avatar_key": binding.avatar_key or "mint",
        "enabled": binding.enabled,
        "notification_preferences": normalized_wechat_claw_binding_preferences(binding.notification_preferences),
        "created_at": binding.created_at.isoformat() if binding.created_at else None,
        "updated_at": binding.updated_at.isoformat() if binding.updated_at else None,
    }


def wechat_claw_binding_status(db: Session, binding: WechatClawBinding) -> dict[str, Any]:
    state = get_wechat_claw_ilink_state(db, binding.id)
    last_poll = get_wechat_claw_last_poll(db, binding.id)
    return {
        **serialize_wechat_claw_binding(binding),
        "configured": bool(get_config(db, "wechat_claw") and get_config(db, "wechat_claw").encrypted_payload),
        "connected": bool(state.get("bot_token")),
        "account_id": state.get("account_id") or "",
        "qrcode_status": (state.get("qrcode") or {}).get("status") if isinstance(state.get("qrcode"), dict) else "",
        "last_poll": last_poll,
        "recent_interactions": get_wechat_claw_recent_interactions(db, binding.id),
    }


def module_health_setting_key(module: str) -> str:
    return f"{MODULE_HEALTH_PREFIX}{module}"


def get_module_health(db: Session, module: str) -> dict[str, Any]:
    row = db.query(Setting).filter(Setting.key == module_health_setting_key(module)).one_or_none()
    return row.value if row and isinstance(row.value, dict) else {}


def record_module_collection_result(db: Session, module: str, success: bool, error: str = "") -> dict[str, Any]:
    """Track one result per UTC hour so short retry bursts do not count as hours of outage."""
    key = module_health_setting_key(module)
    row = db.query(Setting).filter(Setting.key == key).one_or_none()
    state = dict(row.value) if row and isinstance(row.value, dict) else {}
    now = datetime.utcnow()
    hour = now.strftime("%Y-%m-%dT%H")
    if success:
        state.update({"consecutive_failed_hours": 0, "last_failure_hour": "", "last_success_at": now.isoformat(), "last_error": "", "last_checked_at": now.isoformat()})
    else:
        if state.get("last_failure_hour") != hour:
            state["consecutive_failed_hours"] = int(state.get("consecutive_failed_hours") or 0) + 1
            state["last_failure_hour"] = hour
        state.update({"last_error": str(error)[:300], "last_checked_at": now.isoformat()})
    if row is None:
        row = Setting(key=key, value=state)
        db.add(row)
    else:
        row.value = state
        row.updated_at = now
    db.commit()

    failed_hours = int(state.get("consecutive_failed_hours") or 0)
    if success or not failed_hours:
        return state
    preference_key = "qb_exception" if module.startswith("qb") else f"{module}_exception"
    targets = [
        binding for binding in ensure_wechat_claw_bindings(db)
        if binding.enabled
        and normalized_wechat_claw_binding_preferences(binding.notification_preferences).get(preference_key)
        and failed_hours == normalized_wechat_claw_binding_preferences(binding.notification_preferences).get("exception_after_hours")
    ]
    if not targets:
        return state
    notification = Notification(
        title=f"模块异常：{module}",
        message=f"{module} 已连续 {failed_hours} 小时未能拉取数据。{str(error)[:180]}",
        level="error",
        source="module_health",
    )
    db.add(notification)
    db.commit()
    db.refresh(notification)
    for binding in targets:
        scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(binding.id)
        try:
            push_wechat_claw_notification(db, notification, preference_key)
        except Exception:
            logger.exception("module exception notification delivery failed: module=%s binding=%s", module, binding.id)
        finally:
            _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)
    return state


def get_wechat_claw_binding_or_error(db: Session, binding_id: int) -> WechatClawBinding:
    binding = db.get(WechatClawBinding, binding_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="WeChat claw 绑定不存在")
    return binding


def ensure_wechat_claw_bindings(db: Session) -> list[WechatClawBinding]:
    bindings = db.query(WechatClawBinding).order_by(WechatClawBinding.id.asc()).all()
    if bindings:
        return bindings
    config = get_decrypted_config(db, "wechat_claw") or {}
    legacy_state = get_wechat_claw_ilink_state(db)
    legacy_prefix = f"{WECHAT_CLAW_ILINK_STATE_KEY}.user."
    legacy_state_rows = db.query(Setting).filter(Setting.key.like(f"{legacy_prefix}%")).all()
    if not config and not legacy_state and not legacy_state_rows:
        return []

    sources: list[int | None] = []
    if legacy_state:
        sources.append(None)
    for row in legacy_state_rows:
        try:
            sources.append(int(str(row.key).removeprefix(legacy_prefix)))
        except ValueError:
            continue
    if not sources:
        sources.append(None)

    migrated: list[tuple[WechatClawBinding, int | None]] = []
    for source_id in dict.fromkeys(sources):
        source_user = db.get(User, source_id) if source_id is not None else None
        display_name = source_user.username if source_user else str(config.get("name") or "微信成员").strip() or "微信成员"
        binding = WechatClawBinding(
            display_name=display_name,
            role_name="微信助手",
            notification_preferences=dict(DEFAULT_WECHAT_CLAW_BINDING_PREFERENCES),
        )
        db.add(binding)
        migrated.append((binding, source_id))
    db.commit()
    for binding, source_id in migrated:
        db.refresh(binding)
        for key in (WECHAT_CLAW_ILINK_STATE_KEY, WECHAT_CLAW_INTERACTIONS_KEY, WECHAT_CLAW_LAST_POLL_KEY):
            source_key = key if source_id is None else f"{key}.user.{source_id}"
            legacy = db.query(Setting).filter(Setting.key == source_key).one_or_none()
            if legacy and isinstance(legacy.value, dict):
                db.add(Setting(key=wechat_claw_setting_key(key, binding.id), value=legacy.value))
    db.commit()
    return [binding for binding, _ in migrated]

WECHAT_CLAW_MOBILE_SECTIONS: list[dict[str, Any]] = [
    {"key": "discover", "label": "发现", "description": "TMDB 趋势、热门、筛选和 M-Team 搜索入口。"},
    {"key": "dashboard", "label": "仪表盘", "description": "M-Team、qB 下载器、NAS 空间总览。"},
    {"key": "downloads", "label": "下载", "description": "qB1/qB2/qB3 下载任务状态与操作入口。"},
    {"key": "notifications", "label": "通知", "description": "通知中心、AI 助手和通知偏好。"},
    {"key": "settings", "label": "设置", "description": "M-Team、qB、TMDB、AI、WeChat claw 等运行配置。"},
    {"key": "diagnostics", "label": "诊断", "description": "管理员诊断健康检查与脱敏导出。"},
]

WECHAT_CLAW_CAPABILITIES: list[dict[str, Any]] = [
    {"action": "tmdb_lookup", "label": "TMDB 影视查询", "examples": ["找个高分韩剧看看", "沙丘 2 评分和简介"]},
    {"action": "mteam_search", "label": "M-Team 资源搜索", "examples": ["搜索沙丘 2160p", "推荐一个沙丘资源"]},
    {"action": "dashboard_query", "label": "仪表盘查询", "examples": ["qB1 现在速度多少", "查一下仪表盘状态"]},
    {"action": "download_selected", "label": "手机端添加下载", "examples": ["第一个", "下载推荐的那个", "确认开始下载"]},
    {"action": "general_chat", "label": "影视中枢对话", "examples": ["周末适合看什么电影"]},
]


def bytes_label(value: float, digits: int = 1) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    current = float(value)
    for unit in units:
        if current < 1024 or unit == units[-1]:
            return f"{current:.{digits}f} {unit}"
        current /= 1024
    return f"{current:.{digits}f} PB"


DEFAULT_STORAGE_MOUNT_PATHS = ("/mnt/storage1", "/mnt/storage2", "/mnt/storage3")


def signed_bytes_delta(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}{bytes_label(abs(value), 2)}"


def signed_number_delta(value: float, digits: int = 1) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}{abs(value):.{digits}f}"


def signed_int_delta(value: int) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}{abs(value)}"


def apply_mteam_five_hour_deltas(db: Session, mteam: dict[str, Any]) -> dict[str, Any]:
    cutoff = datetime.utcnow() - timedelta(hours=5)
    previous = (
        db.query(MTeamSnapshot)
        .filter(MTeamSnapshot.source != "mock", MTeamSnapshot.captured_at <= cutoff)
        .order_by(MTeamSnapshot.captured_at.desc())
        .first()
    )
    mteam["delta_window_label"] = "近5h"
    if previous is None:
        mteam["delta_preview"] = True
        return mteam

    current_level = str(mteam.get("user_level") or "")
    previous_level = str(previous.user_level or "")
    mteam["user_level_delta_label"] = "无变化" if not previous_level or previous_level == current_level else f"{previous_level} → {current_level}"
    mteam["upload_delta_label"] = signed_bytes_delta(float(mteam.get("upload_total") or 0) - float(previous.upload_total or 0))
    mteam["download_delta_label"] = signed_bytes_delta(float(mteam.get("download_total") or 0) - float(previous.download_total or 0))
    mteam["bonus_delta_label"] = signed_number_delta(float(mteam.get("bonus") or 0) - float(previous.bonus or 0), 1)
    mteam["ratio_delta_label"] = signed_number_delta(float(mteam.get("ratio") or 0) - float(previous.ratio or 0), 3)
    mteam["seed_size_delta_label"] = signed_bytes_delta(float(mteam.get("seed_size") or 0) - float(getattr(previous, "seed_size", 0) or 0))
    upload_delta = int(mteam.get("active_uploads") or 0) - int(previous.active_uploads or 0)
    download_delta = int(mteam.get("active_downloads") or 0) - int(previous.active_downloads or 0)
    mteam["active_delta_label"] = f"上传 {signed_int_delta(upload_delta)} / 下载 {signed_int_delta(download_delta)}"
    return mteam


def preload_cache_key(name: str) -> str:
    return f"{PRELOAD_PREFIX}{name}"


def get_preload_cache(db: Session, name: str) -> dict[str, Any] | None:
    row = db.query(Setting).filter(Setting.key == preload_cache_key(name)).one_or_none()
    if not row or not isinstance(row.value, dict) or "payload" not in row.value:
        return None
    return row.value


def set_preload_cache(db: Session, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    row = db.query(Setting).filter(Setting.key == preload_cache_key(name)).one_or_none()
    value = {"payload": payload, "cached_at": datetime.utcnow().isoformat(), "name": name}
    if row is None:
        row = Setting(key=preload_cache_key(name), value=value)
        db.add(row)
    else:
        row.value = value
        row.updated_at = datetime.utcnow()
    db.commit()
    return value


def _rewrite_tmdb_image_url(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        size = match.group("size")
        image_path = match.group("path").lstrip("/")
        if size not in TMDB_IMAGE_SIZES:
            return match.group(0)
        return f"/api/tmdb/image/{size}/{image_path}"

    return TMDB_DIRECT_IMAGE_RE.sub(replace, value)


def rewrite_tmdb_image_urls(payload: Any) -> Any:
    if isinstance(payload, str):
        return _rewrite_tmdb_image_url(payload)
    if isinstance(payload, list):
        return [rewrite_tmdb_image_urls(item) for item in payload]
    if isinstance(payload, dict):
        return {key: rewrite_tmdb_image_urls(value) for key, value in payload.items()}
    return payload


def with_preload_meta(payload: dict[str, Any], cache: dict[str, Any] | None, preloaded: bool) -> dict[str, Any]:
    result = dict(rewrite_tmdb_image_urls(payload))
    result["_preload"] = {
        "preloaded": preloaded,
        "cached_at": cache.get("cached_at") if cache else None,
        "refreshing": False,
    }
    return result


def add_preload_task_once(background_tasks: BackgroundTasks, key: str, task, *args: Any) -> None:
    with _PRELOAD_TASK_LOCK:
        if key in _PRELOAD_TASKS_RUNNING:
            return
        _PRELOAD_TASKS_RUNNING.add(key)
    background_tasks.add_task(run_preload_task_once, key, task, *args)


def run_preload_task_once(key: str, task, *args: Any) -> None:
    try:
        task(*args)
    finally:
        with _PRELOAD_TASK_LOCK:
            _PRELOAD_TASKS_RUNNING.discard(key)


def _safe_tmdb_image_path(image_path: str) -> str:
    clean_path = image_path.strip().lstrip("/")
    if not clean_path or "\\" in clean_path or ".." in clean_path or clean_path.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Invalid TMDB image path")
    if len(clean_path) > 240 or not TMDB_IMAGE_PATH_RE.fullmatch(clean_path):
        raise HTTPException(status_code=400, detail="Invalid TMDB image path")
    return clean_path


def _tmdb_image_cache_file(size: str, image_path: str) -> Path:
    digest = hashlib.sha1(f"{size}/{image_path}".encode("utf-8")).hexdigest()
    suffix = Path(image_path).suffix.lower() or ".jpg"
    return Path(settings.tmdb_image_cache_dir) / f"{digest}{suffix}"


def _tmdb_image_headers(cache_file: Path) -> dict[str, str]:
    stat = cache_file.stat()
    return {
        "Cache-Control": "public, max-age=86400, stale-while-revalidate=604800",
        "ETag": f'W/"{stat.st_mtime_ns:x}-{stat.st_size:x}"',
    }


def _valid_tmdb_image_bytes(content: bytes) -> bool:
    if len(content) < 12:
        return False
    if content.startswith(b"\xff\xd8\xff"):
        return True
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return True
    if content[4:8] == b"ftyp" and content[8:12] in {b"avif", b"avis"}:
        return True
    return False


def _tmdb_image_media_type_from_bytes(content: bytes, fallback: str = "image/jpeg") -> str:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    if len(content) >= 12 and content[4:8] == b"ftyp" and content[8:12] in {b"avif", b"avis"}:
        return "image/avif"
    return fallback


def _valid_tmdb_image_file(cache_file: Path) -> bool:
    try:
        with cache_file.open("rb") as handle:
            return _valid_tmdb_image_bytes(handle.read(32))
    except OSError:
        return False


def _tmdb_cached_image_media_type(cache_file: Path, fallback: str) -> str:
    try:
        with cache_file.open("rb") as handle:
            return _tmdb_image_media_type_from_bytes(handle.read(32), fallback)
    except OSError:
        return fallback


def _tmdb_image_error_label(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        return f"http_{int(exc.code)}"
    if isinstance(exc, TmdbDohError):
        return exc.error_type
    if isinstance(exc, URLError):
        reason = str(getattr(exc, "reason", exc))
        return f"url_error:{reason}"[:180]
    return f"{exc.__class__.__name__}:{exc}"[:180]


def _tmdb_image_placeholder_response(reason: str = "fetch_failed") -> Response:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="342" height="513" viewBox="0 0 342 513">'
        '<rect width="342" height="513" fill="#e5e9f0"/>'
        '<path d="M96 208h150v97H96z" fill="#cbd5e1"/>'
        '<circle cx="130" cy="238" r="17" fill="#94a3b8"/>'
        '<path d="M107 288l44-45 31 31 21-22 34 36z" fill="#94a3b8"/>'
        '<text x="171" y="350" text-anchor="middle" font-family="Arial,sans-serif" '
        'font-size="24" font-weight="700" fill="#64748b">No Image</text>'
        "</svg>"
    )
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "no-store",
            "X-TMDB-Image-Status": "placeholder",
            "X-TMDB-Image-Error": reason[:180],
        },
    )


def _prune_tmdb_image_cache(cache_dir: Path) -> None:
    max_bytes = max(1, int(settings.tmdb_image_cache_max_mb)) * 1024 * 1024
    files = [path for path in cache_dir.iterdir() if path.is_file() and not path.name.endswith(".tmp")]
    total = sum(path.stat().st_size for path in files)
    if total <= max_bytes:
        return
    for path in sorted(files, key=lambda item: item.stat().st_mtime):
        try:
            total -= path.stat().st_size
            path.unlink()
        except OSError:
            continue
        if total <= max_bytes:
            break


def _tmdb_image_network_config(db: Session) -> tuple[str, str, int]:
    row = get_config(db, "tmdb")
    config = get_decrypted_config(db, "tmdb") if row and row.encrypted_payload else {}
    mode = str((config or {}).get("mode") or settings.tmdb_mode or "direct").strip().lower()
    if mode not in {"direct", "proxy"}:
        mode = "direct"
    proxy_url = str((config or {}).get("proxy_url") or settings.tmdb_proxy_url or DEFAULT_TMDB_PROXY_URL).strip()
    timeout = int((config or {}).get("timeout") or 12)
    return mode, proxy_url, timeout


def refresh_dashboard_preload_task() -> None:
    db = SessionLocal()
    try:
        refresh_dashboard_preload(db)
    except Exception:
        pass
    finally:
        db.close()


def refresh_discover_preload_task() -> None:
    db = SessionLocal()
    try:
        refresh_discover_preload(db)
    except Exception:
        pass
    finally:
        db.close()


def refresh_discover_filter_preload_task(filters: dict[str, Any]) -> None:
    db = SessionLocal()
    try:
        refresh_discover_filter_preload(db, filters)
    except Exception:
        pass
    finally:
        db.close()


def refresh_download_preload_task(downloader_id: str) -> None:
    db = SessionLocal()
    try:
        refresh_download_preload(db, downloader_id)
    except Exception:
        pass
    finally:
        db.close()


def tmdb_network_detail_from_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    mode = str(config.get("mode") or settings.tmdb_mode or "direct").strip().lower()
    if mode not in {"direct", "proxy"}:
        mode = "direct"
    proxy_url = str(config.get("proxy_url") or settings.tmdb_proxy_url or DEFAULT_TMDB_PROXY_URL).strip()
    proxy_host = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}").netloc if proxy_url else ""
    return {
        "network_mode": mode,
        "route_label": "TMDB：mihomo VPN 代理" if mode == "proxy" else "TMDB：直连 + DoH",
        "proxy_enabled": mode == "proxy",
        "proxy_url": proxy_host if mode == "proxy" else "",
        "non_tmdb_policy": "direct_only",
    }


def tmdb_test_result(success: bool, trace: str, message: str, explanation: str, next_step: str, error_type: str | None = None, http_status: int | None = None, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "success": success,
        "provider": "tmdb",
        "mode": "real",
        "http_status": http_status,
        "duration_ms": 0,
        "message": message,
        "explanation": explanation,
        "next_step": next_step,
        "error_type": error_type,
        "can_enable": success,
        "trace_id": trace,
        "detail": detail or tmdb_network_detail_from_config(),
    }


def classify_tmdb_test_error(exc: Exception, trace: str) -> dict[str, Any]:
    if isinstance(exc, TmdbConfigError):
        return tmdb_test_result(
            False,
            trace,
            "还没有填写 TMDB 密钥。",
            "请填写 TMDB Bearer Token 后再测试连接。",
            "从 TMDB 账号设置复制 Bearer Token 并保存。",
            "missing_credentials",
        )
    if isinstance(exc, TmdbDohError):
        if exc.error_type == "doh_bad_answer":
            return tmdb_test_result(
                False,
                trace,
                "暂时无法连接 TMDB。",
                "当前网络未能完成连接。",
                "请稍后重试；如持续失败，可在高级设置中启用代理。",
                "doh_bad_answer",
            )
        return tmdb_test_result(
            False,
            trace,
            "暂时无法连接 TMDB。",
            "当前网络未能完成连接。",
            "请稍后重试；如持续失败，可在高级设置中启用代理。",
            "doh_unavailable",
        )
    if isinstance(exc, TmdbImageError):
        return tmdb_test_result(
            False,
            trace,
            "TMDB 已连接，但海报暂时无法加载。",
            "影视信息可正常使用，部分海报可能显示为空。",
            "请稍后重试；如持续发生，可在高级设置中启用代理。",
            "tmdb_image_network_error",
            detail=getattr(exc, "network_detail", None) or tmdb_network_detail_from_config(),
        )
    if isinstance(exc, HTTPError):
        status_code = int(exc.code)
        if status_code in (401, 403):
            return tmdb_test_result(
                False,
                trace,
                "TMDB 密钥验证失败。",
                "请检查 Bearer Token 是否有效或重新复制后再试。",
                "更新 Bearer Token 后重新测试。",
                "invalid_credentials",
                status_code,
            )
        if status_code == 429:
            return tmdb_test_result(False, trace, "TMDB 请求太频繁。", "TMDB 暂时限制了请求频率，密钥本身不一定有问题。", "请等待几分钟后再点击“保存并测试”。", "rate_limited", status_code)
        if status_code >= 500:
            return tmdb_test_result(False, trace, "TMDB 服务暂时不可用。", "TMDB 服务器返回了异常状态，通常需要稍后重试。", "请稍后再测试。如果一直失败，再检查网络或代理设置。", "tmdb_service_error", status_code)
        return tmdb_test_result(False, trace, "TMDB 暂时无法完成请求。", "服务返回了异常响应。", "请稍后重试。", "http_error", status_code)
    if isinstance(exc, (TimeoutError, SocketTimeout)):
        return tmdb_test_result(False, trace, "连接 TMDB 超时。", "当前网络响应较慢。", "请稍后重试；如持续发生，可在高级设置中调整连接方式。", "timeout")
    if isinstance(exc, URLError):
        return tmdb_test_result(False, trace, "无法连接 TMDB。", "当前网络未能完成连接。", "请稍后重试；如持续失败，可在高级设置中调整连接方式。", "network_error")
    return tmdb_test_result(False, trace, "TMDB 测试失败。", "暂时无法完成测试。", "请稍后重试。", "unknown_error")


def classify_tmdb_gateway_test_error(exc: Exception, trace: str, phase: str = "api") -> dict[str, Any]:
    result = classify_tmdb_test_error(exc, trace)
    result["message"] = "当前连接方式已不再支持。"
    result["explanation"] = "请使用 TMDB 的默认连接方式，或在高级设置中配置代理。"
    result["next_step"] = "保存后重新测试。"
    result["error_type"] = "gateway_removed"
    return result


def mock_test_result(provider: str, trace: str, row: IntegrationConfig | None) -> dict[str, Any]:
    return {
        "success": True,
        "provider": provider,
        "mode": "mock",
        "http_status": 200,
        "duration_ms": 120,
        "message": "Mock 只读连接测试成功。",
        "trace_id": trace,
        "config_version": row.config_version if row else 0,
    }


def qb_config_test_result(provider: str, trace: str, config: dict[str, Any], row: IntegrationConfig | None) -> dict[str, Any]:
    missing = [
        label
        for key, label in (("base_url", "qB WebUI 地址"), ("username", "用户名"), ("password", "密码"))
        if not str(config.get(key) or "").strip()
    ]
    success = not missing
    return {
        "success": success,
        "provider": provider,
        "mode": "config",
        "http_status": None,
        "duration_ms": 0,
        "message": "qB 配置字段完整。" if success else "qB 配置还缺少必填项。",
        "explanation": "已保存下载器连接信息。" if success else f"缺少：{', '.join(missing)}。",
        "next_step": "请测试连接后再启用。" if success else "请填好 WebUI 地址、用户名和密码后再测试。",
        "error_type": None if success else "missing_required_fields",
        "can_enable": False,
        "trace_id": trace,
        "config_version": row.config_version if row else 0,
    }


def qb_test_result(success: bool, provider: str, trace: str, message: str, explanation: str, next_step: str, error_type: str | None = None, http_status: int | None = None, can_enable: bool | None = None, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "success": success,
        "provider": provider,
        "mode": "real",
        "http_status": http_status,
        "duration_ms": 0,
        "message": message,
        "explanation": explanation,
        "next_step": next_step,
        "error_type": error_type,
        "can_enable": success if can_enable is None else can_enable,
        "trace_id": trace,
        "detail": detail or {},
    }


def classify_qb_test_error(provider: str, exc: Exception, trace: str) -> dict[str, Any]:
    if isinstance(exc, QbittorrentConfigError):
        return qb_test_result(
            False,
            provider,
            trace,
            "qB 配置还缺少必填项。",
            "请填写 WebUI 地址、用户名和密码。",
            "请填好 WebUI 地址、用户名和密码后再测试。",
            "missing_required_fields",
            can_enable=False,
        )
    if isinstance(exc, QbittorrentApiError):
        http_status = exc.http_status
        if http_status in (401, 403):
            return qb_test_result(
                False,
                provider,
                trace,
                "qB 登录失败。",
                "用户名或密码可能不正确。",
                "请检查 WebUI 地址、用户名和密码后重试。",
                "invalid_credentials",
                http_status,
                False,
            )
        return qb_test_result(
            False,
            provider,
            trace,
            "qB 连接测试失败。",
            "暂时无法连接下载器。",
            "请检查 WebUI 地址后重新测试。",
            "qb_api_error",
            http_status,
            False,
        )
    return qb_test_result(
        False,
        provider,
        trace,
        "qB 连接测试失败。",
        "暂时无法完成连接测试。",
        "请检查 WebUI 地址、用户名和密码后重试。",
        "unknown_error",
        can_enable=False,
    )


def mteam_test_result(success: bool, trace: str, message: str, explanation: str, next_step: str, error_type: str | None = None, http_status: int | None = None, can_enable: bool | None = None) -> dict[str, Any]:
    return {
        "success": success,
        "provider": "mteam",
        "mode": "real",
        "http_status": http_status,
        "duration_ms": 0,
        "message": message,
        "explanation": explanation,
        "next_step": next_step,
        "error_type": error_type,
        "can_enable": success if can_enable is None else can_enable,
        "trace_id": trace,
    }


def classify_mteam_test_error(exc: Exception, trace: str) -> dict[str, Any]:
    if isinstance(exc, MTeamConfigError):
        return mteam_test_result(
            False,
            trace,
            "还没有填写 M-Team API Key。",
            "真实 M-Team API 需要 API Key。Cookie 和 Passkey 不能替代 API Key。",
            "请在 M-Team 个人资料里复制 API Key，填入设置页的“API Key”字段后再保存并测试。",
            "missing_api_key",
        )
    if isinstance(exc, MTeamApiError):
        message = str(exc)
        lower = message.lower()
        if "key" in lower or "401" in lower or "403" in lower or str(exc.code) in {"401", "403"}:
            return mteam_test_result(
                False,
                trace,
                "M-Team API Key 验证失败。",
                "M-Team 已响应请求，但拒绝了当前 API Key。",
                "请确认填入的是 M-Team API Key，不是下载 Passkey；如果刚生成过 Key，请刷新站点后重新复制。",
                "invalid_api_key",
                exc.http_status,
            )
        return mteam_test_result(
            False,
            trace,
            "M-Team API 返回失败。",
            f"M-Team 返回：{message}",
            "请检查 API Key、站点地址和网络环境，然后重试。",
            "mteam_api_error",
            exc.http_status,
        )
    return mteam_test_result(
        False,
        trace,
        "M-Team 测试失败。",
        "应用遇到了未能自动识别的问题。",
        "请检查填写内容后重试；如果仍然失败，可以把这条提示截图发给开发者排查。",
        "unknown_error",
    )


def mteam_connection_payload(row: IntegrationConfig | None) -> dict[str, Any]:
    result = row.last_test_result if row else None
    configured = bool(row and row.redacted_summary)
    enabled = bool(row and row.enabled)
    last_test_success = bool(result and result.get("success"))
    if enabled and last_test_success:
        message = "M-Team 已启用，仪表盘正在读取真实站点数据。"
    elif configured:
        message = "M-Team 配置已保存，请在设置页完成测试并启用。"
    else:
        message = "M-Team 尚未配置。"
    return {
        "configured": configured,
        "enabled": enabled,
        "last_test_success": last_test_success,
        "config_version": row.config_version if row else 0,
        "last_tested_at": row.last_tested_at.isoformat() if row and row.last_tested_at else None,
        "last_test_result": result,
        "data_source": "M-Team 原始数据（Real API）",
        "message": message,
    }


def ai_test_result(success: bool, trace: str, message: str, explanation: str, next_step: str, error_type: str | None = None, http_status: int | None = None, can_enable: bool | None = None, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "success": success,
        "provider": "ai",
        "mode": "real",
        "http_status": http_status,
        "duration_ms": 0,
        "message": message,
        "explanation": explanation,
        "next_step": next_step,
        "error_type": error_type,
        "can_enable": success if can_enable is None else can_enable,
        "trace_id": trace,
        "detail": detail or {},
    }


def classify_ai_test_error(exc: Exception, trace: str) -> dict[str, Any]:
    if isinstance(exc, AIConfigError):
        return ai_test_result(
            False,
            trace,
            "AI 助手尚未配置完成。",
            "请填写 API Key 后再测试。",
            "保存 API Key 并重新测试。",
            "missing_credentials",
            can_enable=False,
        )
    if isinstance(exc, AIServiceError):
        return ai_test_result(
            False,
            trace,
            "AI 助手暂时无法连接。",
            "请检查 API Key 是否有效，以及服务是否可用。",
            "请稍后重试。",
            "deepseek_api_error",
            exc.http_status,
            False,
        )
    return ai_test_result(
        False,
        trace,
        "AI 助手测试失败。",
        "暂时无法完成测试。",
        "请稍后重试。",
        "unknown_error",
        can_enable=False,
    )


def get_ai_adapter_or_error(db: Session) -> DeepSeekChatAdapter:
    row = get_config(db, "ai")
    if not row or not row.encrypted_payload:
        raise HTTPException(status_code=409, detail="请先在设置中配置 AI 助手。")
    if not row.enabled:
        raise HTTPException(status_code=409, detail="AI 模块尚未启用，请先在设置中测试并启用。")
    try:
        return DeepSeekChatAdapter(get_decrypted_config(db, "ai") or {})
    except AIConfigError as exc:
        raise HTTPException(status_code=409, detail="AI 助手配置不完整。") from exc


def format_assistant_result(intent: dict[str, Any], result: dict[str, Any]) -> str:
    action = intent.get("action")
    if action == "resource_search":
        items = result.get("items") or []
        if not items:
            return "没有找到匹配的 M-Team 资源。"
        lines = ["找到这些资源："]
        for index, item in enumerate(items[:5], 1):
            lines.append(f"{index}. {item.get('title') or item.get('name') or '-'} / {item.get('resolution') or '-'} / {item.get('size') or '-'} / 做种 {item.get('seeders', 0)} / ID {item.get('id') or '-'}")
        return "\n".join(lines)
    if action == "download_started":
        return f"已记录下载开始通知：{result.get('notification', {}).get('title', '下载已开始')}"
    if action == "download_completed":
        return f"已记录下载完成通知：{result.get('notification', {}).get('title', '下载已完成')}"
    if action == "status_query":
        if result.get("target") == "mteam":
            data = result.get("mteam") or result
            return f"M-Team 状态：等级 {data.get('user_level', '-')}，分享率 {data.get('ratio', 0)}，做种 {data.get('seed_count', 0)}。"
        if result.get("target") in {"qb", "downloads"}:
            qbs = result.get("qbs") or []
            if qbs:
                return "qB 状态：" + "；".join(f"{item.get('name') or item.get('id')} 下载 {bytes_label(item.get('download_speed') or 0)}/s，上传 {bytes_label(item.get('upload_speed') or 0)}/s，任务 {item.get('active_downloads', 0)}" for item in qbs)
        overview = result.get("overview") or {}
        return f"总览：下载 {bytes_label(overview.get('total_download_speed') or 0)}/s，上传 {bytes_label(overview.get('total_upload_speed') or 0)}/s，活动下载 {overview.get('download_tasks', 0)}，NAS {overview.get('nas_space_label') or '-'}。"
    return "请求已处理。"


def execute_assistant_intent(db: Session, intent: dict[str, Any], message: str, user: User) -> dict[str, Any]:
    action = str(intent.get("action") or "")
    if action == "resource_search":
        query = str(intent.get("query") or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail="资源查询缺少关键词。")
        try:
            items = get_mteam_adapter_or_error(db).search_torrents(query)[: int(intent.get("limit") or 5)]
        except MTeamApiError as exc:
            raise HTTPException(status_code=502, detail=f"M-Team 搜索失败：{exc}") from exc
        return {"action": action, "query": query, "items": items, "count": len(items)}

    if action in {"download_started", "download_completed"}:
        title = "下载已开始" if action == "download_started" else "下载已完成"
        detail = str(intent.get("message") or message or "").strip() or title
        row = Notification(title=title, message=detail, level="info" if action == "download_started" else "success", source="ai")
        db.add(row)
        db.commit()
        db.refresh(row)
        return {
            "action": action,
            "notification": {
                "id": row.id,
                "title": row.title,
                "message": row.message,
                "level": row.level,
                "source": row.source,
                "created_at": row.created_at.isoformat(),
            },
        }

    if action == "status_query":
        target = str(intent.get("target") or "dashboard")
        downloader_id = str(intent.get("downloader_id") or "all")
        if target == "mteam":
            return {"action": action, "target": target, "mteam": mteam_stats(db, user)}
        if target in {"qb", "downloads"}:
            if downloader_id in {"qb1", "qb2", "qb3"}:
                state = get_qb_state_for_dashboard(db, downloader_id)
                return {"action": action, "target": target, "qbs": [state]}
            return {"action": action, "target": target, **build_dashboard_qbs_payload(db)}
        if target == "notifications":
            return {"action": action, "target": target, **notifications(user, db)}
        return {"action": action, "target": "dashboard", **build_dashboard_payload(db)}

    raise HTTPException(status_code=400, detail="不支持的 AI 指令。")


def get_wechat_claw_adapter_or_error(db: Session) -> WechatClawAdapter:
    row = get_config(db, "wechat_claw")
    if not row or not row.encrypted_payload:
        raise HTTPException(status_code=409, detail="请先在设置中配置 WeChat claw。")
    if not row.enabled:
        raise HTTPException(status_code=409, detail="WeChat claw 尚未启用，请先在设置中测试并启用。")
    try:
        config = get_decrypted_config(db, "wechat_claw") or {}
        state = get_wechat_claw_ilink_state(db)
        return WechatClawAdapter(wechat_claw_config_with_state(config, state))
    except WechatClawConfigError as exc:
        raise HTTPException(status_code=409, detail=f"WeChat claw 配置不完整：{exc}") from exc


def get_notification_preferences(db: Session) -> dict[str, bool]:
    row = db.query(Setting).filter(Setting.key == NOTIFICATION_PREFERENCES_KEY).one_or_none()
    value = row.value if row and isinstance(row.value, dict) else {}
    merged = dict(DEFAULT_NOTIFICATION_PREFERENCES)
    for key in DEFAULT_NOTIFICATION_PREFERENCES:
        if key in value:
            merged[key] = bool(value[key])
    return merged


def save_notification_preferences(db: Session, preferences: dict[str, bool]) -> dict[str, bool]:
    current = get_notification_preferences(db)
    normalized = {
        key: bool(preferences.get(key, current[key]))
        for key in DEFAULT_NOTIFICATION_PREFERENCES
    }
    row = db.query(Setting).filter(Setting.key == NOTIFICATION_PREFERENCES_KEY).one_or_none()
    if row is None:
        row = Setting(key=NOTIFICATION_PREFERENCES_KEY, value=normalized)
        db.add(row)
    else:
        row.value = normalized
        row.updated_at = datetime.utcnow()
    db.commit()
    return normalized


def get_wechat_claw_recent_interactions(db: Session, user_id: int | None = None) -> list[dict[str, Any]]:
    row = db.query(Setting).filter(Setting.key == wechat_claw_setting_key(WECHAT_CLAW_INTERACTIONS_KEY, user_id)).one_or_none()
    value = row.value if row and isinstance(row.value, dict) else {}
    items = value.get("items") if isinstance(value, dict) else []
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)][:WECHAT_CLAW_INTERACTION_LIMIT]


def new_wechat_claw_interaction_telemetry() -> dict[str, Any]:
    return {"trace_id": trace_id("WXC"), "started": time.perf_counter(), "stages": []}


def record_wechat_claw_stage(
    telemetry: dict[str, Any],
    stage: str,
    started: float,
    *,
    status: str = "success",
    error: Any = None,
) -> None:
    item: dict[str, Any] = {
        "stage": stage,
        "status": status,
        "duration_ms": max(0, int((time.perf_counter() - started) * 1000)),
    }
    if error:
        item["error"] = trim_wechat_claw_text(redact_payload(str(error)), 240)
    telemetry.setdefault("stages", []).append(item)


def serialize_wechat_claw_interaction_telemetry(telemetry: dict[str, Any] | None) -> dict[str, Any]:
    value = telemetry if isinstance(telemetry, dict) else {}
    started = value.get("started")
    total_duration_ms = max(0, int((time.perf_counter() - started) * 1000)) if isinstance(started, (float, int)) else 0
    stages = value.get("stages") if isinstance(value.get("stages"), list) else []
    return {
        "trace_id": trim_wechat_claw_text(value.get("trace_id"), 80),
        "duration_ms": total_duration_ms,
        "stages": stages[-12:],
    }


def get_wechat_claw_ilink_state(db: Session, user_id: int | None = None) -> dict[str, Any]:
    row = db.query(Setting).filter(Setting.key == wechat_claw_setting_key(WECHAT_CLAW_ILINK_STATE_KEY, user_id)).one_or_none()
    return row.value if row and isinstance(row.value, dict) else {}


def list_wechat_claw_binding_user_ids(db: Session) -> list[int | None]:
    bindings = ensure_wechat_claw_bindings(db)
    ids: list[int] = []
    for binding in bindings:
        state = get_wechat_claw_ilink_state(db, binding.id)
        if binding.enabled and state.get("bot_token"):
            ids.append(binding.id)
    return ids


def save_wechat_claw_ilink_state(db: Session, state: dict[str, Any], user_id: int | None = None) -> dict[str, Any]:
    key = wechat_claw_setting_key(WECHAT_CLAW_ILINK_STATE_KEY, user_id)
    row = db.query(Setting).filter(Setting.key == key).one_or_none()
    if row is None:
        row = Setting(key=key, value=state)
        db.add(row)
    else:
        row.value = state
        row.updated_at = datetime.utcnow()
    db.commit()
    return state


def update_wechat_claw_ilink_state(db: Session, user_id: int | None = None, **kwargs: Any) -> dict[str, Any]:
    state = get_wechat_claw_ilink_state(db, user_id)
    state.update({key: value for key, value in kwargs.items() if value is not None})
    return save_wechat_claw_ilink_state(db, state, user_id)


def get_wechat_claw_last_poll(db: Session, user_id: int | None = None) -> dict[str, Any]:
    row = db.query(Setting).filter(Setting.key == wechat_claw_setting_key(WECHAT_CLAW_LAST_POLL_KEY, user_id)).one_or_none()
    return row.value if row and isinstance(row.value, dict) else {}


def save_wechat_claw_mobile_candidates(
    db: Session,
    request: WechatClawMessageRequest,
    items: list[dict[str, Any]],
    *,
    query: str = "",
    recommended_index: int | None = None,
    presentation: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Persist a short-lived, per-chat list so the next mobile message can select it safely."""
    state = get_wechat_claw_ilink_state(db)
    selections = state.get("mobile_selections") if isinstance(state.get("mobile_selections"), dict) else {}
    selection_key = str(request.user_id or request.conversation_id or "default").strip() or "default"
    candidates = [
        {
            "id": str(item.get("id") or "").strip(),
            "title": str(item.get("title") or item.get("name") or "资源").strip()[:180],
            "subtitle": str(item.get("subtitle") or "").strip()[:300],
            "resolution": str(item.get("resolution") or "").strip(),
            "size": str(item.get("size") or "").strip(),
            "size_bytes": int(item.get("size_bytes") or 0),
            "seeders": int(item.get("seeders") or 0),
            "promotion_label": str(item.get("promotion_label") or "普通").strip(),
            "promotion_type": str(item.get("promotion_type") or "").strip(),
            "codec": str(item.get("codec") or "").strip(),
            "hdr": str(item.get("hdr") or "").strip(),
            "group": str(item.get("group") or "").strip(),
            "presentation": (presentation or {}).get(str(item.get("id") or ""), {}),
        }
        for item in items[:WECHAT_CLAW_MOBILE_SELECTION_LIMIT]
        if str(item.get("id") or "").strip()
    ]
    selections[selection_key] = {
        "created_at": datetime.utcnow().isoformat(),
        "context_token": str(request.context_token or ""),
        "query": str(query or "").strip()[:160],
        "recommended_index": int(recommended_index or 0),
        "items": candidates,
    }
    update_wechat_claw_ilink_state(db, mobile_selections=selections)
    return {"selection_key": selection_key, "items": candidates, "expires_in_seconds": WECHAT_CLAW_MOBILE_SELECTION_TTL_SECONDS}


def get_wechat_claw_mobile_search_result(db: Session, request: WechatClawMessageRequest) -> dict[str, Any] | None:
    state = get_wechat_claw_ilink_state(db)
    selections = state.get("mobile_selections") if isinstance(state.get("mobile_selections"), dict) else {}
    selection_key = wechat_claw_mobile_key(request)
    selection = selections.get(selection_key) if isinstance(selections.get(selection_key), dict) else {}
    try:
        created_at = datetime.fromisoformat(str(selection.get("created_at") or ""))
    except ValueError:
        created_at = datetime.min
    if (datetime.utcnow() - created_at).total_seconds() > WECHAT_CLAW_MOBILE_SELECTION_TTL_SECONDS:
        if selection:
            selections.pop(selection_key, None)
            update_wechat_claw_ilink_state(db, mobile_selections=selections)
        return None
    items = selection.get("items") if isinstance(selection.get("items"), list) else []
    if not items:
        return None
    return {
        "intent_type": "mteam_search",
        "query": str(selection.get("query") or ""),
        "items": [dict(item) for item in items if isinstance(item, dict)],
        "count": len(items),
        "recommended_index": int(selection.get("recommended_index") or 0) or None,
        "show_all": True,
    }


def get_wechat_claw_mobile_candidate(db: Session, request: WechatClawMessageRequest, index: int) -> dict[str, Any] | None:
    if index < 1 or index > WECHAT_CLAW_MOBILE_SELECTION_LIMIT:
        return None
    state = get_wechat_claw_ilink_state(db)
    selections = state.get("mobile_selections") if isinstance(state.get("mobile_selections"), dict) else {}
    selection_key = str(request.user_id or request.conversation_id or "default").strip() or "default"
    selection = selections.get(selection_key) if isinstance(selections.get(selection_key), dict) else {}
    try:
        created_at = datetime.fromisoformat(str(selection.get("created_at") or ""))
    except ValueError:
        created_at = datetime.min
    if (datetime.utcnow() - created_at).total_seconds() > WECHAT_CLAW_MOBILE_SELECTION_TTL_SECONDS:
        selections.pop(selection_key, None)
        update_wechat_claw_ilink_state(db, mobile_selections=selections)
        return None
    items = selection.get("items") if isinstance(selection.get("items"), list) else []
    candidate = items[index - 1] if len(items) >= index and isinstance(items[index - 1], dict) else None
    return dict(candidate) if candidate else None


def wechat_claw_mobile_key(request: WechatClawMessageRequest) -> str:
    return str(request.user_id or request.conversation_id or "default").strip() or "default"


def save_wechat_claw_pending_download(db: Session, request: WechatClawMessageRequest, candidate: dict[str, Any]) -> None:
    state = get_wechat_claw_ilink_state(db)
    pending = state.get("mobile_pending_downloads") if isinstance(state.get("mobile_pending_downloads"), dict) else {}
    pending[wechat_claw_mobile_key(request)] = {"created_at": datetime.utcnow().isoformat(), "candidate": candidate}
    update_wechat_claw_ilink_state(db, mobile_pending_downloads=pending)


def get_wechat_claw_pending_download(db: Session, request: WechatClawMessageRequest) -> dict[str, Any] | None:
    state = get_wechat_claw_ilink_state(db)
    pending = state.get("mobile_pending_downloads") if isinstance(state.get("mobile_pending_downloads"), dict) else {}
    item = pending.get(wechat_claw_mobile_key(request)) if isinstance(pending.get(wechat_claw_mobile_key(request)), dict) else {}
    try:
        fresh = (datetime.utcnow() - datetime.fromisoformat(str(item.get("created_at") or ""))).total_seconds() <= WECHAT_CLAW_MOBILE_SELECTION_TTL_SECONDS
    except ValueError:
        fresh = False
    candidate = item.get("candidate") if isinstance(item.get("candidate"), dict) else None
    if fresh and candidate:
        return dict(candidate)
    if item:
        pending.pop(wechat_claw_mobile_key(request), None)
        update_wechat_claw_ilink_state(db, mobile_pending_downloads=pending)
    return None


def clear_wechat_claw_pending_download(db: Session, request: WechatClawMessageRequest) -> None:
    state = get_wechat_claw_ilink_state(db)
    pending = state.get("mobile_pending_downloads") if isinstance(state.get("mobile_pending_downloads"), dict) else {}
    pending.pop(wechat_claw_mobile_key(request), None)
    update_wechat_claw_ilink_state(db, mobile_pending_downloads=pending)


def grant_wechat_claw_privacy_access(db: Session, request: WechatClawMessageRequest) -> None:
    state = get_wechat_claw_ilink_state(db)
    grants = state.get("mobile_privacy_grants") if isinstance(state.get("mobile_privacy_grants"), dict) else {}
    grants[wechat_claw_mobile_key(request)] = {"expires_at": (datetime.utcnow() + timedelta(seconds=WECHAT_CLAW_PRIVACY_GRANT_SECONDS)).isoformat()}
    update_wechat_claw_ilink_state(db, mobile_privacy_grants=grants)


def has_wechat_claw_privacy_access(db: Session, request: WechatClawMessageRequest) -> bool:
    state = get_wechat_claw_ilink_state(db)
    grants = state.get("mobile_privacy_grants") if isinstance(state.get("mobile_privacy_grants"), dict) else {}
    entry = grants.get(wechat_claw_mobile_key(request)) if isinstance(grants.get(wechat_claw_mobile_key(request)), dict) else {}
    try:
        return datetime.fromisoformat(str(entry.get("expires_at") or "")) > datetime.utcnow()
    except ValueError:
        return False


def redact_wechat_claw_message(message: str) -> str:
    return "[管理员密码验证已脱敏]" if WECHAT_CLAW_ADMIN_VERIFY_RE.match(message or "") else message


def resolve_wechat_claw_default_downloader(db: Session) -> str:
    config = get_decrypted_config(db, "wechat_claw") or {}
    configured = str(config.get("default_downloader_id") or "all").strip().lower()
    if configured in {"qb1", "qb2", "qb3"}:
        return configured
    for downloader_id in ("qb1", "qb2", "qb3"):
        row = get_config(db, downloader_id)
        if row and row.enabled and row.encrypted_payload:
            return downloader_id
    raise HTTPException(status_code=409, detail="未找到可用的默认 qB 下载器，请先在设置中启用 qB1、qB2 或 qB3。")


def download_wechat_claw_selected_torrent(db: Session, torrent_id: str, actor: User) -> dict[str, Any]:
    downloader_id = resolve_wechat_claw_default_downloader(db)
    stage = "mteam_download_torrent"
    try:
        torrent_file = get_mteam_adapter_or_error(db).download_torrent_file(torrent_id)
        stage = "qb_add_torrent_file"
        result = get_qb_adapter_or_error(db, downloader_id).add_torrent_file(
            downloader_id,
            torrent_file["filename"],
            torrent_file["content"],
            {},
        )
    except MTeamApiError as exc:
        raise HTTPException(status_code=502, detail=f"下载种子失败：{exc}") from exc
    except QbittorrentApiError as exc:
        raise HTTPException(status_code=502, detail=f"推送到 {downloader_id} 失败：{exc}") from exc
    db.add(DownloadAction(trace_id=result["trace_id"], downloader_id=downloader_id, action="add_mteam_mobile", target_hash=torrent_id, actor_user_id=actor.id, status="accepted"))
    db.commit()
    return {**result, "torrent_id": torrent_id, "filename": torrent_file["filename"], "downloader_id": downloader_id}


def rank_mteam_search_items(items: list[dict[str, Any]], filters: dict[str, Any]) -> tuple[list[dict[str, Any]], int | None]:
    resolution = str(filters.get("resolution") or "").lower()
    promotion = str(filters.get("promotion") or "any").lower()
    # M-Team search always exposes a ranked recommendation. The model may omit
    # this optional preference, but it must not make the recommendation vanish.
    recommend = True
    min_size = max(0.0, float(filters.get("min_size_gb") or 0))
    max_size = max(0.0, float(filters.get("max_size_gb") or 0))
    filtered = list(items)
    if resolution:
        matched = [item for item in filtered if resolution in str(item.get("resolution") or "").lower() or resolution in " ".join(item.get("labels") or []).lower()]
        if matched:
            filtered = matched
    if promotion in {"free", "discount"}:
        matched = [item for item in filtered if promotion == str(item.get("promotion_type") or "").lower() or (promotion == "discount" and bool(item.get("promotion_label")))]
        if matched:
            filtered = matched
    if min_size or max_size:
        matched = []
        for item in filtered:
            size_gb = float(item.get("size_bytes") or 0) / (1024 ** 3)
            if (not min_size or size_gb >= min_size) and (not max_size or size_gb <= max_size):
                matched.append(item)
        if matched:
            filtered = matched

    def recommendation_score(item: dict[str, Any]) -> tuple[float, ...]:
        promo_type = str(item.get("promotion_type") or "").lower()
        promo_rank = 3 if promo_type == "free" or "FREE" in str(item.get("promotion_label") or "").upper() else 2 if promo_type or item.get("promotion_label") else 1
        resolution_text = f"{item.get('resolution') or ''} {' '.join(item.get('labels') or [])}".lower()
        four_k = 1 if any(value in resolution_text for value in ("2160", "4k", "uhd")) else 0
        size_gb = float(item.get("size_bytes") or 0) / (1024 ** 3)
        in_size_range = 1 if 15 <= size_gb <= 60 else 0
        size_distance = -abs(size_gb - 35) if size_gb else -9999
        codec_text = f"{item.get('codec') or ''} {item.get('title') or ''}".lower()
        codec_rank = 1 if any(value in codec_text for value in ("x265", "h.265", "h265", "hevc")) else 0
        # Codec is deliberately a late tie-breaker: HEVC saves space, but never
        # outweighs an explicit quality preference, promotion, or swarm health.
        return (promo_rank, four_k, float(item.get("seeders") or 0), in_size_range, size_distance, codec_rank)

    if recommend:
        filtered.sort(key=recommendation_score, reverse=True)
    else:
        filtered.sort(key=lambda item: float(item.get("seeders") or 0), reverse=True)
    return filtered[:10], 1 if recommend and filtered else None


def mteam_default_recommendation(items: list[dict[str, Any]], recommended_index: int | None) -> str:
    if not recommended_index or recommended_index < 1 or recommended_index > len(items):
        return ""
    item = items[recommended_index - 1]
    try:
        seeders = int(float(item.get("seeders") or 0))
    except (TypeError, ValueError):
        seeders = 0
    facts = [
        str(item.get("promotion_label") or "普通"),
        str(item.get("resolution") or "-"),
        f"{seeders} 人做种",
        str(item.get("size") or "-"),
        str(item.get("codec") or "-") + " 编码",
    ]
    return f"推荐第 {recommended_index} 个：{'，'.join(facts)}，综合优惠、清晰度、体积与做种情况排序最佳。"


MTEAM_COUNTRY_TOKENS = ("中国大陆", "中国香港", "中国台湾", "中国", "美国", "英国", "日本", "韩国", "法国", "德国", "印度", "泰国", "加拿大", "澳大利亚", "西班牙", "意大利", "俄罗斯")
MTEAM_GENRE_TOKENS = ("剧情", "喜剧", "动作", "爱情", "科幻", "恐怖", "惊悚", "悬疑", "犯罪", "动画", "奇幻", "冒险", "纪录", "战争", "音乐", "家庭", "历史", "同性")


def _mteam_source_text(item: dict[str, Any]) -> str:
    return " ".join(str(item.get(key) or "") for key in ("title", "subtitle"))


def _mteam_compact_work(item: dict[str, Any]) -> str:
    subtitle = str(item.get("subtitle") or "").strip()
    first = re.split(r"[|¦]", subtitle, maxsplit=1)[0].strip() if subtitle else ""
    aliases = [part.strip() for part in re.split(r"\s*/\s*", first) if part.strip()]
    if aliases:
        return " / ".join(aliases[:2])
    return mteam_markdown_cell(item.get("title") or item.get("name"), 180)


def _mteam_rule_metadata(item: dict[str, Any]) -> dict[str, str]:
    source = _mteam_source_text(item)
    year_match = re.search(r"\b(?:19|20)\d{2}\b", source)
    country = next((token for token in MTEAM_COUNTRY_TOKENS if token in source), "")
    genres = [token for token in MTEAM_GENRE_TOKENS if token in source][:2]
    locator = " · ".join(part for part in (year_match.group(0) if year_match else "", country, "/".join(genres)) if part)
    version_parts = [str(item.get("resolution") or "").strip(), str(item.get("codec") or "").strip(), str(item.get("hdr") or "").strip(), str(item.get("group") or "").strip()]
    return {"work": _mteam_compact_work(item), "locator": locator or "-", "version": " · ".join(part for part in version_parts if part) or "-"}


def mteam_has_chinese_subtitles(item: dict[str, Any]) -> bool:
    text = " ".join(
        [
            str(item.get("title") or ""),
            str(item.get("subtitle") or ""),
            " ".join(str(label) for label in (item.get("labels") or [])),
        ]
    ).lower()
    return any(token in text for token in ("中字", "简中", "繁中", "中文", "chs", "cht", "chinese"))


def _mteam_fallback_row(item: dict[str, Any], index: int) -> dict[str, str]:
    compact = _mteam_rule_metadata(item)
    source = _mteam_source_text(item)
    year_match = re.search(r"\b(?:19|20)\d{2}\b", source)
    year = year_match.group(0) if year_match else "未知年份"
    chinese_info = compact["locator"] if compact["locator"] != "-" else "中文信息未标注"
    chinese_info = f"{chinese_info} · {'含中字' if mteam_has_chinese_subtitles(item) else '未标注中字'}"
    return {
        "index": str(index),
        "title": f"{compact['work']} {year}",
        "chinese_info": chinese_info,
        "quality": compact["version"],
        "size": str(item.get("size") or "-"),
        "seeders": str(int(item.get("seeders") or 0)),
        "promotion": str(item.get("promotion_label") or "普通"),
    }


def mteam_model_rows(items: list[dict[str, Any]], rows: list[dict[str, Any]] | None = None) -> dict[str, dict[str, str]]:
    model_rows = {int(row.get("index") or 0): row for row in (rows or []) if isinstance(row, dict)}
    output: dict[str, dict[str, str]] = {}
    required = ("title", "chinese_info", "quality", "size", "seeders", "promotion")
    for index, item in enumerate(items, 1):
        fallback = _mteam_fallback_row(item, index)
        candidate = model_rows.get(index) or {}
        valid = all(str(candidate.get(field) or "").strip() for field in required)
        title = str(candidate.get("title") or "").strip()
        chinese_info = str(candidate.get("chinese_info") or "").strip()
        quality = str(candidate.get("quality") or "").strip()
        if not re.search(r"\b(?:19|20)\d{2}\b", title):
            valid = False
        expected_subtitle_state = "含中字" if mteam_has_chinese_subtitles(item) else "未标注中字"
        if expected_subtitle_state not in chinese_info:
            valid = False
        if str(candidate.get("size") or "").strip() != fallback["size"]:
            valid = False
        if str(candidate.get("seeders") or "").strip() != fallback["seeders"]:
            valid = False
        if str(candidate.get("promotion") or "").strip() != fallback["promotion"]:
            valid = False
        if str(item.get("resolution") or "") and str(item.get("resolution") or "") not in quality:
            valid = False
        output[str(item.get("id") or "")] = ({
            "title": title,
            "chinese_info": chinese_info,
            "quality": quality,
            "size": fallback["size"],
            "seeders": fallback["seeders"],
            "promotion": fallback["promotion"],
        } if valid else fallback)
    return output


def enrich_mteam_recommendation(ai_adapter: DeepSeekChatAdapter, result: dict[str, Any]) -> dict[str, Any]:
    items = result.get("items") if isinstance(result.get("items"), list) else []
    recommended_index = result.get("recommended_index")
    enriched = dict(result)
    enriched["recommendation_note"] = mteam_default_recommendation(items, recommended_index)
    enriched_items = [dict(item, has_chinese_subtitles=mteam_has_chinese_subtitles(item)) for item in items]
    enriched["items"] = enriched_items
    enriched["presentation"] = mteam_model_rows(enriched_items)
    if not enriched["recommendation_note"]:
        return enriched
    try:
        ai_presentation = ai_adapter.describe_mteam_presentation(
            str(result.get("query") or ""),
            enriched_items,
            int(recommended_index or 0),
        )
        enriched["recommendation_note"] = ai_presentation["recommendation"]
        enriched["presentation"] = mteam_model_rows(enriched_items, ai_presentation.get("rows"))
    except (AIServiceError, ValueError, TypeError):
        # Ranking remains deterministic even when the optional wording pass fails.
        pass
    return enriched


def mteam_markdown_cell(value: Any, limit: int = 72) -> str:
    text = " ".join(str(value or "-").replace("|", "¦").replace("\n", " ").split())
    return text if len(text) <= limit else f"{text[:limit - 1]}…"


TMDB_TV_REQUEST_HINTS = ("动画", "動漫", "动漫", "番剧", "番劇", "剧集", "劇集", "电视剧", "電視劇", "第1季", "第一季", "第1集", "第一集")
TMDB_MOVIE_REQUEST_HINTS = ("电影", "電影", "影片")


def tmdb_filters_for_request(intent: dict[str, Any], message: str) -> dict[str, Any]:
    """Keep AI title lookup compatible with the discover page when the model guesses a medium."""
    filters = dict(intent.get("tmdb_filters") or {})
    text = str(message or "").lower()
    query = str(intent.get("query") or "").strip()
    if any(hint.lower() in text for hint in TMDB_TV_REQUEST_HINTS):
        filters["media_type"] = "tv"
    elif any(hint.lower() in text for hint in TMDB_MOVIE_REQUEST_HINTS):
        filters["media_type"] = "movie"
    elif query and str(filters.get("media_type") or "all").lower() in {"movie", "tv"}:
        # An unqualified title should search both types, exactly like Discover.
        filters["media_type"] = "all"
    return filters


def execute_mobile_agent_intent(db: Session, intent: dict[str, Any], request: WechatClawMessageRequest, user: User) -> dict[str, Any]:
    intent_type = str(intent.get("intent_type") or "general_chat")
    if intent_type == "tmdb_lookup":
        row = get_config(db, "tmdb")
        if not row or not row.enabled:
            raise HTTPException(status_code=409, detail="请先在设置中启用 TMDB。")
        lookup_filters = tmdb_filters_for_request(intent, request.message)
        try:
            items = TmdbAdapter(get_decrypted_config(db, "tmdb") or {}).lookup_media(str(intent.get("query") or ""), lookup_filters)
        except Exception as exc:
            # A remote lookup error is not a "no results" outcome. Return a
            # structured failure so the mobile template can tell the user exactly
            # which provider failed while the interaction diagnostics retain detail.
            return {
                "intent_type": intent_type,
                "state": "failed",
                "query": intent.get("query"),
                "filters": lookup_filters,
                "error": f"TMDB 查询失败：{exc}",
                "items": [],
                "count": 0,
            }
        return {"intent_type": intent_type, "query": intent.get("query"), "filters": lookup_filters, "items": items, "count": len(items)}
    if intent_type == "mteam_search":
        query = str(intent.get("query") or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail="M-Team 搜索需要影片或资源关键词。")
        try:
            raw_items = get_mteam_adapter_or_error(db).search_torrents(query)[:30]
        except MTeamApiError as exc:
            raise HTTPException(status_code=502, detail=f"M-Team 搜索失败：{exc}") from exc
        items, recommended_index = rank_mteam_search_items(raw_items, intent.get("mteam_filters") or {})
        return {"intent_type": intent_type, "query": query, "filters": intent.get("mteam_filters"), "items": items, "count": len(items), "recommended_index": recommended_index}
    if intent_type == "dashboard_query":
        return build_mobile_dashboard_result(db, request, intent.get("dashboard_sections") or [])
    raise HTTPException(status_code=400, detail="不支持的手机端工具意图。")


def build_mobile_dashboard_result(db: Session, request: WechatClawMessageRequest, sections: list[str]) -> dict[str, Any]:
    selected = list(dict.fromkeys(section for section in sections if section)) or ["overview"]
    result: dict[str, Any] = {"intent_type": "dashboard_query", "sections": selected}
    dashboard = None
    if any(section in {"overview", "mteam", "nas", "qb1", "qb2", "qb3", "downloads"} for section in selected):
        dashboard = build_dashboard_payload(db)
    if dashboard and "overview" in selected:
        result["overview"] = dashboard.get("overview") or {}
    if dashboard and "mteam" in selected:
        result["mteam"] = dashboard.get("mteam") or {}
    if dashboard and "nas" in selected:
        result["nas"] = dashboard.get("overview") or {}
    for downloader_id in ("qb1", "qb2", "qb3"):
        if downloader_id not in selected:
            continue
        if downloader_id == "qb2" and not has_wechat_claw_privacy_access(db, request):
            result["qb2_privacy_required"] = True
            continue
        summary = next((item for item in (dashboard or {}).get("qbs") or [] if item.get("id") == downloader_id), {})
        if downloader_id == "qb2":
            try:
                result[downloader_id] = {**summary, "tasks": get_qb_adapter_or_error(db, downloader_id).get_torrents(downloader_id)[:5]}
            except QbittorrentApiError as exc:
                result[downloader_id] = {**summary, "detail_error": str(exc)}
        else:
            result[downloader_id] = summary
    if "downloads" in selected:
        result["downloads"] = [{"id": item.get("id"), "active_downloads": item.get("active_downloads"), "active_uploads": item.get("active_uploads")} for item in (dashboard or {}).get("qbs") or []]
    if "stats" in selected:
        result["stats"] = stats(assistant_actor_for_wechat_claw(db), db)
    if "diagnostics" in selected:
        result["diagnostics"] = diagnostics_health(assistant_actor_for_wechat_claw(db), db)
    return result


def save_wechat_claw_last_poll(db: Session, payload: dict[str, Any], user_id: int | None = None) -> dict[str, Any]:
    safe_payload = {
        "success": bool(payload.get("success")),
        "stage": str(payload.get("stage") or "poll"),
        "raw_count": int(payload.get("raw_count") or 0),
        "parsed_count": int(payload.get("parsed_count") or 0),
        "handled_count": int(payload.get("handled_count") or 0),
        "reply_sent_count": int(payload.get("reply_sent_count") or 0),
        "pending_count": int(payload.get("pending_count") or 0),
        "message": trim_wechat_claw_text(payload.get("message"), 360),
        "updated_at": datetime.utcnow().isoformat(),
    }
    key = wechat_claw_setting_key(WECHAT_CLAW_LAST_POLL_KEY, user_id)
    row = db.query(Setting).filter(Setting.key == key).one_or_none()
    if row is None:
        row = Setting(key=key, value=safe_payload)
        db.add(row)
    else:
        row.value = safe_payload
        row.updated_at = datetime.utcnow()
    db.commit()
    return safe_payload


def refresh_wechat_claw_login_state(db: Session) -> dict[str, Any]:
    config = get_decrypted_config(db, "wechat_claw") or {}
    state = get_wechat_claw_ilink_state(db)
    qrcode = state.get("qrcode") if isinstance(state.get("qrcode"), dict) else {}
    if not qrcode.get("qrcode") or state.get("bot_token"):
        return state
    try:
        adapter = WechatClawAdapter(wechat_claw_config_with_state(config, state))
        result = adapter.get_qrcode_status(str(qrcode.get("qrcode")))
        updated_qrcode = dict(qrcode)
        updated_qrcode["status"] = result.get("status") or updated_qrcode.get("status") or "waiting"
        updated_qrcode["updated_at"] = int(datetime.utcnow().timestamp())
        if result.get("qrcode_url"):
            updated_qrcode["qrcode_url"] = result.get("qrcode_url")
        update_payload: dict[str, Any] = {"qrcode": updated_qrcode}
        if result.get("token"):
            update_payload.update(
                {
                    "bot_token": result.get("token"),
                    "account_id": result.get("account_id"),
                    "sync_buf": "",
                    "base_url": (result.get("base_url") or adapter.base_url).rstrip("/"),
                }
            )
        return update_wechat_claw_ilink_state(db, **update_payload)
    except Exception as exc:
        logger.warning("刷新 WeChat claw iLink 扫码状态失败：%s", exc)
        return state


def clear_wechat_claw_ilink_state(db: Session) -> dict[str, Any]:
    return save_wechat_claw_ilink_state(db, {"qrcode": {}, "known_targets": {}})


def clear_wechat_claw_session(db: Session, qrcode_status: str | None = None) -> dict[str, Any]:
    state = get_wechat_claw_ilink_state(db)
    qrcode = state.get("qrcode") if isinstance(state.get("qrcode"), dict) else {}
    if qrcode_status and qrcode:
        qrcode = {**qrcode, "status": qrcode_status, "updated_at": int(datetime.utcnow().timestamp())}
    return update_wechat_claw_ilink_state(
        db,
        bot_token="",
        account_id="",
        sync_buf="",
        known_targets={},
        pending_messages=[],
        qrcode=qrcode,
    )


def is_wechat_claw_session_timeout(message: Any) -> bool:
    text = str(message or "").strip().lower()
    return "session timeout" in text or "session expired" in text or "errcode=-14" in text or "ret=-14" in text


def wechat_claw_pending_message_id(item: dict[str, Any]) -> str:
    message_id = str(item.get("message_id") or item.get("conversation_id") or "").strip()
    if message_id:
        return f"message:{message_id}"
    identity = json.dumps(
        {
            "user_id": str(item.get("user_id") or "").strip(),
            "context_token": str(item.get("context_token") or "").strip(),
            "text": str(item.get("text") or item.get("message") or "").strip(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"hash:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def get_wechat_claw_pending_messages(state: dict[str, Any]) -> list[dict[str, Any]]:
    values = state.get("pending_messages") if isinstance(state.get("pending_messages"), list) else []
    pending: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        pending_id = str(item.get("id") or "").strip()
        if not pending_id:
            continue
        pending.append(dict(item, id=pending_id))
    return pending[:WECHAT_CLAW_PENDING_LIMIT]


def save_wechat_claw_pending_messages(db: Session, pending: list[dict[str, Any]], sync_buf: str | None = None) -> list[dict[str, Any]]:
    values = pending[:WECHAT_CLAW_PENDING_LIMIT]
    payload: dict[str, Any] = {"pending_messages": values}
    if sync_buf is not None:
        payload["sync_buf"] = sync_buf
    update_wechat_claw_ilink_state(db, **payload)
    return values


def queue_wechat_claw_messages(db: Session, messages: list[dict[str, Any]], sync_buf: str | None) -> int:
    state = get_wechat_claw_ilink_state(db)
    pending = get_wechat_claw_pending_messages(state)
    known_ids = {str(item.get("id")) for item in pending}
    added = 0
    for item in messages:
        message = str(item.get("text") or "").strip()
        user_id = str(item.get("user_id") or "").strip()
        context_token = str(item.get("context_token") or "").strip()
        if not message or not user_id or not context_token:
            continue
        pending_id = wechat_claw_pending_message_id(item)
        if pending_id in known_ids:
            continue
        pending.append(
            {
                "id": pending_id,
                "message": message,
                "user_id": user_id,
                "conversation_id": str(item.get("message_id") or "").strip(),
                "context_token": context_token,
                "received_at": datetime.utcnow().isoformat(),
                "attempts": 0,
            }
        )
        known_ids.add(pending_id)
        added += 1
    save_wechat_claw_pending_messages(db, pending, str(sync_buf or ""))
    return added


def update_wechat_claw_pending_message(db: Session, pending_id: str, **changes: Any) -> None:
    state = get_wechat_claw_ilink_state(db)
    pending = get_wechat_claw_pending_messages(state)
    for index, item in enumerate(pending):
        if item.get("id") == pending_id:
            pending[index] = {**item, **changes}
            break
    save_wechat_claw_pending_messages(db, pending)


def remove_wechat_claw_pending_message(db: Session, pending_id: str) -> None:
    state = get_wechat_claw_ilink_state(db)
    pending = [item for item in get_wechat_claw_pending_messages(state) if item.get("id") != pending_id]
    save_wechat_claw_pending_messages(db, pending)


def wechat_claw_retry_after_seconds(pending: list[dict[str, Any]]) -> float | None:
    now = datetime.utcnow().timestamp()
    retry_times = [float(item.get("next_retry_at") or 0) for item in pending if item.get("next_retry_at")]
    if not retry_times:
        return None
    return max(0.0, min(retry_times) - now)


def retry_wechat_claw_pending_message(db: Session, pending: dict[str, Any], error: str) -> float:
    attempts = int(pending.get("attempts") or 0) + 1
    delay_seconds = min(60, 2 ** min(attempts, 6))
    update_wechat_claw_pending_message(
        db,
        str(pending["id"]),
        attempts=attempts,
        last_error=trim_wechat_claw_text(error, 240),
        next_retry_at=int(datetime.utcnow().timestamp()) + delay_seconds,
    )
    return float(delay_seconds)


def process_wechat_claw_pending_messages(db: Session, adapter: WechatClawAdapter) -> dict[str, Any]:
    state = get_wechat_claw_ilink_state(db)
    pending = get_wechat_claw_pending_messages(state)
    now = datetime.utcnow().timestamp()
    handled: list[dict[str, Any]] = []
    reply_sent_count = 0

    for item in pending:
        next_retry_at = float(item.get("next_retry_at") or 0)
        if next_retry_at > now:
            continue
        pending_id = str(item["id"])
        telemetry = new_wechat_claw_interaction_telemetry()
        interaction_trace_id = str(item.get("interaction_trace_id") or "")
        interaction_status = "completed"
        try:
            reply = str(item.get("reply") or "").strip()
            if not reply:
                response = handle_wechat_claw_text(
                    db,
                    WechatClawMessageRequest(
                        message=str(item.get("message") or ""),
                        user_id=str(item.get("user_id") or ""),
                        conversation_id=str(item.get("conversation_id") or ""),
                        context_token=str(item.get("context_token") or ""),
                    ),
                    telemetry,
                )
                reply = str(response.get("reply") or "").strip()
                if not reply:
                    raise RuntimeError("empty AI reply")
                interaction = response.get("interaction") if isinstance(response.get("interaction"), dict) else {}
                interaction_trace_id = str(interaction.get("trace_id") or "")
                interaction_status = str(interaction.get("status") or "completed")
                # Persist the generated reply before delivery. A transient send failure
                # must retry the same answer rather than execute the user intent twice.
                update_wechat_claw_pending_message(
                    db,
                    pending_id,
                    reply=reply,
                    interaction_trace_id=interaction_trace_id,
                    handled_at=datetime.utcnow().isoformat(),
                )
            delivery_started = time.perf_counter()
            delivery = adapter.send_text(str(item.get("user_id") or ""), reply, str(item.get("context_token") or ""))
            record_wechat_claw_stage(
                telemetry,
                "final_reply_send",
                delivery_started,
                status="success" if delivery.get("sent") else "failed",
                error=None if delivery.get("sent") else delivery.get("message") or delivery.get("reason"),
            )
            if not delivery.get("sent"):
                reason = str(delivery.get("message") or delivery.get("reason") or "send rejected")
                update_wechat_claw_interaction_delivery(db, interaction_trace_id, telemetry, status="delivery_failed", error=reason)
                retry_wechat_claw_pending_message(db, item, reason)
                handled.append({"user_id": item.get("user_id"), "message_id": item.get("conversation_id"), "reply_sent": False, "error": reason})
                continue
        except HTTPException as exc:
            failure = str(exc.detail)
            if interaction_trace_id:
                update_wechat_claw_interaction_delivery(db, interaction_trace_id, telemetry, status="failed", error=failure)
            else:
                failed_interaction = record_wechat_claw_interaction(
                    db,
                    WechatClawMessageRequest(message=str(item.get("message") or ""), user_id=str(item.get("user_id") or ""), conversation_id=str(item.get("conversation_id") or ""), context_token=str(item.get("context_token") or "")),
                    {"action": "interaction_failed", "intent_type": "interaction_failed"},
                    {"stage": (telemetry.get("stages") or [{}])[-1].get("stage", "unknown")},
                    "",
                    telemetry,
                    status="failed",
                )
                update_wechat_claw_pending_message(db, pending_id, interaction_trace_id=failed_interaction.get("trace_id"))
            retry_wechat_claw_pending_message(db, item, str(exc.detail))
            handled.append({"user_id": item.get("user_id"), "message_id": item.get("conversation_id"), "reply_sent": False, "error": str(exc.detail)})
            continue
        except Exception as exc:
            logger.exception("WeChat claw message handling failed: user_id=%s", item.get("user_id"))
            error = str(exc)[:240]
            if interaction_trace_id:
                update_wechat_claw_interaction_delivery(db, interaction_trace_id, telemetry, status="failed", error=error)
            else:
                failed_interaction = record_wechat_claw_interaction(
                    db,
                    WechatClawMessageRequest(message=str(item.get("message") or ""), user_id=str(item.get("user_id") or ""), conversation_id=str(item.get("conversation_id") or ""), context_token=str(item.get("context_token") or "")),
                    {"action": "interaction_failed", "intent_type": "interaction_failed"},
                    {"stage": (telemetry.get("stages") or [{}])[-1].get("stage", "unknown")},
                    "",
                    telemetry,
                    status="failed",
                )
                update_wechat_claw_pending_message(db, pending_id, interaction_trace_id=failed_interaction.get("trace_id"))
            retry_wechat_claw_pending_message(db, item, error)
            handled.append({"user_id": item.get("user_id"), "message_id": item.get("conversation_id"), "reply_sent": False, "error": error})
            continue

        remove_wechat_claw_pending_message(db, pending_id)
        update_wechat_claw_interaction_delivery(db, interaction_trace_id, telemetry, status=interaction_status)
        reply_sent_count += 1
        handled.append({"user_id": item.get("user_id"), "message_id": item.get("conversation_id"), "reply_sent": True})

    current_pending = get_wechat_claw_pending_messages(get_wechat_claw_ilink_state(db))
    return {
        "handled": handled,
        "reply_sent_count": reply_sent_count,
        "pending_count": len(current_pending),
        "retry_after_seconds": wechat_claw_retry_after_seconds(current_pending),
    }


def wechat_claw_config_with_state(config: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    merged = dict(config)
    merged.update(
        {
            "bot_token": state.get("bot_token"),
            "account_id": state.get("account_id"),
            "sync_buf": state.get("sync_buf"),
            "known_targets": state.get("known_targets") or {},
            "base_url": state.get("base_url") or config.get("base_url"),
        }
    )
    return merged


def trim_wechat_claw_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit - 1]}…"


def wechat_claw_qr_payload(config: dict[str, Any]) -> dict[str, Any]:
    try:
        timeout = int(config.get("poll_timeout") or config.get("timeout") or 25)
    except (TypeError, ValueError):
        timeout = 25
    qrcode = config.get("qrcode") if isinstance(config.get("qrcode"), dict) else {}
    return {
        "type": "pt-media-hub.wechat-claw",
        "version": 1,
        "name": "PT Media Hub",
        "mode": str(config.get("mode") or "ilink"),
        "base_url": str(config.get("base_url") or "https://ilinkai.weixin.qq.com").rstrip("/"),
        "qrcode": qrcode.get("qrcode"),
        "qrcode_url": qrcode.get("qrcode_url"),
        "qrcode_status": qrcode.get("status"),
        "default_downloader_id": str(config.get("default_downloader_id") or "all"),
        "default_target": str(config.get("default_target") or ""),
        "timeout": timeout,
    }


def build_wechat_claw_setup(db: Session, binding_id: int | None = None) -> dict[str, Any]:
    row = get_config(db, "wechat_claw")
    config = get_decrypted_config(db, "wechat_claw") or {}
    bindings = ensure_wechat_claw_bindings(db)
    binding = get_wechat_claw_binding_or_error(db, binding_id) if binding_id is not None else (bindings[0] if bindings else None)
    state = get_wechat_claw_ilink_state(db, binding.id if binding else None)
    qrcode = state.get("qrcode") if isinstance(state.get("qrcode"), dict) else {}
    qr_payload = wechat_claw_qr_payload({**config, **state, "qrcode": qrcode})
    return {
        "configured": bool(row and row.encrypted_payload),
        "enabled": bool(row and row.enabled),
        "binding": serialize_wechat_claw_binding(binding) if binding else None,
        "bindings": [serialize_wechat_claw_binding(item) for item in bindings],
        "mode": str(config.get("mode") or "ilink"),
        "connected": bool(state.get("bot_token")),
        "account_id": state.get("account_id"),
        "base_url": state.get("base_url") or config.get("base_url") or "https://ilinkai.weixin.qq.com",
        "config_version": row.config_version if row else 0,
        "last_test_result": row.last_test_result if row else None,
        "qr_payload": qr_payload,
        "qr_text": qrcode.get("qrcode_url") or json.dumps(qr_payload, ensure_ascii=False, separators=(",", ":")),
        "qrcode": qrcode,
        "known_targets": state.get("known_targets") or {},
        "mobile_sections": WECHAT_CLAW_MOBILE_SECTIONS,
        "capabilities": WECHAT_CLAW_CAPABILITIES,
        "recent_interactions": get_wechat_claw_recent_interactions(db, binding.id if binding else None),
        "last_poll": get_wechat_claw_last_poll(db, binding.id if binding else None),
    }


def record_wechat_claw_interaction(
    db: Session,
    request: WechatClawMessageRequest,
    intent: dict[str, Any],
    result: dict[str, Any],
    reply: str,
    telemetry: dict[str, Any] | None = None,
    status: str = "completed",
) -> dict[str, Any]:
    if _WECHAT_CLAW_ACTIVE_USER_ID.get() is None:
        bindings = ensure_wechat_claw_bindings(db)
        if bindings:
            scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(bindings[0].id)
            try:
                return record_wechat_claw_interaction(db, request, intent, result, reply, telemetry, status)
            finally:
                _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)
    action = str(intent.get("action") or "")
    target = str(intent.get("target") or intent.get("downloader_id") or result.get("target") or "")
    diagnostics = serialize_wechat_claw_interaction_telemetry(telemetry)
    item = {
        "user_id": trim_wechat_claw_text(request.user_id or "unknown", 120),
        "conversation_id": trim_wechat_claw_text(request.conversation_id, 120),
        "message": trim_wechat_claw_text(redact_wechat_claw_message(request.message), 240),
        "reply": trim_wechat_claw_text(reply, 360),
        "action": action,
        "target": target,
        "status": status,
        **diagnostics,
        "created_at": datetime.utcnow().isoformat(),
    }
    current = get_wechat_claw_recent_interactions(db)
    row = db.query(Setting).filter(Setting.key == wechat_claw_setting_key(WECHAT_CLAW_INTERACTIONS_KEY)).one_or_none()
    value = {"items": [item, *current][:WECHAT_CLAW_INTERACTION_LIMIT]}
    if row is None:
        row = Setting(key=WECHAT_CLAW_INTERACTIONS_KEY, value=value)
        db.add(row)
    else:
        row.value = value
        row.updated_at = datetime.utcnow()
    db.commit()
    state = get_wechat_claw_ilink_state(db)
    known_targets = state.get("known_targets") if isinstance(state.get("known_targets"), dict) else {}
    if item["user_id"] and item["user_id"] != "unknown":
        known_targets[item["user_id"]] = {
            "username": item["user_id"],
            "last_active": int(datetime.utcnow().timestamp()),
            "context_token": request.context_token,
        }
        update_wechat_claw_ilink_state(db, known_targets=known_targets)
    return item


def update_wechat_claw_interaction_delivery(
    db: Session,
    trace_id_value: str,
    telemetry: dict[str, Any],
    *,
    status: str,
    error: Any = None,
) -> None:
    if not trace_id_value:
        return
    row = db.query(Setting).filter(Setting.key == wechat_claw_setting_key(WECHAT_CLAW_INTERACTIONS_KEY)).one_or_none()
    value = row.value if row and isinstance(row.value, dict) else {}
    items = value.get("items") if isinstance(value.get("items"), list) else []
    diagnostics = serialize_wechat_claw_interaction_telemetry(telemetry)
    for item in items:
        if not isinstance(item, dict) or item.get("trace_id") != trace_id_value:
            continue
        existing_stages = item.get("stages") if isinstance(item.get("stages"), list) else []
        incoming_stages = diagnostics.get("stages") if isinstance(diagnostics.get("stages"), list) else []
        existing_names = [str(stage.get("stage") or "") for stage in existing_stages if isinstance(stage, dict)]
        incoming_names = [str(stage.get("stage") or "") for stage in incoming_stages if isinstance(stage, dict)]
        if existing_names and incoming_names[: len(existing_names)] != existing_names:
            diagnostics["stages"] = [*existing_stages, *incoming_stages][-12:]
        item.update({"status": status, **diagnostics})
        if error:
            item["error"] = trim_wechat_claw_text(redact_payload(str(error)), 240)
        break
    else:
        return
    row.value = {"items": items[:WECHAT_CLAW_INTERACTION_LIMIT]}
    row.updated_at = datetime.utcnow()
    db.commit()


def push_wechat_claw_notification(db: Session, notification: Notification, action: str) -> dict[str, Any]:
    # App-originated download events have no active chat scope. Fan them out to
    # every independently bound member instead of the legacy administrator bot.
    if _WECHAT_CLAW_ACTIVE_USER_ID.get() is None:
        binding_ids = list_wechat_claw_binding_user_ids(db)
        if binding_ids:
            deliveries: dict[str, dict[str, Any]] = {}
            for binding_id in binding_ids:
                scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(binding_id)
                try:
                    deliveries[str(binding_id)] = push_wechat_claw_notification(db, notification, action)
                finally:
                    _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)
            return {"sent": any(item.get("sent") for item in deliveries.values()), "deliveries": deliveries}
    preferences = get_notification_preferences(db)
    if not preferences.get("wechat_claw_push", True):
        return {"sent": False, "reason": "disabled_by_preferences"}
    binding_id = _WECHAT_CLAW_ACTIVE_USER_ID.get()
    role_name = ""
    if binding_id is not None:
        binding = get_wechat_claw_binding_or_error(db, binding_id)
        if not binding.enabled:
            return {"sent": False, "reason": "binding_disabled"}
        if not normalized_wechat_claw_binding_preferences(binding.notification_preferences).get(action, False):
            return {"sent": False, "reason": "disabled_by_binding_preferences"}
        role_name = binding.role_name.strip()
    row = get_config(db, "wechat_claw")
    if not row or not row.enabled or not row.encrypted_payload:
        return {"sent": False, "reason": "wechat_claw_not_enabled"}
    try:
        config = get_decrypted_config(db, "wechat_claw") or {}
        state = get_wechat_claw_ilink_state(db)
        adapter = WechatClawAdapter(wechat_claw_config_with_state(config, state))
        return adapter.send_message(
            {
                "type": action,
                "title": f"【{role_name}】{notification.title}" if role_name else notification.title,
                "message": notification.message,
                "level": notification.level,
                "source": notification.source,
                "notification_id": notification.id,
                "created_at": notification.created_at.isoformat() if notification.created_at else None,
            }
        )
    except (WechatClawConfigError, WechatClawApiError) as exc:
        return {"sent": False, "reason": str(exc), "http_status": getattr(exc, "http_status", None)}


def format_assistant_result_v2(intent: dict[str, Any], result: dict[str, Any]) -> str:
    action = intent.get("action")
    if action == "resource_search":
        items = result.get("items") or []
        if not items:
            return "【资源查询】\n没有找到匹配的 M-Team 资源。"
        lines = ["【资源查询】", f"找到 {len(items)} 个资源。", "", "结果"]
        for index, item in enumerate(items[:5], 1):
            lines.append(f"- {index}. {item.get('title') or item.get('name') or '-'} / {item.get('resolution') or '-'} / {item.get('size') or '-'} / 做种 {item.get('seeders', 0)} / ID {item.get('id') or '-'}")
        if result.get("mobile_selection"):
            lines.extend(["", "选择下载", "回复“下载 1”即可将对应资源推送到默认下载器。"])
        return "\n".join(lines)
    if action == "mobile_download":
        if result.get("selection_expired"):
            return "【下载选择已过期】\n请重新搜索资源，然后回复“下载 1”选择需要的条目。"
        if result.get("selection_missing"):
            return "【未找到可下载资源】\n请先搜索资源，再回复“下载 1”选择资源。"
        if result.get("accepted"):
            return f"【已添加下载】\n{result.get('title') or '已选资源'} 已推送到 {result.get('downloader_id')}。"
        return f"【添加下载失败】\n{result.get('message') or '请检查默认下载器和 M-Team 配置后重试。'}"
    if action in {"download_started", "download_completed"}:
        label = "下载开始" if action == "download_started" else "下载完成"
        notification = result.get("notification") or {}
        if result.get("notification_skipped"):
            return f"【{label}】\n已按通知偏好跳过通知中心记录。"
        lines = [f"【{label}】", f"已记录通知：{notification.get('title', label)}", f"通知 ID：{notification.get('id', '-')}"]
        push = result.get("wechat_claw_push") or {}
        if push:
            lines.append(f"WeChat claw：{'已推送' if push.get('sent') else push.get('reason', '未推送')}")
        return "\n".join(lines)
    if action == "status_query":
        if result.get("target") == "stats":
            series = result.get("series") or []
            return f"【统计状态】\n可用区间：{', '.join(result.get('ranges') or [])}\n最近样本：{len(series)} 条"
        if result.get("target") == "diagnostics":
            modules = result.get("modules") or []
            failed = [item for item in modules if item.get("status") == "failed"]
            return f"【诊断状态】\n模块数：{len(modules)}\n异常：{len(failed)}"
        if result.get("target") == "discover":
            return f"【发现页状态】\n已配置：{'是' if result.get('configured') else '否'}\n已启用：{'是' if result.get('enabled') else '否'}\n{result.get('message') or ''}".strip()
        if result.get("target") == "mteam":
            data = result.get("mteam") or result
            return f"【M-Team 状态】\n等级：{data.get('user_level', '-')}\n分享率：{data.get('ratio', 0)}\n做种：{data.get('seed_count', 0)}"
        if result.get("target") in {"qb", "downloads"}:
            qbs = result.get("qbs") or []
            if qbs:
                lines = ["【qB 状态】"]
                lines.extend(f"- {item.get('name') or item.get('id')}：下载 {bytes_label(item.get('download_speed') or 0)}/s，上传 {bytes_label(item.get('upload_speed') or 0)}/s，任务 {item.get('active_downloads', 0)}" for item in qbs)
                return "\n".join(lines)
        overview = result.get("overview") or {}
        return f"【总览状态】\n下载：{bytes_label(overview.get('total_download_speed') or 0)}/s\n上传：{bytes_label(overview.get('total_upload_speed') or 0)}/s\n活动下载：{overview.get('download_tasks', 0)}\nNAS：{overview.get('nas_space_label') or '-'}"
    return "【处理结果】\n请求已处理。"


def format_mobile_agent_reply(intent: dict[str, Any], result: dict[str, Any]) -> str:
    intent_type = str(intent.get("intent_type") or intent.get("action") or "")
    if intent_type == "tmdb_lookup":
        if result.get("state") == "failed":
            return f"【TMDB 查询失败】\n{trim_wechat_claw_text(result.get('error'), 300)}\n请在设置中测试 TMDB 连通性后重试。"
        items = result.get("items") or []
        if not items:
            return "【TMDB 影视查询】\n没有找到符合条件的作品。可以放宽地区、评分或年份条件后重试。"
        lines = ["【TMDB 影视查询】", f"找到 {len(items)} 部符合条件的作品。"]
        for index, item in enumerate(items[:5], 1):
            genres = " / ".join(item.get("genres") or []) or "-"
            overview = str(item.get("overview") or "暂无简介").replace("\n", " ")[:86]
            lines.extend(["", f"#{index} {item.get('title') or '-'}", f"{item.get('media_type') == 'tv' and '电视剧' or '电影'} · {item.get('year') or '-'} · 评分 {item.get('rating') or '-'}", f"类型：{genres}", f"简介：{overview}"])
        return "\n".join(lines)
    if intent_type == "mteam_search":
        if result.get("state") == "selection_expired":
            return "【M-Team 完整结果已过期】\n请重新搜索资源后，再回复“查看全部”。"
        items = result.get("items") or []
        if not items:
            return "【M-Team 资源搜索】\n没有找到匹配资源。建议换用片名、英文名或降低筛选条件。"
        total_count = int(result.get("count") or len(items))
        display_limit = total_count if result.get("show_all") else min(MTEAM_INITIAL_DISPLAY_LIMIT, total_count)
        displayed = items[:display_limit]
        presentation_map = result.get("presentation") if isinstance(result.get("presentation"), dict) else {}
        if result.get("show_all"):
            lines = [f"在馒头站找到 **{total_count} 个资源**，以下为完整列表："]
        elif total_count > display_limit:
            lines = [f"在馒头站找到 **{total_count} 个资源**，以下展示综合排名前 **{display_limit}** 个。回复“查看全部”可获取完整列表："]
        else:
            lines = [f"在馒头站找到 **{total_count} 个资源**，以下为全部结果："]
        lines.extend(["", "| # | 标题 | 中文信息 | 清晰度 | 大小 | 做种 | 促销 |", "|---|------|----------|--------|------|------|------|"])
        for index, item in enumerate(displayed, 1):
            promotion = item.get("promotion_label") or "普通"
            presentation = item.get("presentation") if isinstance(item.get("presentation"), dict) else presentation_map.get(str(item.get("id") or ""), _mteam_fallback_row(item, index))
            title = mteam_markdown_cell(presentation.get("title") or _mteam_fallback_row(item, index)["title"], 180)
            chinese_info = mteam_markdown_cell(presentation.get("chinese_info") or _mteam_fallback_row(item, index)["chinese_info"], 120)
            quality = mteam_markdown_cell(presentation.get("quality") or _mteam_fallback_row(item, index)["quality"], 72)
            size = mteam_markdown_cell(presentation.get("size") or item.get("size"), 16)
            seeders = mteam_markdown_cell(presentation.get("seeders") or item.get("seeders"), 12)
            promotion = presentation.get("promotion") or promotion
            if index == result.get("recommended_index"):
                size = f"**{size}**"
                seeders = f"**{seeders}**"
            lines.append(f"| {index} | {title} | {chinese_info} | {quality} | {size} | {seeders} | {mteam_markdown_cell(promotion, 18)} |")
        if result.get("recommended_index"):
            lines.extend(["", f"**{result.get('recommendation_note') or mteam_default_recommendation(items, result.get('recommended_index'))}**"])
        if result.get("show_all"):
            lines.extend(["", "需要下载哪个资源？回复“第 X 个”或“下载推荐的那个”，我会先展示下载摘要，再等待确认。"])
        else:
            lines.extend(["", "需要下载哪个资源？回复“第 X 个”或“下载推荐的那个”；想看完整列表可回复“查看全部”。"])
        return "\n".join(lines)
    if intent_type == "dashboard_query":
        lines = ["【仪表盘查询】"]
        overview = result.get("overview") or {}
        if overview:
            lines.extend(["", "核心总览", f"下载 {bytes_label(overview.get('total_download_speed') or 0)}/s · 上传 {bytes_label(overview.get('total_upload_speed') or 0)}/s", f"活跃下载 {overview.get('download_tasks', 0)} · NAS {overview.get('nas_space_label') or overview.get('nas_free_space_label') or '-'}"])
        mteam = result.get("mteam") or {}
        if mteam:
            lines.extend(["", "M-Team", f"等级 {mteam.get('user_level') or '-'} · 分享率 {mteam.get('ratio') or 0} · 做种 {mteam.get('seed_count') or 0}"])
        for downloader_id in ("qb1", "qb2", "qb3"):
            qb = result.get(downloader_id) or {}
            if qb:
                lines.extend(["", downloader_label(downloader_id), f"下载 {bytes_label(qb.get('download_speed') or 0)}/s · 上传 {bytes_label(qb.get('upload_speed') or 0)}/s · 任务 {qb.get('active_downloads') or 0}"])
                if downloader_id == "qb2" and isinstance(qb.get("tasks"), list):
                    lines.extend(f"- {item.get('name') or '-'} · {item.get('progress', 0)}%" for item in qb["tasks"][:5])
        if result.get("qb2_privacy_required"):
            lines.extend(["", "qB2 隐私详情", "请先发送“验证管理员密码：你的密码”，验证成功后 15 分钟内可查询详情。"])
        if result.get("stats"):
            lines.extend(["", "统计", f"可用区间：{', '.join(result['stats'].get('ranges') or [])}"])
        if result.get("diagnostics"):
            failed = [item for item in result["diagnostics"].get("modules") or [] if item.get("status") == "failed"]
            lines.extend(["", "诊断", f"异常模块：{len(failed)}"])
        return "\n".join(lines)
    if intent_type == "download_selected":
        state = str(result.get("state") or "")
        candidate = result.get("candidate") or {}
        title = candidate.get("title") or result.get("title") or "该资源"
        if state == "awaiting_confirmation":
            return f"【下载确认】\n已选择：{title}\n{candidate.get('resolution') or '-'} · {candidate.get('size') or '-'}\n回复“确认”“就它”或“开始下载”即可推送到默认下载器。"
        if state == "accepted":
            return f"【已添加下载】\n{title}\n已推送到 {result.get('downloader_id')}。"
        if state == "selection_missing":
            return "【未找到待选资源】\n请先搜索 M-Team 资源，再选择编号或推荐资源。"
        return f"【添加下载失败】\n{result.get('message') or '请检查默认下载器、M-Team 和 qB 配置。'}"
    return "【影视中枢】\n请告诉我想查的电影、资源、仪表盘信息，或聊聊你的观影计划。"


def mobile_agent_history(db: Session, request: WechatClawMessageRequest) -> list[dict[str, str]]:
    user_id = str(request.user_id or "").strip()
    history: list[dict[str, str]] = []
    for item in reversed(get_wechat_claw_recent_interactions(db)):
        if user_id and str(item.get("user_id") or "") != user_id:
            continue
        history.extend([{"role": "user", "content": str(item.get("message") or "")}, {"role": "assistant", "content": str(item.get("reply") or "")}])
    return history[-10:]


def infer_mobile_selection(message: str) -> int:
    normalized = re.sub(r"\s+", "", str(message or "").lower())
    if "推荐" in normalized:
        return -1
    for phrase, index in WECHAT_CLAW_SELECTION_WORDS.items():
        if phrase in normalized:
            return index
    command_match = WECHAT_CLAW_DOWNLOAD_SELECTION_RE.match(normalized)
    if command_match:
        return int(command_match.group(1))
    match = re.search(r"(?:第)?(\d+)(?:个|部|项)?", normalized)
    return int(match.group(1)) if match and 1 <= int(match.group(1)) <= WECHAT_CLAW_MOBILE_SELECTION_LIMIT else 0


def attach_optional_assistant_notification(db: Session, intent: dict[str, Any], result: dict[str, Any], title: str, level: str = "info") -> dict[str, Any]:
    action = str(intent.get("action") or "")
    if not get_notification_preferences(db).get(action, False):
        return result
    detail = format_assistant_result_v2(intent, result)
    row = Notification(title=title[:200], message=detail, level=level, source="ai")
    db.add(row)
    db.commit()
    db.refresh(row)
    updated = dict(result)
    updated["notification"] = {
        "id": row.id,
        "title": row.title,
        "message": row.message,
        "level": row.level,
        "source": row.source,
        "created_at": row.created_at.isoformat(),
    }
    updated["wechat_claw_push"] = push_wechat_claw_notification(db, row, action)
    return updated


def execute_assistant_intent_v2(db: Session, intent: dict[str, Any], message: str, user: User) -> dict[str, Any]:
    action = str(intent.get("action") or "")
    if action == "status_query":
        target = str(intent.get("target") or "dashboard")
        if target == "stats":
            result = {"action": action, "target": target, **stats(user, db)}
            return attach_optional_assistant_notification(db, intent, result, "状态查询：统计")
        if target == "diagnostics":
            if user.role != "admin":
                raise HTTPException(status_code=403, detail="诊断状态需要管理员权限。")
            result = {"action": action, "target": target, **diagnostics_health(user, db)}
            return attach_optional_assistant_notification(db, intent, result, "状态查询：诊断")
        if target == "discover":
            row = get_config(db, "tmdb")
            result = {
                "action": action,
                "target": target,
                "configured": bool(row and row.redacted_summary),
                "enabled": bool(row and row.enabled),
                "last_test_result": row.last_test_result if row else None,
                "message": "发现页使用 TMDB 配置获取趋势、热门和筛选内容。",
            }
            return attach_optional_assistant_notification(db, intent, result, "状态查询：发现页")
        result = execute_assistant_intent(db, intent, message, user)
        return attach_optional_assistant_notification(db, intent, result, "状态查询")

    if action == "resource_search":
        result = execute_assistant_intent(db, intent, message, user)
        query = str(intent.get("query") or "").strip()
        title = f"资源查询：{query}" if query else "资源查询"
        return attach_optional_assistant_notification(db, intent, result, title)

    if action in {"download_started", "download_completed"}:
        title = "下载已开始" if action == "download_started" else "下载已完成"
        detail = str(intent.get("message") or message or "").strip() or title
        if not get_notification_preferences(db).get(action, True):
            return {
                "action": action,
                "notification_skipped": True,
                "reason": "disabled_by_preferences",
                "title": title,
                "message": detail,
            }
        row = Notification(title=title, message=detail, level="info" if action == "download_started" else "success", source="ai")
        db.add(row)
        db.commit()
        db.refresh(row)
        push_result = push_wechat_claw_notification(db, row, action)
        return {
            "action": action,
            "notification": {
                "id": row.id,
                "title": row.title,
                "message": row.message,
                "level": row.level,
                "source": row.source,
                "created_at": row.created_at.isoformat(),
            },
            "wechat_claw_push": push_result,
        }

    raise HTTPException(status_code=400, detail="不支持的 AI 指令。")


def empty_mteam_stats(message: str = "请先在设置中启用 M-Team。") -> dict[str, Any]:
    return {
        "user_level": "-",
        "upload_total": 0,
        "upload_delta_label": None,
        "download_total": 0,
        "download_delta_label": None,
        "bonus": 0,
        "bonus_delta_label": None,
        "ratio": 0,
        "ratio_delta_label": None,
        "seed_count": 0,
        "seed_count_delta_label": None,
        "seed_size": 0,
        "seed_size_delta_label": None,
        "joined_at": "-",
        "active_uploads": 0,
        "active_downloads": 0,
        "bonus_per_hour_label": "",
        "source": "M-Team 未启用",
        "updated_at": None,
        "traffic_history": [],
        "message": message,
    }


def get_mteam_adapter_or_error(db: Session) -> MTeamAdapter:
    row = get_config(db, "mteam")
    if not row or not row.enabled:
        raise HTTPException(status_code=409, detail="请先在设置中保存、测试并启用 M-Team。")
    try:
        return MTeamAdapter(get_decrypted_config(db, "mteam") or {})
    except MTeamConfigError as exc:
        raise HTTPException(status_code=409, detail="M-Team API Key 未配置。") from exc


def qb_placeholder_state(downloader_id: str, row: IntegrationConfig | None, message: str | None = None) -> dict[str, Any]:
    index = downloader_id.replace("qb", "")
    configured = bool(row and row.encrypted_payload)
    enabled = bool(row and row.enabled)
    name = (row.redacted_summary or {}).get("name") if row else None
    return qb_placeholder_state_from_meta(downloader_id, configured, enabled, name, message)


def qb_placeholder_state_from_meta(downloader_id: str, configured: bool, enabled: bool, name: str | None = None, message: str | None = None) -> dict[str, Any]:
    index = downloader_id.replace("qb", "")
    return {
        "id": downloader_id,
        "name": name or f"qB{index}",
        "online": False,
        "configured": configured,
        "enabled": enabled,
        "download_speed": 0,
        "upload_speed": 0,
        "active_downloads": 0,
        "active_uploads": 0,
        "paused": 0,
        "errors": 0,
        "free_space": 0,
        "total_space": 0,
        "source": "qB Web API 未启用",
        "updated_at": datetime.utcnow().isoformat(),
        "message": message or ("配置已保存，请在设置中测试并启用。" if configured else "请先在设置中配置 qB WebUI。"),
    }


def downloader_label(downloader_id: str) -> str:
    if downloader_id.lower().startswith("qb"):
        return f"qB{downloader_id[2:]}"
    return downloader_id


def get_qb_adapter_or_error(db: Session, downloader_id: str) -> QbittorrentWebAdapter:
    if downloader_id not in {"qb1", "qb2", "qb3"}:
        raise HTTPException(status_code=404, detail="未知下载器")
    row = get_config(db, downloader_id)
    if not row or not row.encrypted_payload:
        raise HTTPException(status_code=409, detail=f"{downloader_label(downloader_id)} 尚未配置")
    if not row.enabled:
        raise HTTPException(status_code=409, detail=f"{downloader_label(downloader_id)} 尚未启用，请先在设置中测试并启用")
    try:
        return QbittorrentWebAdapter(get_decrypted_config(db, downloader_id) or {})
    except QbittorrentConfigError as exc:
        raise HTTPException(status_code=409, detail="qB WebUI 地址、用户名或密码未配置") from exc


def get_qb_state_for_dashboard(db: Session, downloader_id: str) -> dict[str, Any]:
    row = get_config(db, downloader_id)
    if not row or not row.encrypted_payload or not row.enabled:
        return qb_placeholder_state(downloader_id, row)
    try:
        return QbittorrentWebAdapter(get_decrypted_config(db, downloader_id) or {}).get_server_state(downloader_id)
    except Exception as exc:
        return qb_placeholder_state(downloader_id, row, f"qB 真实数据读取失败：{exc}")


def _storage_disk_key(path: Path, raw_path: str) -> Any:
    try:
        return path.stat().st_dev
    except OSError:
        return os.path.normcase(path.anchor or raw_path)


def nas_storage_from_configs(db: Session) -> dict[str, Any]:
    del db
    disks_seen: set[Any] = set()
    primary_usage: Any | None = None
    readable_paths: list[str] = []
    errors: list[dict[str, str]] = []
    primary_path = DEFAULT_STORAGE_MOUNT_PATHS[0]

    for raw_path in DEFAULT_STORAGE_MOUNT_PATHS:
        path = Path(raw_path)
        try:
            if not path.exists():
                errors.append({"path": raw_path, "message": "Path is not mounted or the container cannot access it."})
                continue
            readable_paths.append(raw_path)
            disk_key = _storage_disk_key(path, raw_path)
            disks_seen.add(disk_key)
            if raw_path == primary_path:
                primary_usage = shutil.disk_usage(path)
        except Exception as exc:
            errors.append({"path": raw_path, "message": str(exc)})

    pool_count = len(disks_seen)
    folder_count = len(readable_paths)
    summary_label = f"已识别 {pool_count} 个存储池，{folder_count} 个文件夹可访问" if folder_count else "未检测到存储挂载"
    common = {
        "nas_storage_pool_count": pool_count,
        "nas_storage_folder_count": folder_count,
        "nas_storage_detected_paths": readable_paths,
        "nas_storage_summary_label": summary_label,
        "nas_storage_paths": readable_paths,
        "nas_storage_errors": errors,
    }

    total_space = float(primary_usage.total) if primary_usage else 0.0
    free_space = float(primary_usage.free) if primary_usage else 0.0

    if total_space <= 0:
        return {
            "nas_free_space": 0,
            "nas_total_space": 0,
            "nas_used_space": 0,
            "nas_usage_percent": None,
            "nas_free_space_label": "-",
            "nas_free_space_source": "",
            "nas_space_label": "-",
            "nas_space_helper": "请先配置 NAS 存储路径，以显示空间使用情况。",
            "nas_storage_configured": folder_count > 0,
            "nas_storage_readable": False,
            **common,
        }

    used_space = max(0.0, total_space - free_space)
    usage_percent = round((used_space / total_space) * 100, 1)
    return {
        "nas_free_space": free_space,
        "nas_total_space": total_space,
        "nas_used_space": used_space,
        "nas_usage_percent": usage_percent,
        "nas_free_space_label": bytes_label(free_space, 2),
        "nas_free_space_source": "Docker 固定存储挂载",
        "nas_space_label": f"{bytes_label(used_space, 2)} / {bytes_label(total_space, 2)}",
        "nas_space_helper": f"已使用 {usage_percent}%",
        "nas_storage_configured": True,
        "nas_storage_readable": True,
        **common,
    }


TRAFFIC_DIMENSIONS = ("hour", "day", "week", "month", "year")


def build_mteam_traffic_series(db: Session) -> dict[str, list[dict[str, Any]]]:
    points = mteam_snapshot_delta_points(db)
    return {dimension: aggregate_traffic_points(points, dimension) for dimension in TRAFFIC_DIMENSIONS}


def mteam_snapshot_delta_points(db: Session, limit: int = 5000) -> list[dict[str, Any]]:
    rows = list(
        reversed(
            db.query(MTeamSnapshot)
            .filter(MTeamSnapshot.source != "mock")
            .order_by(MTeamSnapshot.captured_at.desc())
            .limit(limit)
            .all()
        )
    )
    points = []
    previous: MTeamSnapshot | None = None
    for row in rows:
        if previous is None:
            previous = row
            continue
        points.append(
            {
                "captured_at": row.captured_at,
                "upload_total": max(0.0, float(row.upload_total or 0) - float(previous.upload_total or 0)),
                "download_total": max(0.0, float(row.download_total or 0) - float(previous.download_total or 0)),
                "source": "app_calculated",
            }
        )
        previous = row
    return points


def aggregate_traffic_points(points: list[dict[str, Any]], dimension: str) -> list[dict[str, Any]]:
    buckets: dict[datetime, dict[str, float]] = {}
    for point in points:
        captured_at = point.get("captured_at")
        if not isinstance(captured_at, datetime):
            continue
        period = traffic_period_start(captured_at, dimension)
        if period not in buckets:
            buckets[period] = {"upload_total": 0.0, "download_total": 0.0}
        buckets[period]["upload_total"] += float(point.get("upload_total") or 0)
        buckets[period]["download_total"] += float(point.get("download_total") or 0)
    return [
        {
            "date": period.isoformat(),
            "label": traffic_period_label(period, dimension),
            "dimension": dimension,
            "upload_total": totals["upload_total"],
            "download_total": totals["download_total"],
        }
        for period, totals in sorted(buckets.items())
    ]


def traffic_period_start(value: datetime, dimension: str) -> datetime:
    if dimension == "hour":
        return datetime(value.year, value.month, value.day, value.hour)
    day = datetime(value.year, value.month, value.day)
    if dimension == "week":
        return day - timedelta(days=day.weekday())
    if dimension == "month":
        return datetime(value.year, value.month, 1)
    if dimension == "year":
        return datetime(value.year, 1, 1)
    return day


def traffic_period_label(value: datetime, dimension: str) -> str:
    display_value = value + timedelta(hours=8)
    if dimension == "hour":
        return display_value.strftime("%m/%d %H:00")
    if dimension == "year":
        return display_value.strftime("%Y")
    if dimension == "month":
        return display_value.strftime("%Y/%m")
    if dimension == "week":
        end = display_value + timedelta(days=6)
        return f"{display_value.strftime('%m/%d')}~{end.strftime('%m/%d')}"
    return display_value.strftime("%m/%d")


def persist_dashboard_snapshots(db: Session, mteam: dict[str, Any], qbs: list[dict[str, Any]], mteam_ok: bool) -> None:
    has_rows = False
    if mteam_ok:
        db.add(
            MTeamSnapshot(
                user_level=str(mteam.get("user_level") or ""),
                upload_total=float(mteam.get("upload_total") or 0),
                download_total=float(mteam.get("download_total") or 0),
                bonus=float(mteam.get("bonus") or 0),
                ratio=float(mteam.get("ratio") or 0),
                seed_size=float(mteam.get("seed_size") or 0),
                active_uploads=int(mteam.get("active_uploads") or 0),
                active_downloads=int(mteam.get("active_downloads") or 0),
                source="real",
            )
        )
        has_rows = True
    for qb in qbs:
        if not qb.get("online"):
            continue
        db.add(
            QbSnapshot(
                downloader_id=str(qb.get("id") or ""),
                download_speed=float(qb.get("download_speed") or 0),
                upload_speed=float(qb.get("upload_speed") or 0),
                downloaded_total=float(qb.get("downloaded_total") or 0),
                uploaded_total=float(qb.get("uploaded_total") or 0),
                active_downloads=int(qb.get("active_downloads") or 0),
                active_uploads=int(qb.get("active_uploads") or 0),
                source="real",
            )
        )
        has_rows = True
    if has_rows:
        db.commit()


@router.get("/setup/status")
def setup_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"initialized": db.query(User).filter(User.role == "admin").count() > 0}


@router.post("/setup/admin")
def setup_admin(request: SetupAdminRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    if db.query(User).filter(User.role == "admin").count() > 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Setup already completed")
    user = User(username=request.username, password_hash=hash_password(request.password), role="admin")
    db.add(user)
    db.add(Notification(title="欢迎", message="PT Media Hub 初始化完成。", level="success"))
    db.commit()
    db.refresh(user)
    token = create_access_token(db, user)
    return {"access_token": token, "token_type": "bearer", "user": {"username": user.username, "role": user.role}}


@router.post("/auth/login")
def login(request: LoginRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    trace = TraceRecorder(db, "login", "LOGIN")
    user = db.query(User).filter(User.username == request.username).one_or_none()
    if not user or user.role != "admin" or not verify_password(request.password, user.password_hash):
        trace.add("login failed", {"username": request.username})
        trace.finish(status="failed", error_summary="用户名或密码错误")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    token = create_access_token(db, user)
    trace.add("login success", {"username": user.username, "role": user.role})
    trace.finish()
    return {"access_token": token, "token_type": "bearer", "user": {"username": user.username, "role": user.role}}


@router.get("/auth/me")
def me(user: User = Depends(get_current_user)) -> dict[str, Any]:
    return {"id": user.id, "username": user.username, "role": user.role}


@router.get("/admin/users")
def list_users(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    admin = db.query(User).filter(User.role == "admin").order_by(User.id.asc()).first()
    return {"items": [{"id": admin.id, "username": admin.username, "role": admin.role, "is_active": admin.is_active}] if admin else []}


@router.post("/auth/admin-verify")
def admin_verify(request: AdminVerifyRequest, authorization: str | None = Header(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    admin = db.query(User).filter(User.username == request.username, User.role == "admin").one_or_none()
    if not admin or not verify_password(request.password, admin.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="管理员验证失败")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    payload = decode_token(authorization.split(" ", 1)[1])
    session = db.query(UserSession).filter(UserSession.token_id == payload["jti"]).one()
    expires = datetime.utcnow() + timedelta(minutes=settings.qb2_grant_minutes)
    session.qb2_grant_expires_at = expires
    db.commit()
    trace = TraceRecorder(db, "admin_verify", "ADMIN")
    trace.add("qB2 grant issued", {"actor": user.username, "admin": admin.username, "expires_at": expires.isoformat()})
    trace.finish()
    return {"qb2_granted": True, "expires_at": expires.isoformat()}


@router.post("/auth/qb2-grant/revoke")
def revoke_qb2_grant(authorization: str | None = Header(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    payload = decode_token(authorization.split(" ", 1)[1])
    session = db.query(UserSession).filter(UserSession.token_id == payload["jti"]).one_or_none()
    if session:
        session.qb2_grant_expires_at = None
        db.commit()
    trace = TraceRecorder(db, "qb2_grant_revoke", "ADMIN")
    trace.add("qB2 grant revoked", {"actor": user.username})
    trace.finish()
    return {"qb2_granted": False}


@router.get("/admin/integrations")
def list_integrations(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    existing = {row.provider: row for row in db.query(IntegrationConfig).all()}
    return {
        "providers": [
            serialize_config(existing[provider], include_plain_payload=True)
            if provider in existing
            else {"provider": provider, "config_version": 0, "enabled": False, "redacted_summary": {}, "saved_payload": {}, "last_tested_at": None, "last_test_result": None, "updated_at": None}
            for provider in PROVIDERS
        ]
    }


@router.get("/admin/storage/status")
def admin_storage_status(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    return nas_storage_from_configs(db)


@router.put("/admin/integrations/{provider}")
def save_integration(provider: str, request: IntegrationPayload, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider")
    return serialize_config(upsert_config(db, provider, request.payload, actor_user_id=user.id))


@router.post("/admin/integrations/{provider}/test")
def test_integration(provider: str, request: IntegrationPayload | None = None, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider")
    row = upsert_config(db, provider, request.payload, actor_user_id=user.id, action="save_and_test") if request and request.payload else get_config(db, provider)
    test_trace_id = trace_id("CFGTEST")
    if provider == "tmdb":
        config = get_decrypted_config(db, provider) or {}
        network_detail = tmdb_network_detail_from_config(config)
        try:
            adapter = TmdbAdapter(config)
            detail = adapter.test_connection().get("network") or adapter.network_detail()
            result = tmdb_test_result(True, test_trace_id, "TMDB 连接成功。", "应用已经成功访问 TMDB，并验证了当前 Bearer Token 可以使用。", "你现在可以点击“启用”，然后回到“发现”页查看真实 TMDB 数据。", None, 200, detail)
        except Exception as exc:
            result = classify_tmdb_test_error(exc, test_trace_id)
            result["detail"] = network_detail
            if network_detail.get("network_mode") == "proxy" and result.get("error_type") in {"network_error", "timeout", "unknown_error"}:
                result["message"] = "无法通过 mihomo 代理连接 TMDB。"
                result["explanation"] = "当前代理未能完成连接。"
                result["next_step"] = "请检查高级设置中的代理地址后重试。"
        result["config_version"] = row.config_version if row else 0
    elif provider == "mteam":
        try:
            config = get_decrypted_config(db, provider) or {}
            adapter = MTeamAdapter(config)
            stats = adapter.get_user_stats()
            result = mteam_test_result(
                True,
                test_trace_id,
                "M-Team 连接成功。",
                f"应用已经成功读取 M-Team 个人资料，当前等级为 {stats.get('user_level') or 'User'}。",
                "你现在可以点击“启用”，然后回到仪表盘查看真实 M-Team 站点数据。",
                None,
                200,
            )
        except Exception as exc:
            result = classify_mteam_test_error(exc, test_trace_id)
        result["config_version"] = row.config_version if row else 0
    elif provider in {"qb1", "qb2", "qb3"}:
        config = get_decrypted_config(db, provider) or {}
        try:
            detail = QbittorrentWebAdapter(config).test_connection()
            result = qb_test_result(
                True,
                provider,
                test_trace_id,
                "qB 连接成功。",
                f"应用已经成功登录 qB WebUI，并读取到版本 {detail.get('version') or 'unknown'}。",
                "你现在可以启用这个 qB 下载器，仪表盘和下载页会读取真实实时数据。",
                detail=detail,
            )
        except Exception as exc:
            result = classify_qb_test_error(provider, exc, test_trace_id)
        result["config_version"] = row.config_version if row else 0
    elif provider == "ai":
        config = get_decrypted_config(db, provider) or {}
        try:
            detail = DeepSeekChatAdapter(config).test_connection()
            result = ai_test_result(
                True,
                test_trace_id,
                "AI 助手连接成功。",
                "现在可以用自然语言查询影视、资源和下载状态。",
                "启用 AI 助手即可开始使用。",
                None,
                200,
                detail=detail,
            )
        except Exception as exc:
            result = classify_ai_test_error(exc, test_trace_id)
        result["config_version"] = row.config_version if row else 0
    elif provider == "wechat_claw":
        config = get_decrypted_config(db, provider) or {}
        try:
            detail = WechatClawAdapter(config).test_connection()
            result = {
                "success": True,
                "provider": "wechat_claw",
                "mode": "real",
                "http_status": 200,
                "duration_ms": 0,
                "message": "微信连接可用。",
                "explanation": "已准备好接收微信消息。",
                "next_step": "启用后刷新二维码，用微信扫码绑定。",
                "error_type": None,
                "can_enable": True,
                "trace_id": test_trace_id,
                "detail": detail,
                "config_version": row.config_version if row else 0,
            }
        except WechatClawConfigError as exc:
            result = {
                "success": False,
                "provider": "wechat_claw",
                "mode": "real",
                "http_status": None,
                "duration_ms": 0,
                "message": "微信连接尚未配置完成。",
                "explanation": "请检查高级设置中的连接信息。",
                "next_step": "保存后重新测试。",
                "error_type": "missing_required_fields",
                "can_enable": False,
                "trace_id": test_trace_id,
                "config_version": row.config_version if row else 0,
            }
    else:
        result = mock_test_result(provider, test_trace_id, row)
    return serialize_config(record_test_result(db, provider, result, actor_user_id=user.id))


@router.post("/admin/integrations/tmdb/cloudflare-secrets")
def set_tmdb_cloudflare_secrets(request: IntegrationPayload, _: User = Depends(require_admin)) -> dict[str, Any]:
    raise HTTPException(status_code=410, detail="此连接方式已不再支持，请使用 TMDB 的默认连接方式或高级设置中的代理。")


@router.post("/admin/integrations/{provider}/enable")
def enable_integration(provider: str, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider")
    if provider == "tmdb":
        row = get_config(db, provider)
        result = row.last_test_result if row else None
        if not result or result.get("provider") != "tmdb" or result.get("mode") != "real" or result.get("can_enable") is not True:
            raise HTTPException(status_code=409, detail="请先点击“保存并测试”，确认 TMDB 连接成功后再启用。")
    if provider == "mteam":
        row = get_config(db, provider)
        result = row.last_test_result if row else None
        if not result or result.get("provider") != "mteam" or result.get("mode") != "real" or result.get("success") is not True:
            raise HTTPException(status_code=409, detail="请先点击“保存并测试”，确认 M-Team 配置可用后再启用。")
    if provider in {"qb1", "qb2", "qb3"}:
        row = get_config(db, provider)
        result = row.last_test_result if row else None
        if not result or result.get("provider") != provider or result.get("mode") != "real" or result.get("success") is not True or result.get("can_enable") is not True:
            raise HTTPException(status_code=409, detail="请先点击“保存并测试”，确认 qB WebUI 连接成功后再启用。")
    if provider == "ai":
        row = get_config(db, provider)
        result = row.last_test_result if row else None
        if not result or result.get("provider") != "ai" or result.get("mode") != "real" or result.get("success") is not True or result.get("can_enable") is not True:
            raise HTTPException(status_code=409, detail="请先保存并测试 DeepSeek，确认 AI 模块可用后再启用。")
    if provider == "wechat_claw":
        row = get_config(db, provider)
        result = row.last_test_result if row else None
        if not result or result.get("provider") != "wechat_claw" or result.get("success") is not True or result.get("can_enable") is not True:
            raise HTTPException(status_code=409, detail="请先保存并测试 WeChat claw，确认手机端入口配置可用后再启用。")
    return serialize_config(set_enabled(db, provider, True, actor_user_id=user.id))


@router.post("/admin/integrations/{provider}/disable")
def disable_integration(provider: str, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider")
    return serialize_config(set_enabled(db, provider, False, actor_user_id=user.id))


@router.get("/admin/integrations/{provider}/audit")
def integration_audit(provider: str, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.query(ConfigAuditLog).filter(ConfigAuditLog.provider == provider).order_by(ConfigAuditLog.created_at.desc()).limit(30).all()
    return {"items": [{"action": row.action, "config_version": row.config_version, "test_success": row.test_success, "trace_id": row.trace_id, "created_at": row.created_at.isoformat()} for row in rows]}


def build_dashboard_payload(db: Session) -> dict[str, Any]:
    mteam_row = get_config(db, "mteam")
    connection = mteam_connection_payload(mteam_row)
    mteam = empty_mteam_stats()
    mteam_ok = False
    if mteam_row and mteam_row.enabled:
        try:
            mteam = MTeamAdapter(get_decrypted_config(db, "mteam") or {}).get_user_stats()
            mteam_ok = True
        except Exception as exc:
            connection["last_test_success"] = False
            connection["message"] = f"M-Team 真实数据读取失败：{exc}"
            mteam = empty_mteam_stats("M-Team 真实数据读取失败，请回到设置页重新测试。")
    qbs = [get_qb_state_for_dashboard(db, downloader_id) for downloader_id in ["qb1", "qb2", "qb3"]]
    nas_space = nas_storage_from_configs(db)
    persist_dashboard_snapshots(db, mteam, qbs, mteam_ok)
    if mteam_ok:
        apply_mteam_five_hour_deltas(db, mteam)
    mteam["traffic_series"] = build_mteam_traffic_series(db)
    mteam["traffic_history"] = mteam["traffic_series"]["day"]
    mteam["traffic_calculation"] = {
        "source": "APP 本地累计快照",
        "formula": "周期流量 = 周期内相邻累计快照差值之和；负增量按 0 处理，避免站点计数回退污染统计。",
        "snapshot_interval_minutes": settings.snapshot_interval_minutes,
    }
    return {
        "overview": {
            "total_download_speed": sum(item.get("download_speed", 0) for item in qbs),
            "total_upload_speed": sum(item.get("upload_speed", 0) for item in qbs),
            "nas_free_space": nas_space["nas_free_space"],
            "nas_total_space": nas_space["nas_total_space"],
            "nas_used_space": nas_space["nas_used_space"],
            "nas_usage_percent": nas_space["nas_usage_percent"],
            "nas_free_space_label": nas_space["nas_free_space_label"],
            "nas_free_space_source": nas_space["nas_free_space_source"],
            "nas_space_label": nas_space["nas_space_label"],
            "nas_space_helper": nas_space["nas_space_helper"],
            "nas_storage_configured": nas_space["nas_storage_configured"],
            "nas_storage_readable": nas_space["nas_storage_readable"],
            "nas_storage_paths": nas_space["nas_storage_paths"],
            "nas_storage_pool_count": nas_space["nas_storage_pool_count"],
            "nas_storage_folder_count": nas_space["nas_storage_folder_count"],
            "nas_storage_detected_paths": nas_space["nas_storage_detected_paths"],
            "nas_storage_summary_label": nas_space["nas_storage_summary_label"],
            "nas_storage_errors": nas_space["nas_storage_errors"],
            "download_tasks": sum(item.get("active_downloads", 0) for item in qbs),
            "upload_tasks": sum(item.get("active_uploads", 0) for item in qbs),
        },
        "mteam_connection": connection,
        "mteam": mteam,
        "qbs": qbs,
        "updated_at": datetime.utcnow().isoformat(),
    }


def refresh_dashboard_preload(db: Session) -> dict[str, Any]:
    payload = build_dashboard_payload(db)
    set_preload_cache(db, "dashboard", payload)
    return payload


def build_dashboard_qbs_payload(db: Session) -> dict[str, Any]:
    configs: list[tuple[str, dict[str, Any], str | None]] = []
    states: list[dict[str, Any] | None] = []
    for downloader_id in ["qb1", "qb2", "qb3"]:
        row = get_config(db, downloader_id)
        configured = bool(row and row.encrypted_payload)
        enabled = bool(row and row.enabled)
        name = (row.redacted_summary or {}).get("name") if row else None
        if not configured or not enabled:
            states.append(qb_placeholder_state_from_meta(downloader_id, configured, enabled, name))
            continue
        try:
            configs.append((downloader_id, get_decrypted_config(db, downloader_id) or {}, name))
            states.append(None)
        except Exception as exc:
            states.append(qb_placeholder_state_from_meta(downloader_id, configured, enabled, name, f"qB 配置读取失败：{exc}"))
    db.close()

    config_index = 0
    for index, state in enumerate(states):
        if state is not None:
            continue
        downloader_id, config, name = configs[config_index]
        config_index += 1
        try:
            states[index] = QbittorrentWebAdapter(config).get_server_state(downloader_id)
        except Exception as exc:
            states[index] = qb_placeholder_state_from_meta(downloader_id, True, True, name, f"qB 真实数据读取失败：{exc}")

    qbs = [item for item in states if item is not None]
    return {
        "qbs": qbs,
        "overview": {
            "total_download_speed": sum(item.get("download_speed", 0) for item in qbs),
            "total_upload_speed": sum(item.get("upload_speed", 0) for item in qbs),
            "download_tasks": sum(item.get("active_downloads", 0) for item in qbs),
            "upload_tasks": sum(item.get("active_uploads", 0) for item in qbs),
        },
        "updated_at": datetime.utcnow().isoformat(),
    }


def build_download_payload(db: Session, downloader_id: str) -> dict[str, Any]:
    adapter = get_qb_adapter_or_error(db, downloader_id)
    summary = adapter.get_server_state(downloader_id)
    items = adapter.get_torrents(downloader_id)
    summary.update(adapter.summarize_torrents(items))
    return {
        "downloader_id": downloader_id,
        "summary": summary,
        "items": items,
        "source": "qB Web API 原始数据（Real）",
        "updated_at": datetime.utcnow().isoformat(),
    }


def refresh_download_preload(db: Session, downloader_id: str) -> dict[str, Any]:
    payload = build_download_payload(db, downloader_id)
    set_preload_cache(db, f"downloads.{downloader_id}", payload)
    return payload


@router.get("/dashboard")
def dashboard(
    background_tasks: BackgroundTasks,
    cached: bool = Query(default=False),
    refresh: bool = Query(default=False),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict[str, Any]:
    if cached and not refresh:
        cache = get_preload_cache(db, "dashboard")
        if cache:
            return with_preload_meta(cache["payload"], cache, True)
    payload = refresh_dashboard_preload(db)
    return with_preload_meta(payload, get_preload_cache(db, "dashboard"), False)


@router.get("/dashboard/qbs")
def dashboard_qbs(db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    return build_dashboard_qbs_payload(db)


@router.get("/mteam/stats")
def mteam_stats(db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    try:
        return get_mteam_adapter_or_error(db).get_user_stats()
    except MTeamApiError as exc:
        raise HTTPException(status_code=502, detail=f"M-Team 数据获取失败：{exc}") from exc


@router.post("/mteam/test")
def mteam_quick_test(db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    try:
        result = get_mteam_adapter_or_error(db).test_connection()
        return {
            "success": True,
            "message": "M-Team 连接正常",
            "detail": result,
        }
    except MTeamApiError as exc:
        raise HTTPException(status_code=502, detail=f"M-Team 连通性测试失败：{exc}") from exc


@router.get("/downloads/{downloader_id}/overview")
def download_overview(
    downloader_id: str,
    background_tasks: BackgroundTasks,
    cached: bool = Query(default=False),
    refresh: bool = Query(default=False),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict[str, Any]:
    if downloader_id not in {"qb1", "qb2", "qb3"}:
        raise HTTPException(status_code=404, detail="未知下载器")
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB2 需要管理员验证")
    cache_name = f"downloads.{downloader_id}"
    if cached and not refresh:
        cache = get_preload_cache(db, cache_name)
        if cache:
            add_preload_task_once(background_tasks, f"downloads.{downloader_id}", refresh_download_preload_task, downloader_id)
            return with_preload_meta(cache["payload"], cache, True)
    try:
        payload = refresh_download_preload(db, downloader_id)
    except QbittorrentApiError as exc:
        raise HTTPException(status_code=502, detail=f"qB 下载页数据获取失败：{exc}") from exc
    return with_preload_meta(payload, get_preload_cache(db, cache_name), False)


@router.post("/qb/{downloader_id}/test")
def qb_test_connection(downloader_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    if downloader_id not in {"qb1", "qb2", "qb3"}:
        raise HTTPException(status_code=404, detail="未知下载器")
    row = get_config(db, downloader_id)
    test_trace_id = trace_id("QBTEST")
    try:
        detail = QbittorrentWebAdapter(get_decrypted_config(db, downloader_id) or {}).test_connection()
        result = qb_test_result(
            True,
            downloader_id,
            test_trace_id,
            "qB 连接成功。",
            f"应用已经成功登录 qB WebUI，并读取到版本 {detail.get('version') or 'unknown'}。",
            "下载器连接正常，可以继续读取仪表盘和任务数据。",
            detail=detail,
        )
    except Exception as exc:
        result = classify_qb_test_error(downloader_id, exc, test_trace_id)
    result["config_version"] = row.config_version if row else 0
    record_test_result(db, downloader_id, result, actor_user_id=user.id)
    return {
        "success": result["success"],
        "provider": downloader_id,
        "message": result["message"],
        "explanation": result.get("explanation"),
        "next_step": result.get("next_step"),
        "trace_id": test_trace_id,
        "updated_at": datetime.utcnow().isoformat(),
    }


@router.get("/qb/{downloader_id}/summary")
def qb_summary(downloader_id: str, authorization: str | None = Header(default=None), db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB2 需要管理员验证")
    try:
        return get_qb_adapter_or_error(db, downloader_id).get_server_state(downloader_id)
    except QbittorrentApiError as exc:
        raise HTTPException(status_code=502, detail=f"qB 数据获取失败：{exc}") from exc


@router.get("/qb/{downloader_id}/torrents")
def qb_torrents(downloader_id: str, authorization: str | None = Header(default=None), db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB2 需要管理员验证")
    try:
        return {"items": get_qb_adapter_or_error(db, downloader_id).get_torrents(downloader_id)}
    except QbittorrentApiError as exc:
        raise HTTPException(status_code=502, detail=f"qB 任务列表获取失败：{exc}") from exc


@router.get("/qb/{downloader_id}/torrents/{torrent_hash}/detail")
def qb_torrent_detail(downloader_id: str, torrent_hash: str, authorization: str | None = Header(default=None), db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB2 需要管理员验证")
    try:
        return get_qb_adapter_or_error(db, downloader_id).get_torrent_detail(downloader_id, torrent_hash)
    except QbittorrentApiError as exc:
        raise HTTPException(status_code=502, detail=f"qB 任务详情获取失败：{exc}") from exc


@router.post("/qb/{downloader_id}/torrents/{torrent_hash}/files/{file_id}/priority")
def qb_file_priority(downloader_id: str, torrent_hash: str, file_id: int, request: QbActionPayload, authorization: str | None = Header(default=None), db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB2 需要管理员验证")
    try:
        priority = int(request.payload.get("priority"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="文件优先级参数无效") from exc
    try:
        return get_qb_adapter_or_error(db, downloader_id).set_file_priority(downloader_id, torrent_hash, file_id, priority)
    except QbittorrentApiError as exc:
        raise HTTPException(status_code=502, detail=f"qB 文件优先级设置失败：{exc}") from exc


@router.post("/qb/{downloader_id}/torrents")
def qb_add_torrent(downloader_id: str, request: QbActionPayload, authorization: str | None = Header(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB2 需要管理员验证")
    try:
        result = get_qb_adapter_or_error(db, downloader_id).add_torrent(downloader_id, request.payload)
    except QbittorrentApiError as exc:
        raise HTTPException(status_code=502, detail=f"qB 添加任务失败：{exc}") from exc
    db.add(DownloadAction(trace_id=result["trace_id"], downloader_id=downloader_id, action="add", actor_user_id=user.id, status="accepted"))
    db.commit()
    return result


@router.post("/mteam/torrents/{torrent_id}/download-to/{downloader_id}")
def mteam_download_to_qb(
    torrent_id: str,
    downloader_id: str,
    request: QbActionPayload,
    authorization: str | None = Header(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if downloader_id not in {"qb1", "qb2", "qb3"}:
        raise HTTPException(status_code=404, detail="未知下载器")
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB2 需要管理员验证")
    stage = "mteam_config"
    try:
        mteam_adapter = get_mteam_adapter_or_error(db)
        stage = "mteam_download_torrent"
        torrent_file = mteam_adapter.download_torrent_file(torrent_id)
        stage = "qb_config"
        qb_adapter = get_qb_adapter_or_error(db, downloader_id)
        stage = "qb_add_torrent_file"
        result = qb_adapter.add_torrent_file(
            downloader_id,
            torrent_file["filename"],
            torrent_file["content"],
            request.payload,
        )
    except MTeamApiError as exc:
        raise HTTPException(status_code=502, detail={"stage": stage, "provider": "mteam", "torrent_id": torrent_id, "message": str(exc)}) from exc
    except QbittorrentApiError as exc:
        raise HTTPException(status_code=502, detail={"stage": stage, "provider": downloader_id, "torrent_id": torrent_id, "message": str(exc)}) from exc
    db.add(DownloadAction(trace_id=result["trace_id"], downloader_id=downloader_id, action="add_mteam", target_hash=torrent_id, actor_user_id=user.id, status="accepted"))
    db.commit()
    return {**result, "torrent_id": torrent_id, "filename": torrent_file["filename"]}


@router.post("/qb/{downloader_id}/torrents/{torrent_hash}/delete-confirm")
def qb_delete_confirm(downloader_id: str, torrent_hash: str, user: User = Depends(get_current_user)) -> dict[str, Any]:
    return {"confirm_token": trace_id("DEL"), "message": "用户确认风险后，请在 DELETE 请求中提交这个确认令牌。", "target": {"downloader_id": downloader_id, "hash": torrent_hash, "actor": user.username}}


@router.post("/qb/{downloader_id}/torrents/{torrent_hash}/{action}")
def qb_mutate(downloader_id: str, torrent_hash: str, action: str, request: QbActionPayload, authorization: str | None = Header(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    if action not in {"pause", "resume", "limits", "category", "tags"}:
        raise HTTPException(status_code=404, detail="不支持的操作")
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB2 需要管理员验证")
    try:
        result = get_qb_adapter_or_error(db, downloader_id).mutate_torrent(downloader_id, torrent_hash, action, request.payload)
    except QbittorrentApiError as exc:
        raise HTTPException(status_code=502, detail=f"qB 操作失败：{exc}") from exc
    db.add(DownloadAction(trace_id=result["trace_id"], downloader_id=downloader_id, action=action, target_hash=torrent_hash, actor_user_id=user.id))
    db.commit()
    return result


@router.delete("/qb/{downloader_id}/torrents/{torrent_hash}")
def qb_delete(downloader_id: str, torrent_hash: str, confirm_token: str = Query(min_length=8), delete_files: bool = Query(default=False), authorization: str | None = Header(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    if not confirm_token.startswith("DEL-"):
        raise HTTPException(status_code=400, detail="需要服务端删除确认令牌")
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB2 需要管理员验证")
    action = "delete_files" if delete_files else "delete"
    try:
        result = get_qb_adapter_or_error(db, downloader_id).mutate_torrent(downloader_id, torrent_hash, action, {"delete_files": delete_files})
    except QbittorrentApiError as exc:
        raise HTTPException(status_code=502, detail=f"qB 删除任务失败：{exc}") from exc
    db.add(DownloadAction(trace_id=result["trace_id"], downloader_id=downloader_id, action=action, target_hash=torrent_hash, actor_user_id=user.id))
    db.commit()
    return result


def build_discover_payload(db: Session) -> dict[str, Any]:
    config_row = get_config(db, "tmdb")
    if not config_row or not config_row.enabled:
        return {"source": "tmdb", "configured": False, "message": "请先在设置中保存并启用 TMDB API Key 或 Bearer Token。", "trending": [], "popular_movies": [], "popular_tv": [], "top_rated_movies": [], "top_rated_tv": []}
    try:
        return TmdbAdapter(get_decrypted_config(db, "tmdb") or {}).get_discover_lists()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TMDB 数据获取失败：{exc}") from exc


def default_discover_filter() -> dict[str, Any]:
    return {
        "media_type": "movie",
        "sort_by": "popularity.desc",
        "genre": "",
        "region": "",
        "language": "",
        "year": "",
        "min_rating": None,
        "page": 1,
        "pages": 1,
    }


def discover_filter_cache_name(filters: dict[str, Any]) -> str:
    normalized = {
        key: value
        for key, value in filters.items()
        if key in {"media_type", "sort_by", "genre", "region", "language", "year", "min_rating", "page", "pages"} and value not in (None, "")
    }
    digest = hashlib.sha1(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:18]
    return f"discover.filter.{digest}"


def build_discover_filter_payload(db: Session, filters: dict[str, Any]) -> dict[str, Any]:
    config_row = get_config(db, "tmdb")
    if config_row and config_row.enabled:
        try:
            return TmdbAdapter(get_decrypted_config(db, "tmdb") or {}).discover_media(filters)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"TMDB 条件筛选失败：{exc}") from exc
    return MockMetadataAdapter().discover_media(filters)


def refresh_discover_filter_preload(db: Session, filters: dict[str, Any]) -> dict[str, Any]:
    payload = build_discover_filter_payload(db, filters)
    set_preload_cache(db, discover_filter_cache_name(filters), payload)
    return payload


def refresh_discover_preload(db: Session) -> dict[str, Any]:
    payload = build_discover_payload(db)
    set_preload_cache(db, "discover", payload)
    try:
        refresh_discover_filter_preload(db, default_discover_filter())
    except Exception:
        pass
    return payload


@router.get("/discover/lists")
def discover_lists(
    background_tasks: BackgroundTasks,
    cached: bool = Query(default=False),
    refresh: bool = Query(default=False),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if cached and not refresh:
        cache = get_preload_cache(db, "discover")
        if cache:
            return with_preload_meta(cache["payload"], cache, True)
    payload = refresh_discover_preload(db)
    return with_preload_meta(payload, get_preload_cache(db, "discover"), False)


@router.get("/discover/filter")
def discover_filter(
    background_tasks: BackgroundTasks,
    media_type: str = Query(default="movie"),
    sort_by: str = Query(default="popularity.desc"),
    genre: str = Query(default=""),
    region: str = Query(default=""),
    language: str = Query(default=""),
    year: str = Query(default=""),
    min_rating: float | None = Query(default=None, ge=0, le=10),
    page: int = Query(default=1, ge=1, le=500),
    pages: int = Query(default=1, ge=1, le=4),
    include_options: bool = Query(default=True),
    cached: bool = Query(default=False),
    refresh: bool = Query(default=False),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict[str, Any]:
    filters = {
        "media_type": media_type,
        "sort_by": sort_by,
        "genre": genre,
        "region": region,
        "language": language,
        "year": year,
        "min_rating": min_rating,
        "page": page,
        "pages": pages,
        "include_options": include_options,
    }
    cache_name = discover_filter_cache_name(filters)
    if cached and not refresh:
        cache = get_preload_cache(db, cache_name)
        if cache:
            return with_preload_meta(cache["payload"], cache, True)
    payload = refresh_discover_filter_preload(db, filters)
    return with_preload_meta(payload, get_preload_cache(db, cache_name), False)


@router.get("/tmdb/image/{size}/{image_path:path}")
def tmdb_image_proxy(size: str, image_path: str, db: Session = Depends(get_db)) -> Response:
    if size not in TMDB_IMAGE_SIZES:
        raise HTTPException(status_code=400, detail="Unsupported TMDB image size")
    clean_path = _safe_tmdb_image_path(image_path)
    cache_dir = Path(settings.tmdb_image_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = _tmdb_image_cache_file(size, clean_path)
    media_type = mimetypes.guess_type(clean_path)[0] or "image/jpeg"
    if cache_file.exists() and cache_file.stat().st_size > 0:
        if _valid_tmdb_image_file(cache_file):
            cached_media_type = _tmdb_cached_image_media_type(cache_file, media_type)
            return FileResponse(cache_file, media_type=cached_media_type, headers=_tmdb_image_headers(cache_file))
        try:
            cache_file.unlink()
            logger.warning("Removed invalid TMDB image cache file: size=%s path=%s file=%s", size, clean_path, cache_file)
        except OSError:
            logger.warning("TMDB image cache file is invalid but could not be removed: size=%s path=%s file=%s", size, clean_path, cache_file)

    url = f"https://image.tmdb.org/t/p/{size}/{clean_path}"
    request = Request(url, headers={"Accept": "image/jpeg,image/png,image/webp,*/*", "User-Agent": "PT-Media-Hub"})
    mode, proxy_url, timeout = _tmdb_image_network_config(db)
    try:
        with open_tmdb_network_request(request, mode, proxy_url, timeout) as response:
            content = response.read(TMDB_IMAGE_MAX_BYTES + 1)
            if len(content) > TMDB_IMAGE_MAX_BYTES:
                logger.warning("TMDB image fetch rejected because it is too large: size=%s path=%s mode=%s", size, clean_path, mode)
                return _tmdb_image_placeholder_response("image_too_large")
            if not _valid_tmdb_image_bytes(content):
                logger.warning("TMDB image fetch returned non-image content: size=%s path=%s mode=%s content_type=%s", size, clean_path, mode, media_type)
                return _tmdb_image_placeholder_response("invalid_image_content")
            media_type = _tmdb_image_media_type_from_bytes(content, response.headers.get_content_type() or media_type)
    except (HTTPError, HTTPException, Exception) as exc:
        reason = _tmdb_image_error_label(exc)
        logger.warning(
            "TMDB image fetch failed: size=%s path=%s mode=%s proxy=%s error=%s",
            size,
            clean_path,
            mode,
            urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}").netloc if proxy_url else "",
            reason,
        )
        return _tmdb_image_placeholder_response(reason)

    tmp_file = cache_file.with_suffix(f"{cache_file.suffix}.tmp")
    tmp_file.write_bytes(content)
    tmp_file.replace(cache_file)
    _prune_tmdb_image_cache(cache_dir)
    return FileResponse(cache_file, media_type=media_type, headers=_tmdb_image_headers(cache_file))


@router.get("/search/media")
def search_media(q: str = Query(default=""), db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    config_row = get_config(db, "tmdb")
    if config_row and config_row.enabled:
        try:
            return {"items": TmdbAdapter(get_decrypted_config(db, "tmdb") or {}).search_media(q)}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"TMDB 搜索失败：{exc}") from exc
    return {"items": MockMetadataAdapter().search_media(q)}


@router.get("/tmdb/media/{media_type}/{media_id}")
def tmdb_media_detail(media_type: str, media_id: str, db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    if media_type not in {"movie", "tv"}:
        raise HTTPException(status_code=404, detail="不支持的 TMDB 媒体类型")
    config_row = get_config(db, "tmdb")
    if config_row and config_row.enabled:
        try:
            return TmdbAdapter(get_decrypted_config(db, "tmdb") or {}).get_media_details(media_id, media_type)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"TMDB 详情获取失败：{exc}") from exc
    return MockMetadataAdapter().get_media_details(media_id, media_type)


@router.get("/tmdb/person/{person_id}")
def tmdb_person_detail(person_id: str, db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    config_row = get_config(db, "tmdb")
    if config_row and config_row.enabled:
        try:
            return TmdbAdapter(get_decrypted_config(db, "tmdb") or {}).get_person_details(person_id)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"TMDB 演员详情获取失败：{exc}") from exc
    return MockMetadataAdapter().get_person_details(person_id)


@router.get("/search/mteam")
def search_mteam(q: str = Query(default=""), db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    if not q.strip():
        return {"items": []}
    try:
        return {"items": get_mteam_adapter_or_error(db).search_torrents(q)}
    except MTeamApiError as exc:
        raise HTTPException(status_code=502, detail=f"M-Team 搜索失败：{exc}") from exc


@router.get("/stats")
def stats(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    snapshots = db.query(QbSnapshot).order_by(QbSnapshot.captured_at.desc()).limit(18).all()
    series = [{"downloader_id": row.downloader_id, "download_speed": row.download_speed, "upload_speed": row.upload_speed, "captured_at": row.captured_at.isoformat(), "source": row.source, "completeness": row.completeness} for row in reversed(snapshots)]
    return {
        "ranges": ["24h", "7d", "30d", "custom"],
        "series": series,
        "explainability": {"source": "下载器原始数据与应用计算数据（Mock）", "formula": "周期增量 = 当前有效快照 - 起始有效快照", "completeness": "完整" if series else "部分缺失"},
    }


@router.post("/assistant/execute")
def assistant_execute(request: AssistantExecuteRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    intent = request.intent
    result = execute_assistant_intent_v2(db, intent, request.message, user)
    return {
        "intent": intent,
        "result": result,
        "reply": format_assistant_result_v2(intent, result),
        "source": "structured_json",
        "handled_at": datetime.utcnow().isoformat(),
    }


@router.post("/assistant/chat")
def assistant_chat(request: AssistantChatRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    adapter = get_ai_adapter_or_error(db)
    mobile_request = WechatClawMessageRequest(message=request.message, user_id=f"web-{user.id}", conversation_id=f"web-{user.id}")
    if MTEAM_SHOW_ALL_RESULTS_RE.search(request.message or ""):
        intent = {"intent_type": "mteam_search", "action": "mteam_search", "show_all": True}
        result = get_wechat_claw_mobile_search_result(db, mobile_request) or {"intent_type": "mteam_search", "state": "selection_expired", "show_all": True}
        return {
            "intent": intent,
            "result": result,
            "reply": format_mobile_agent_reply(intent, result),
            "source": "mteam_cached_results",
            "handled_at": datetime.utcnow().isoformat(),
        }
    try:
        intent = adapter.parse_intent(request.message)
    except AIServiceError as exc:
        raise HTTPException(status_code=502, detail=f"DeepSeek 解析失败：{exc}") from exc
    if intent.get("intent_type") == "general_chat":
        result = {"intent_type": "general_chat"}
        try:
            reply = adapter.answer_general(request.message)
        except AIServiceError:
            reply = format_mobile_agent_reply(intent, result)
    elif intent.get("intent_type") == "download_selected":
        result = {"intent_type": "download_selected", "state": "selection_missing"}
        reply = format_mobile_agent_reply(intent, result)
    else:
        result = execute_mobile_agent_intent(db, intent, mobile_request, user)
        if intent.get("intent_type") == "mteam_search":
            result = enrich_mteam_recommendation(adapter, result)
            result["mobile_selection"] = save_wechat_claw_mobile_candidates(
                db,
                mobile_request,
                result.get("items") or [],
                query=str(result.get("query") or ""),
                recommended_index=result.get("recommended_index"),
                presentation=result.get("presentation"),
            )
        reply = format_mobile_agent_reply(intent, result)
    return {
        "intent": intent,
        "result": result,
        "reply": reply,
        "source": "deepseek",
        "handled_at": datetime.utcnow().isoformat(),
    }


@router.get("/notification-preferences")
def notification_preferences(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"preferences": get_notification_preferences(db), "defaults": DEFAULT_NOTIFICATION_PREFERENCES}


@router.put("/notification-preferences")
def update_notification_preferences(request: NotificationPreferencesPayload, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"preferences": save_notification_preferences(db, request.preferences), "defaults": DEFAULT_NOTIFICATION_PREFERENCES}


@router.get("/admin/wechat-claw/bindings")
def list_wechat_claw_bindings(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    bindings = ensure_wechat_claw_bindings(db)
    return {"items": [wechat_claw_binding_status(db, binding) for binding in bindings]}


@router.post("/admin/wechat-claw/bindings")
def create_wechat_claw_binding(
    request: WechatClawBindingPayload,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    binding = WechatClawBinding(
        display_name=request.display_name.strip() or next_wechat_claw_member_name(db),
        role_name=request.role_name.strip() or "微信助手",
        avatar_key=next_wechat_claw_avatar_key(db),
        enabled=request.enabled,
        notification_preferences=normalized_wechat_claw_binding_preferences(request.notification_preferences),
    )
    db.add(binding)
    db.commit()
    db.refresh(binding)
    return serialize_wechat_claw_binding(binding)


@router.patch("/admin/wechat-claw/bindings/{binding_id}")
def update_wechat_claw_binding(
    binding_id: int,
    request: WechatClawBindingPayload,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    binding = get_wechat_claw_binding_or_error(db, binding_id)
    binding.display_name = request.display_name.strip() or binding.display_name
    binding.role_name = request.role_name.strip() or binding.role_name
    binding.enabled = request.enabled
    binding.notification_preferences = normalized_wechat_claw_binding_preferences(request.notification_preferences)
    binding.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(binding)
    return serialize_wechat_claw_binding(binding)


@router.delete("/admin/wechat-claw/bindings/{binding_id}")
def delete_wechat_claw_binding(binding_id: int, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    binding = get_wechat_claw_binding_or_error(db, binding_id)
    for key in (WECHAT_CLAW_ILINK_STATE_KEY, WECHAT_CLAW_INTERACTIONS_KEY, WECHAT_CLAW_LAST_POLL_KEY):
        row = db.query(Setting).filter(Setting.key == wechat_claw_setting_key(key, binding.id)).one_or_none()
        if row:
            db.delete(row)
    db.delete(binding)
    db.commit()
    return {"deleted": True, "id": binding_id}


@router.get("/admin/wechat-claw/bindings/{binding_id}/setup")
def wechat_claw_binding_setup(
    binding_id: int,
    refresh_remote: bool = Query(default=True),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    get_wechat_claw_binding_or_error(db, binding_id)
    scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(binding_id)
    try:
        if refresh_remote:
            refresh_wechat_claw_login_state(db)
        return build_wechat_claw_setup(db, binding_id)
    finally:
        _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)


@router.post("/admin/wechat-claw/bindings/{binding_id}/{action}")
def wechat_claw_binding_action(
    binding_id: int,
    action: str,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    get_wechat_claw_binding_or_error(db, binding_id)
    if action not in {"refresh", "logout", "poll"}:
        raise HTTPException(status_code=404, detail="未知的 WeChat claw 操作")
    scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(binding_id)
    try:
        if action == "refresh":
            return _refresh_wechat_claw_qrcode(db, binding_id)
        if action == "logout":
            clear_wechat_claw_ilink_state(db)
            return build_wechat_claw_setup(db, binding_id)
        return poll_wechat_claw_messages(db, binding_id)
    finally:
        _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)


@router.get("/admin/wechat-claw/setup")
def admin_wechat_claw_setup(
    refresh_remote: bool = Query(default=True),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    bindings = ensure_wechat_claw_bindings(db)
    if not bindings:
        return build_wechat_claw_setup(db)
    binding_id = _WECHAT_CLAW_ACTIVE_USER_ID.get() or bindings[0].id
    scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(binding_id)
    try:
        if refresh_remote:
            refresh_wechat_claw_login_state(db)
        return build_wechat_claw_setup(db, binding_id)
    finally:
        _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)


@router.post("/admin/wechat-claw/refresh")
def admin_wechat_claw_refresh(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    bindings = ensure_wechat_claw_bindings(db)
    if not bindings:
        raise HTTPException(status_code=409, detail="请先添加一个 WeChat claw 绑定")
    binding_id = _WECHAT_CLAW_ACTIVE_USER_ID.get() or bindings[0].id
    scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(binding_id)
    try:
        return _refresh_wechat_claw_qrcode(db, binding_id)
    finally:
        _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)


def _refresh_wechat_claw_qrcode(db: Session, binding_id: int) -> dict[str, Any]:
    config = get_decrypted_config(db, "wechat_claw") or {}
    adapter = WechatClawAdapter(config)
    result = adapter.get_qrcode()
    if not result.get("success"):
        raise HTTPException(status_code=502, detail=result.get("message") or "获取微信二维码失败")
    qrcode = {
        "qrcode": result.get("qrcode"),
        "qrcode_url": result.get("qrcode_url"),
        "status": result.get("status") or "waiting",
        "updated_at": int(datetime.utcnow().timestamp()),
    }
    # A fresh QR code starts a new iLink session. Retaining the old token here
    # would prevent the background worker from polling the new QR status.
    update_wechat_claw_ilink_state(
        db,
        qrcode=qrcode,
        base_url=adapter.base_url,
        bot_token="",
        account_id="",
        sync_buf="",
        known_targets={},
    )
    return build_wechat_claw_setup(db, binding_id)


@router.post("/admin/wechat-claw/logout")
def admin_wechat_claw_logout(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    bindings = ensure_wechat_claw_bindings(db)
    if not bindings:
        raise HTTPException(status_code=409, detail="请先添加一个 WeChat claw 绑定")
    binding_id = _WECHAT_CLAW_ACTIVE_USER_ID.get() or bindings[0].id
    scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(binding_id)
    try:
        clear_wechat_claw_ilink_state(db)
        return build_wechat_claw_setup(db, binding_id)
    finally:
        _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)


def require_wechat_claw_token(db: Session, header_token: str | None, query_token: str | None) -> None:
    config = get_decrypted_config(db, "wechat_claw") or {}
    expected = str(config.get("inbound_token") or "").strip()
    provided = str(header_token or query_token or "").strip()
    if not expected or not provided or provided != expected:
        raise HTTPException(status_code=401, detail="Invalid WeChat claw token")


def assistant_actor_for_wechat_claw(db: Session) -> User:
    user = db.query(User).filter(User.role == "admin", User.is_active.is_(True)).order_by(User.id.asc()).first()
    if user is None:
        raise HTTPException(status_code=409, detail="请先初始化管理员账号")
    return user


def handle_wechat_claw_text(
    db: Session,
    request: WechatClawMessageRequest,
    telemetry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    telemetry = telemetry or new_wechat_claw_interaction_telemetry()
    interaction_status = "completed"
    user = assistant_actor_for_wechat_claw(db)
    verify_match = WECHAT_CLAW_ADMIN_VERIFY_RE.match(request.message or "")
    provided_password = str(request.admin_password or (verify_match.group(1) if verify_match else "")).strip()
    if provided_password:
        parsed_intent = {"intent_type": "dashboard_query", "action": "dashboard_query", "dashboard_sections": ["qb2"]}
        if verify_password(provided_password, user.password_hash):
            grant_wechat_claw_privacy_access(db, request)
            result = {"intent_type": "dashboard_query", "privacy_granted": True, "qb2": {}}
            reply = "【管理员验证成功】\n当前微信会话已获得 qB2 隐私详情权限，有效期 15 分钟。"
        else:
            result = {"intent_type": "dashboard_query", "privacy_granted": False}
            reply = "【管理员验证失败】\n密码不正确，未授予 qB2 隐私详情权限。"
        interaction = record_wechat_claw_interaction(db, request, parsed_intent, result, reply, telemetry)
        return {"reply": reply, "intent": parsed_intent, "result": result, "interaction": interaction, "source": "wechat_claw", "conversation_id": request.conversation_id, "handled_at": datetime.utcnow().isoformat()}

    if MTEAM_SHOW_ALL_RESULTS_RE.search(request.message or ""):
        parsed_intent = {"intent_type": "mteam_search", "action": "mteam_search", "show_all": True}
        result = get_wechat_claw_mobile_search_result(db, request) or {"intent_type": "mteam_search", "state": "selection_expired", "show_all": True}
        render_started = time.perf_counter()
        reply = format_mobile_agent_reply(parsed_intent, result)
        record_wechat_claw_stage(telemetry, "mteam_show_all", render_started)
        interaction = record_wechat_claw_interaction(db, request, parsed_intent, result, reply, telemetry)
        return {"reply": reply, "intent": parsed_intent, "result": result, "interaction": interaction, "source": "wechat_claw", "conversation_id": request.conversation_id, "handled_at": datetime.utcnow().isoformat()}

    adapter_started = time.perf_counter()
    try:
        ai_adapter = get_ai_adapter_or_error(db)
    except HTTPException as exc:
        record_wechat_claw_stage(telemetry, "ai_adapter", adapter_started, status="failed", error=exc.detail)
        raise
    record_wechat_claw_stage(telemetry, "ai_adapter", adapter_started)
    pending_candidate = get_wechat_claw_pending_download(db, request)
    selection_index = infer_mobile_selection(request.message)
    if pending_candidate and WECHAT_CLAW_DOWNLOAD_CONFIRM_RE.search(request.message or ""):
        parsed_intent = {"intent_type": "download_selected", "action": "download_selected", "download_confirmation": True}
        stage_started = time.perf_counter()
        try:
            dispatch = download_wechat_claw_selected_torrent(db, str(pending_candidate["id"]), user)
            clear_wechat_claw_pending_download(db, request)
            result = {"intent_type": "download_selected", "state": "accepted", "candidate": pending_candidate, **dispatch}
        except HTTPException as exc:
            record_wechat_claw_stage(telemetry, "download_selected", stage_started, status="failed", error=exc.detail)
            result = {"intent_type": "download_selected", "state": "failed", "candidate": pending_candidate, "message": str(exc.detail)}
        else:
            record_wechat_claw_stage(telemetry, "download_selected", stage_started)
        render_started = time.perf_counter()
        reply = format_mobile_agent_reply(parsed_intent, result)
        record_wechat_claw_stage(telemetry, "reply_render", render_started)
        interaction = record_wechat_claw_interaction(db, request, parsed_intent, result, reply, telemetry)
        return {"reply": reply, "intent": parsed_intent, "result": result, "interaction": interaction, "source": "wechat_claw", "conversation_id": request.conversation_id, "handled_at": datetime.utcnow().isoformat()}

    if selection_index:
        stage_started = time.perf_counter()
        candidate = get_wechat_claw_mobile_candidate(db, request, 1 if selection_index == -1 else selection_index)
        parsed_intent = {"intent_type": "download_selected", "action": "download_selected", "selection_index": selection_index, "download_confirmation": False}
        if candidate:
            save_wechat_claw_pending_download(db, request, candidate)
            result = {"intent_type": "download_selected", "state": "awaiting_confirmation", "candidate": candidate}
        else:
            result = {"intent_type": "download_selected", "state": "selection_missing"}
        record_wechat_claw_stage(telemetry, "download_selection", stage_started)
        render_started = time.perf_counter()
        reply = format_mobile_agent_reply(parsed_intent, result)
        record_wechat_claw_stage(telemetry, "reply_render", render_started)
        interaction = record_wechat_claw_interaction(db, request, parsed_intent, result, reply, telemetry)
        return {"reply": reply, "intent": parsed_intent, "result": result, "interaction": interaction, "source": "wechat_claw", "conversation_id": request.conversation_id, "handled_at": datetime.utcnow().isoformat()}

    intent_started = time.perf_counter()
    try:
        parsed_intent = ai_adapter.parse_intent(request.message)
    except AIServiceError as exc:
        record_wechat_claw_stage(telemetry, "ai_intent", intent_started, status="failed", error=exc)
        raise HTTPException(status_code=502, detail=f"DeepSeek 解析失败：{exc}") from exc
    record_wechat_claw_stage(telemetry, "ai_intent", intent_started)
    if parsed_intent.get("intent_type") == "general_chat":
        result = {"intent_type": "general_chat"}
        answer_started = time.perf_counter()
        try:
            reply = ai_adapter.answer_general(request.message, mobile_agent_history(db, request))
        except AIServiceError as exc:
            record_wechat_claw_stage(telemetry, "ai_general_answer", answer_started, status="failed", error=exc)
            raise HTTPException(status_code=502, detail=f"DeepSeek 回复失败：{exc}") from exc
        record_wechat_claw_stage(telemetry, "ai_general_answer", answer_started)
    elif parsed_intent.get("intent_type") == "download_selected":
        parsed_index = int(parsed_intent.get("selection_index") or 0)
        if not parsed_index and str(parsed_intent.get("selection_reference") or "").startswith("recommend"):
            parsed_index = -1
        candidate = get_wechat_claw_mobile_candidate(db, request, 1 if parsed_index == -1 else parsed_index) if parsed_index else get_wechat_claw_pending_download(db, request)
        if candidate:
            save_wechat_claw_pending_download(db, request, candidate)
            result = {"intent_type": "download_selected", "state": "awaiting_confirmation", "candidate": candidate}
        else:
            result = {"intent_type": "download_selected", "state": "selection_missing"}
        render_started = time.perf_counter()
        reply = format_mobile_agent_reply(parsed_intent, result)
        record_wechat_claw_stage(telemetry, "reply_render", render_started)
    else:
        tool_started = time.perf_counter()
        tool_stage = str(parsed_intent.get("intent_type") or "tool")
        try:
            result = execute_mobile_agent_intent(db, parsed_intent, request, user)
        except Exception as exc:
            record_wechat_claw_stage(telemetry, tool_stage, tool_started, status="failed", error=getattr(exc, "detail", exc))
            raise
        if result.get("state") == "failed":
            interaction_status = "failed"
            record_wechat_claw_stage(telemetry, tool_stage, tool_started, status="failed", error=result.get("error") or result.get("message"))
        else:
            record_wechat_claw_stage(telemetry, tool_stage, tool_started)
        if parsed_intent.get("intent_type") == "mteam_search":
            recommendation_started = time.perf_counter()
            result = enrich_mteam_recommendation(ai_adapter, result)
            result["mobile_selection"] = save_wechat_claw_mobile_candidates(
                db,
                request,
                result.get("items") or [],
                query=str(result.get("query") or ""),
                recommended_index=result.get("recommended_index"),
                presentation=result.get("presentation"),
            )
            record_wechat_claw_stage(telemetry, "ai_recommendation", recommendation_started)
        render_started = time.perf_counter()
        reply = format_mobile_agent_reply(parsed_intent, result)
        record_wechat_claw_stage(telemetry, "reply_render", render_started)
    interaction = record_wechat_claw_interaction(db, request, parsed_intent, result, reply, telemetry, status=interaction_status)
    return {
        "reply": reply,
        "intent": parsed_intent,
        "result": result,
        "interaction": interaction,
        "source": "wechat_claw",
        "conversation_id": request.conversation_id,
        "handled_at": datetime.utcnow().isoformat(),
    }


def poll_wechat_claw_messages(db: Session, user_id: int | None = None) -> dict[str, Any]:
    # iLink advances one shared get_updates_buf cursor. Never allow the admin
    # diagnostic button to race the background long-poll in this process.
    scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(user_id)
    lock = wechat_claw_poll_lock(user_id)
    if not lock.acquire(blocking=False):
        pending_count = len(get_wechat_claw_pending_messages(get_wechat_claw_ilink_state(db)))
        _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)
        return {
            "success": False,
            "stage": "busy",
            "raw_count": 0,
            "parsed_count": 0,
            "handled_count": 0,
            "reply_sent_count": 0,
            "pending_count": pending_count,
            "message": "WeChat claw 正在由后台接收消息，请勿并发轮询。",
        }
    try:
        return _poll_wechat_claw_messages_locked(db)
    finally:
        lock.release()
        _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)


def _poll_wechat_claw_messages_locked(db: Session) -> dict[str, Any]:
    refresh_wechat_claw_login_state(db)
    row = get_config(db, "wechat_claw")
    if not row or not row.enabled or not row.encrypted_payload:
        return save_wechat_claw_last_poll(db, {"success": False, "stage": "disabled", "message": "WeChat claw 未启用"})
    config = get_decrypted_config(db, "wechat_claw") or {}
    state = get_wechat_claw_ilink_state(db)
    adapter = WechatClawAdapter(wechat_claw_config_with_state(config, state))
    if not adapter.bot_token:
        return save_wechat_claw_last_poll(db, {"success": False, "stage": "login", "message": "尚未完成微信绑定，请扫描二维码后重试。"})

    # A reply that failed after iLink had delivered its update is durable here.
    # Retry it before starting another long-poll so failures recover promptly.
    previous = process_wechat_claw_pending_messages(db, adapter)
    if previous["pending_count"]:
        status = save_wechat_claw_last_poll(
            db,
            {
                "success": False,
                "stage": "retry_wait",
                "handled_count": len(previous["handled"]),
                "reply_sent_count": previous["reply_sent_count"],
                "pending_count": previous["pending_count"],
                "message": "消息已保留，正在等待下一次自动重试。",
            },
        )
        return {**status, "handled": previous["handled"], "retry_after_seconds": previous["retry_after_seconds"] or 1.0}
    try:
        poll_result = adapter.poll_updates(timeout_seconds=int(config.get("poll_timeout") or 25))
    except WechatClawApiError as exc:
        return save_wechat_claw_last_poll(
            db,
            {"success": False, "stage": "getupdates", "message": str(exc), "raw_count": 0, "parsed_count": 0},
        )
    if not poll_result.get("success") and is_wechat_claw_session_timeout(poll_result.get("message")):
        state = get_wechat_claw_ilink_state(db)
        current_qrcode = state.get("qrcode") if isinstance(state.get("qrcode"), dict) else {}
        # Keep a newly generated waiting QR code so the worker can finish the new login.
        clear_wechat_claw_session(db, None if current_qrcode.get("status") == "waiting" else "expired")
        return save_wechat_claw_last_poll(
            db,
            {
                "success": False,
                "stage": "session_expired",
                "raw_count": poll_result.get("raw_count", 0),
                "parsed_count": poll_result.get("parsed_count", 0),
                "message": "微信登录已过期，请刷新二维码重新绑定。",
            },
        )
    queue_wechat_claw_messages(db, poll_result.get("messages") or [], poll_result.get("sync_buf"))
    processed = process_wechat_claw_pending_messages(db, adapter)
    status = save_wechat_claw_last_poll(
        db,
        {
            "success": bool(poll_result.get("success")),
            "stage": "handled" if processed["handled"] else "getupdates",
            "raw_count": poll_result.get("raw_count", 0),
            "parsed_count": poll_result.get("parsed_count", 0),
            "handled_count": len(processed["handled"]),
            "reply_sent_count": processed["reply_sent_count"],
            "pending_count": processed["pending_count"],
            "message": poll_result.get("message"),
        },
    )
    return {**status, "handled": processed["handled"], "retry_after_seconds": processed["retry_after_seconds"]}


@router.post("/wechat-claw/message")
def wechat_claw_message(
    request: WechatClawMessageRequest,
    x_wechat_claw_token: str | None = Header(default=None),
    token: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    get_wechat_claw_adapter_or_error(db)
    require_wechat_claw_token(db, x_wechat_claw_token, token)
    return handle_wechat_claw_text(db, request)


@router.post("/admin/wechat-claw/poll")
def admin_wechat_claw_poll(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    return poll_wechat_claw_messages(db)


@router.get("/me/wechat-claw/setup")
def my_wechat_claw_setup(
    refresh_remote: bool = Query(default=True),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(user.id)
    try:
        return admin_wechat_claw_setup(refresh_remote, user, db)
    finally:
        _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)


@router.post("/me/wechat-claw/refresh")
def my_wechat_claw_refresh(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(user.id)
    try:
        return admin_wechat_claw_refresh(user, db)
    finally:
        _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)


@router.post("/me/wechat-claw/logout")
def my_wechat_claw_logout(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(user.id)
    try:
        return admin_wechat_claw_logout(user, db)
    finally:
        _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)


@router.post("/me/wechat-claw/poll")
def my_wechat_claw_poll(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return poll_wechat_claw_messages(db, user.id)


@router.get("/wechat-claw/capabilities")
def wechat_claw_capabilities(
    x_wechat_claw_token: str | None = Header(default=None),
    token: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    adapter = get_wechat_claw_adapter_or_error(db)
    require_wechat_claw_token(db, x_wechat_claw_token, token)
    preferences = get_notification_preferences(db)
    return {
        "name": "PT Media Hub WeChat claw",
        "public_base_url": adapter.public_base_url,
        "mobile_app_url": adapter.public_base_url,
        "message_endpoint": f"{adapter.public_base_url}/api/wechat-claw/message",
        "capabilities_endpoint": f"{adapter.public_base_url}/api/wechat-claw/capabilities",
        "mobile_sections": WECHAT_CLAW_MOBILE_SECTIONS,
        "capabilities": WECHAT_CLAW_CAPABILITIES,
        "notification_preferences": preferences,
    }


@router.get("/notifications")
def notifications(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.query(Notification).order_by(Notification.created_at.desc()).limit(50).all()
    if not rows:
        rows = [Notification(title="示例通知", message="下载监控已在线。", level="info", source="mock"), Notification(title="示例阈值", message="NAS 剩余空间状态健康。", level="success", source="mock")]
    return {"items": [{"id": row.id, "title": row.title, "message": row.message, "level": row.level, "read": row.read, "source": row.source, "created_at": row.created_at.isoformat() if row.created_at else None} for row in rows]}


@router.get("/diagnostics/health")
def diagnostics_health(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    integrations = {row.provider: row for row in db.query(IntegrationConfig).all()}
    modules = []
    for provider in PROVIDERS + ["nas_disk", "stats_engine"]:
        row = integrations.get(provider)
        result = row.last_test_result if row else None
        if provider in ["nas_disk", "stats_engine"]:
            status = "success"
        elif result and result.get("success") is True:
            status = "success"
        elif result and result.get("success") is False:
            status = "failed"
        else:
            status = "not_tested"
        collection = get_module_health(db, provider)
        modules.append({"module": provider, "status": status, "enabled": bool(row.enabled) if row else provider in ["nas_disk", "stats_engine"], "last_success_at": collection.get("last_success_at") or (row.last_tested_at.isoformat() if row and row.last_tested_at else None), "duration_ms": result.get("duration_ms") if result else None, "last_error": collection.get("last_error") or (result.get("message") if isinstance(result, dict) else None), "consecutive_failed_hours": int(collection.get("consecutive_failed_hours") or 0)})
    bindings = ensure_wechat_claw_bindings(db)
    return {"modules": modules, "wechat_members": [wechat_claw_binding_status(db, binding) for binding in bindings]}


@router.get("/diagnostics/traces")
def diagnostics_traces(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.query(DebugTrace).order_by(DebugTrace.created_at.desc()).limit(50).all()
    return {"items": [{"trace_id": row.trace_id, "event_type": row.event_type, "status": row.status, "timeline": row.timeline, "duration_ms": row.duration_ms, "config_version": row.config_version, "error_summary": row.error_summary, "created_at": row.created_at.isoformat()} for row in rows]}


@router.post("/diagnostics/export")
def diagnostics_export(user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    export_trace = trace_id("DIAG")
    traces = db.query(DebugTrace).order_by(DebugTrace.created_at.desc()).limit(50).all()
    errors = db.query(DebugTrace).filter(DebugTrace.status == "failed").order_by(DebugTrace.created_at.desc()).limit(20).all()
    payload = redact_payload(
        {
            "version": settings.app_version,
            "environment": settings.environment,
            "timezone": "UTC",
            "cpu_arch": "runtime-detected-by-container",
            "recent_traces": [row.timeline for row in traces],
            "recent_errors": [{"trace_id": row.trace_id, "error": row.error_summary} for row in errors],
            "health": diagnostics_health(user, db),
            "generated_at": datetime.utcnow().isoformat(),
        }
    )
    db.add(DiagnosticExport(trace_id=export_trace, payload=payload, created_by=user.id))
    db.commit()
    return {"trace_id": export_trace, "payload": payload}
